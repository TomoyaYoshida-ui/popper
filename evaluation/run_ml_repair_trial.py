"""Real-data sandbox repair plumbing trial with an explicitly scripted policy."""
from __future__ import annotations

import json
import runpy
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from popper.core import initialize, write_json, read_json
from popper.research.controller import ResearchController
from popper.research.execution import EXECUTED_COVERAGE
from popper.research.models import EvidenceDrivenPolicy
from popper.research.revisions import CodeEdit


FAULT = "\nraise RuntimeError('repair_trial_injected_failure')\n"


class ScriptedRepairPolicy(EvidenceDrivenPolicy):
    name = "scripted_repair_fixture"

    def propose_revision(self, objective, hypothesis, config, code_files,
                         parent_revision=None, failure=None):
        source = next(item for item in code_files if item["path"] == "model.py")
        if parent_revision is None:
            content = source["content"] + FAULT
        else:
            stderr = failure["logs"]["stderr.log"]["text"]
            if "RuntimeError: repair_trial_injected_failure" not in stderr:
                raise RuntimeError("Expected verified injected failure traceback")
            if not source["content"].endswith(FAULT):
                raise RuntimeError("Expected immutable failed parent")
            content = source["content"][:-len(FAULT)] + "\n# repaired fixture\n"
        content = content.replace(
            'predictions = model.predict([row["features"] for row in inputs])',
            'predictions = list(model.predict([row["features"] for row in inputs]))')
        return {"edits": (CodeEdit("model.py", content, source["sha256"]),),
                "rationale": "受控故障注入/修复，仅验证工程执行链路。"}


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    trial = ROOT / "evaluation/runs" / f"ml-repair-{stamp}"
    project, run_dir = trial / "project", trial / "research"
    source = ROOT / "examples/breast-cancer-wisconsin"
    project.mkdir(parents=True)
    for name in ("experiment.json", "model.py", "prepare_data.py"):
        shutil.copyfile(source / name, project / name)
    runpy.run_path(str(project / "prepare_data.py"))["prepare"](project)
    initialize(project)
    policy = ScriptedRepairPolicy()
    ResearchController.initialize(project, run_dir, policy=policy)
    controller = ResearchController(run_dir, policy=policy)
    try:
        result = controller.run(sandboxed=True, autonomous_code=True, max_steps=1)
        receipts = [read_json(path) for path in sorted((run_dir / "jobs").glob("JOB-*/receipt.json"))]
        revisions = [read_json(path) for path in sorted((run_dir / "revisions").glob("REV-*/revision.json"))]
        # 回执口径（与 `run_live_ml_trial.py` 同形）：冻结基线自 C2 起也经
        # `_evaluate_registered_via_worker` 逐 seed 走 worker（identity 为
        # `registered_implementation_dev`），其回执按设计 `require_edit_coverage=False`，
        # 因此连 `execution_gate` 键都没有；只有生成的 revision 才置 `require_edit_coverage=True`。
        # 而 worker 只在 `require_edit_coverage` 时写这个键，值仍是初值 None——候选若在到达 gate
        # 之前就非零退出（注入故障 → `CalledProcessError`），gate 保持 None。于是正常轨迹是三类
        # 各 len(seeds) 条：基线 succeeded（无键）+ 注入故障 implementation_failed（键为 None）
        # + 修复后 succeeded（键为非空 gate，passed=True）。原先的 `sum(succeeded) == 3` /
        # `sum(implementation_failed) == 1` 是基线还不走 worker、且注入故障只发一次的旧形态留下的
        # 期望值，会把这份正常轨迹判失败。
        seeds = read_json(project / "experiment.json")["seeds"]
        succeeded = [r for r in receipts if r["status"] == "succeeded"]
        failed = [r for r in receipts if r["status"] == "implementation_failed"]
        gated = [r for r in receipts if r.get("execution_gate")]
        ok = (result["integrity"]["ok"]
              and len(result["observations"]) == 2
              and result["code_revisions"] == 2
              # 每个回执要么 succeeded、要么属于注入故障那批；注入故障逐 seed 一条。
              and len(succeeded) + len(failed) == len(receipts)
              and len(failed) == len(seeds)
              and all(r.get("error_type") for r in failed)
              # 非空 gate 只属于修复后的生成 revision：逐 seed 一条，且改动语句确被执行。
              and len(gated) == len(seeds)
              and all(r["execution_gate"]["passed"] for r in gated)
              and all(r["execution_gate"]["coverage"] in EXECUTED_COVERAGE
                      for r in gated)
              and all(r["execution_backend"] == "windows_low_integrity" for r in receipts)
              and any(r["identity"]["parent_revision_id"] for r in revisions)
              and result["budget"]["spent"] == 3)
        summary = {"classification": "scripted_real_ml_engineering_trial",
                   "claim_limit": "真实公开数据与沙箱执行；脚本策略，不是模型自主研究或创新证明；未消费最终测试集。",
                   "passed": bool(ok), "status": result,
                   "worker_receipts": receipts,
                   "revision_ids": [r["revision_id"] for r in revisions]}
        write_json(trial / "trial-summary.json", summary)
        print(json.dumps({"passed": bool(ok), "summary": str(trial / "trial-summary.json"),
                          "phase": result["phase"], "budget": result["budget"]},
                         ensure_ascii=False, indent=2))
        return 0 if ok else 2
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
