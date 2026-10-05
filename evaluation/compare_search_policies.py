"""离线比较一次实现加一次修复，和开发集上的实验树搜索。

脚本分数比较不执行项目。示例比较会复制 ODE 与 Wisconsin，用确定性提案走真实独立评分，
不调用付费模型，也不消费确认集。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from popper.core import initialize
from popper.research.actions import RUN_EXPERIMENT, ActionProposal
from popper.research.controller import ResearchController
from popper.research.models import EvidenceDrivenPolicy
from popper.research.revisions import CodeEdit
from popper.research.tree_search import best_node, choose_action, node_record

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def one_shot(outcomes):
    """第一次成功即停止；只有第一次失败时才再看一次修复。"""
    tried = outcomes[:1]
    if tried and tried[0].get("value") is None:
        tried = outcomes[:2]
    scored = [item for item in tried if item.get("value") is not None]
    return {"policy": "one_shot_repair", "tried": len(tried),
            "value": None if not scored else scored[0]["value"]}


def tree_search(outcomes, *, direction="min", num_drafts=2):
    nodes = []
    cursor = 0
    while cursor < len(outcomes):
        decision = choose_action(nodes, num_drafts=num_drafts, debug_prob=1, direction=direction)
        item = outcomes[cursor]
        status = "buggy" if item.get("value") is None else "scored"
        parent = decision["parent_id"]
        depth = 0 if parent is None else next(
            node["depth"] + 1 for node in nodes if node["node_id"] == parent)
        nodes.append(node_record(
            f"node-{cursor}", decision["kind"], parent, depth, status, item.get("value")))
        cursor += 1
    winner = best_node(nodes, direction)
    return {"policy": "tree_search", "tried": len(nodes),
            "value": None if winner is None else winner["value"],
            "representative": None if winner is None else winner["node_id"]}


def compare(outcomes, *, baseline, direction="min", threshold=0.2):
    policies = [one_shot(outcomes), tree_search(outcomes, direction=direction)]
    for policy in policies:
        delta = None if policy["value"] is None else (
            baseline - policy["value"] if direction == "min" else policy["value"] - baseline)
        policy["delta"] = None if delta is None else round(delta, 6)
        policy["reaches_threshold"] = delta is not None and delta >= threshold
    return {"schema_version": "1.0", "baseline": baseline, "direction": direction,
            "threshold": threshold, "paid_model_calls": 0, "policies": policies}


def _replace_once(source, old, new):
    if old not in source:
        raise ValueError(f"脚本锚点不在源码中: {old!r}")
    return source.replace(old, new, 1)


def _failed(source):
    marker = "raise RuntimeError('scripted draft failure')\n"
    future = "from __future__ import annotations\n"
    if future in source:
        return source.replace(future, future + marker, 1)
    return marker + source


def ode_variants(source):
    failed = _failed(source)
    weak = _replace_once(source, "        return y + h * (k1 + k2) / 2",
                         "        return y + h * (-rate * y)")
    restored = _replace_once(source, "        return y + h * (k1 + k2) / 2",
                             "        slope = (k1 + k2) / 2\n        return y + h * slope")
    return [failed, weak, restored]


def wisconsin_variants(source):
    failed = _failed(source)
    weak = _replace_once(
        source, "predictions = model.predict([row[\"features\"] for row in inputs])",
        "predictions = model.predict([row[\"features\"] for row in inputs])\n"
        "    if read_json(args.config).get(\"model\") == \"logistic_regression\":\n"
        "        predictions = [0 for _ in predictions]")
    restored = _replace_once(source, "max_iter=2000, random_state=seed))",
                             "max_iter=4000, random_state=seed))")
    return [failed, weak, restored]


class ScriptedHarness:
    """按固定顺序给出失败、弱实现和恢复实现。不读取模型。"""

    name = "scripted_offline_harness"
    model = None

    def __init__(self, path, variants):
        self.path = path
        self.variants = variants
        self.calls = 0

    def propose_revision(self, objective, hypothesis, config, code_files,
                         parent_revision=None, failure=None):
        source = next(row for row in code_files if row["path"] == self.path)
        content = self.variants[min(self.calls, len(self.variants) - 1)]
        self.calls += 1
        return {"edits": (CodeEdit(self.path, content, source["sha256"]),),
                "rationale": f"离线脚本提案 {self.calls}"}


class FixedCandidatePolicy(EvidenceDrivenPolicy):
    name = "fixed_candidate_policy"

    def __init__(self, hypothesis_id):
        self.hypothesis_id = hypothesis_id

    def choose(self, context):
        return ActionProposal(RUN_EXPERIMENT, "离线比较只检验第一个预注册候选。",
                              self.hypothesis_id, source=self.name)


def _executor(command, workspace, env, stdout, stderr, spec):
    subprocess.run(command, cwd=workspace, env=env, stdout=stdout, stderr=stderr,
                   timeout=spec.timeout_seconds, check=True)


def _summarize(status, manifest, policy_name):
    direction = manifest["metric"]["direction"]
    observations = [row for row in status["observations"] if row.get("scope") == "dev"]
    control_id = manifest["control_hypothesis_id"]
    baseline = next(row["value"] for row in observations if row["hypothesis_id"] == control_id)
    candidates = [row for row in observations if row["hypothesis_id"] != control_id]
    if not candidates:
        best, delta = None, None
    elif direction == "max":
        best = max(row["value"] for row in candidates)
        delta = best - baseline
    else:
        best = min(row["value"] for row in candidates)
        delta = baseline - best
    threshold = manifest["min_meaningful_effect"]
    return {"policy": policy_name, "baseline": round(baseline, 6),
            "best_value": None if best is None else round(best, 6),
            "delta": None if delta is None else round(delta, 6),
            "reaches_threshold": delta is not None and delta >= threshold,
            "code_revisions": status["code_revisions"],
            "budget_spent": status["budget"]["spent"],
            "phase": status["phase"],
            "last_decision": None if not status["decisions"] else status["decisions"][-1]["action"]}


def run_domain(domain, variants_for):
    source = EXAMPLES / domain
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "project"
        shutil.copytree(source, project, ignore=shutil.ignore_patterns(".popper"))
        initialize(project)
        results = []
        for policy_name, search, steps in (
                ("one_shot_repair", False, 1), ("tree_search", True, 4)):
            run_dir = root / policy_name
            ResearchController.initialize(project, run_dir)
            manifest = json.loads((run_dir / "research.json").read_text(encoding="utf-8"))
            pristine = (project / "model.py").read_text(encoding="utf-8")
            controller = ResearchController(
                run_dir, policy=FixedCandidatePolicy(manifest["candidate_hypothesis_ids"][0]),
                harness=ScriptedHarness("model.py", variants_for(pristine)))
            controller.worker.executor = _executor
            controller._require_sandbox_backend = lambda: None
            try:
                status = controller.run(
                    sandboxed=True, autonomous_code=True, search=search, max_steps=steps,
                    auto_confirm=False)
                results.append(_summarize(status, manifest, policy_name))
            finally:
                controller.close()
        return {"domain": domain, "paid_model_calls": 0, "execution": "injected_executor",
                "confirm_consumed": False, "policies": results}


def compare_examples():
    domains = [run_domain("ode-convergence", ode_variants)]
    try:
        import sklearn  # noqa: F401
    except ImportError:
        domains.append({"domain": "breast-cancer-wisconsin", "skipped": "sklearn 不可用",
                        "paid_model_calls": 0})
    else:
        domains.append(run_domain("breast-cancer-wisconsin", wisconsin_variants))
    return {"schema_version": "1.0", "paid_model_calls": 0, "domains": domains}


def _write(name, report):
    destination = Path(__file__).resolve().parent / "runs" / name / "report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def main():
    scripted = compare([
        {"value": 0.9}, {"value": None}, {"value": 0.4}, {"value": 0.2},
    ], baseline=1.0)
    _write("search-policy-offline", scripted)
    examples = compare_examples()
    _write("search-policy-examples", examples)
    return examples


if __name__ == "__main__":
    main()
