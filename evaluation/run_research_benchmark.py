"""Frozen, equal-cap, exploratory research benchmark with separate confirmation."""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from popper import sandbox
from popper.core import (ProtocolError, Experiment, canonical, digest, file_hash,
                         initialize, read_json, write_json)
from popper.harness.json_agent import JSONAgentHarness
from popper.model_budget import ModelRequestBudget
from popper.research.actions import RUN_EXPERIMENT, ActionProposal
from popper.research.controller import ResearchController, RevisionExecutionError
from popper.research.envelope import build_envelope, save_envelope
from popper.research.models import DeepSeekResearchPolicy
from popper.research.prediction import evaluate_prediction, research_contract
from popper.research.confirmation_contracts import (load_private_key, public_key_b64,
                                                   sign_payload, verify_envelope)
from popper.scoop import make_json_client

ROOT = Path(__file__).resolve().parents[1]
TASKS = ("breast-cancer-wisconsin", "pima-zero-imputation", "quadratic",
         "ode-convergence", "throughput-sort", "treatment-effect")
STRATEGIES = ("one_shot_repair", "linear_iteration", "autonomous_tree")
GLOBAL_LEDGER = ROOT / ".popper/research-benchmark-requests.sqlite"
IMPLEMENTATION = ("popper/core.py", "popper/scoop.py", "popper/model_budget.py",
    "popper/research/controller.py", "popper/research/store.py", "popper/research/autonomy.py",
    "popper/research/prediction.py", "popper/research/envelope.py", "popper/research/models.py",
    "popper/research/evaluation_service.py", "evaluation/run_research_benchmark.py")


def source_snapshot():
    return {name: file_hash(ROOT / name) for name in IMPLEMENTATION}


def freeze_protocol(out):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tasks = []
    for index, name in enumerate(TASKS):
        source = ROOT / "examples" / name
        spec = read_json(source / "experiment.json")
        files = sorted({"experiment.json", *spec["code_files"], *spec.get("provenance_files", []),
                        spec["train"], spec["dev"], spec["test"]})
        tasks.append({"id": name, "index": index, "files": {p: file_hash(source / p) for p in files},
                      "metric": spec["metric"], "threshold": spec["min_improvement"]})
    cells = [{"id": f"t{t['index']}s{s}r{r}", "task": t["id"], "strategy": strategy,
              "repeat": r} for t in tasks for r in range(3) for s, strategy in enumerate(STRATEGIES)]
    random.Random(20261007).shuffle(cells)
    protocol = {"schema": "popper.research-cohort/v2", "tasks": tasks, "cells": cells,
        "model": "qwen3.8-flash", "global_tokens": 3000000, "global_calls": 600,
        "cell_tokens": 50000, "cell_calls": 10, "development_attempts": 4,
        "preflight_tokens": 300000, "preflight_calls": 60,
        "implementation": source_snapshot(), "exposure": "existing_examples_exploratory",
        "repeat_interpretation": "repeated_model_runs_on_same_data_not_independent_datasets",
        "trust": "engineering_same_account", "general_research_ability_claimed": False}
    protocol["protocol_sha256"] = digest(protocol)
    path = out / "protocol.json"
    if path.exists() and read_json(path) != protocol:
        raise ProtocolError("已有评测协议不同，必须使用新的输出目录")
    write_json(path, protocol)
    return protocol


def validate_protocol(protocol):
    if digest({k: v for k, v in protocol.items() if k != "protocol_sha256"}) != protocol["protocol_sha256"]:
        raise ProtocolError("评测协议摘要变化")
    if protocol["implementation"] != source_snapshot():
        raise ProtocolError("评测实现已变，不能混合到冻结运行")
    for task in protocol["tasks"]:
        for name, sha in task["files"].items():
            if file_hash(ROOT / "examples" / task["id"] / name) != sha:
                raise ProtocolError("冻结任务或数据已变")


def selected_finalist(controller):
    control = controller.manifest["control_hypothesis_id"]
    rows = [r for r in controller.store.list("observation") if r["scope"] == "dev"
            and r["hypothesis_id"] != control]
    if not rows:
        return None
    direction = controller.manifest["metric"]["direction"]
    row = (min if direction == "min" else max)(rows, key=lambda r: r["value"])
    run = controller.store.get("run", row["run_id"])
    return {"hypothesis_id": row["hypothesis_id"], "design_id": row["design_id"],
            "observation_id": row["observation_id"], "value": row["value"],
            "config": controller.manifest["configs"][row["hypothesis_id"]],
            "revision_id": run.get("revision_id"), "artifact_sha256": row["artifact_sha256"]}


def run_cell(cell, protocol, out):
    started = time.monotonic()
    root = out / "cells" / cell["id"]
    root.mkdir(parents=True, exist_ok=True)
    path = root / "result.json"
    if path.exists():
        saved = read_json(path)
        if saved.get("protocol_sha256") != protocol["protocol_sha256"]:
            raise ProtocolError("评测单元身份变化")
        return saved
    scope = out.name + ":" + cell["id"]
    budget = ModelRequestBudget(GLOBAL_LEDGER, global_tokens=protocol["global_tokens"],
        global_calls=protocol["global_calls"], scope=scope,
        scope_tokens=protocol["cell_tokens"], scope_calls=protocol["cell_calls"])
    result = {**cell, "protocol_sha256": protocol["protocol_sha256"], "status": "running",
              "attempts": [], "finalist": None, "confirmation": None, "model_called": False}
    controller = None
    try:
        source = ROOT / "examples" / cell["task"]
        project = root / "project"
        if not (project / ".popper" / "state.db").exists():
            project.mkdir(parents=True, exist_ok=True)
            task = next(t for t in protocol["tasks"] if t["id"] == cell["task"])
            for name in task["files"]:
                target = project / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            spec = read_json(project / "experiment.json")
            spec["budget"] = min(protocol["development_attempts"], len(spec["candidates"]))
            write_json(project / "experiment.json", spec)
            initialize(project)
        run_dir = root / "research"
        ResearchController.initialize(project, run_dir, budget_cap=7)
        call = make_json_client(os.environ["POPPER_CHAT_BASE_URL"], protocol["model"],
                               root / "diagnostics", request_budget=budget)
        policy = object.__new__(DeepSeekResearchPolicy)
        policy._call, policy.model, policy.name = call, protocol["model"], "cohort_qwen_policy"
        controller = ResearchController(run_dir, policy=policy, harness=JSONAgentHarness(call, protocol["model"]))
        controller._require_sandbox_backend()
        manifest = controller.manifest
        control = manifest["control_hypothesis_id"]
        baseline = controller._execute_development(control,
            ActionProposal(RUN_EXPERIMENT, "冻结评测协议中的统一基线", control), False, True)
        result["baseline_dev"] = baseline["value"]
        if cell["strategy"] == "autonomous_tree":
            save_envelope(run_dir, build_envelope(manifest["study_id"], max_dev_runs=4,
                max_model_tokens=50000, max_wall_clock_minutes=60,
                code_files=controller.exp.state()["spec"]["code_files"],
                state_version=controller.store.get("study", manifest["study_id"])["state_version"],
                allowed_actions=("design_dev", "implement", "repair", "execute_dev"),
                contract_sha256=research_contract(manifest)))
            controller.run(sandboxed=True, autonomous_design=True, max_steps=4)
            progress = run_dir / "autonomous-progress.json"
            result["attempts"] = read_json(progress)["attempts"] if progress.is_file() else []
        else:
            candidate = manifest["candidate_hypothesis_ids"][0]
            parent = None
            limit = 2 if cell["strategy"] == "one_shot_repair" else 4
            journal = root / "attempts.json"
            attempts = read_json(journal) if journal.exists() else []
            # Interrupted attempts stay counted and are never silently repeated.
            for attempt in attempts:
                if attempt.get("revision_id"):
                    parent = attempt["revision_id"]
            for index in range(len(attempts), limit):
                entry = {"index": index, "status": "running", "revision_id": None}
                attempts.append(entry)
                write_json(journal, attempts)
                try:
                    implemented = controller.implement(candidate, parent_revision_id=parent,
                        search_node=cell["strategy"] == "linear_iteration")
                    parent = (implemented.get("revision") or {}).get("revision_id")
                    entry.update(status="succeeded", revision_id=parent,
                                 observation_id=implemented["observation"]["observation_id"])
                except ProtocolError as error:
                    if isinstance(error, RevisionExecutionError):
                        parent = error.revision_id
                    entry.update(status="failed", revision_id=parent, reason=str(error), error_type=type(error).__name__)
                write_json(journal, attempts)
                if cell["strategy"] == "one_shot_repair" and entry["status"] == "succeeded":
                    break
                if any(r["status"] == "unknown" for r in budget.snapshot()["attempts"] if r["scope"] == scope):
                    break
            result["attempts"] = attempts
        result["finalist"] = selected_finalist(controller)
        result["status"] = "development_complete" if result["finalist"] else "no_successful_candidate"
    except Exception as error:
        result.update(status="infrastructure_or_budget_blocked", reason=str(error), error_type=type(error).__name__)
        if controller:
            result["finalist"] = selected_finalist(controller)
    finally:
        if controller:
            controller.close()
        snapshot = budget.snapshot()
        result["usage"] = snapshot["scope"]
        result["model_called"] = result["usage"]["calls"] > 0
        result["unknown_usage"] = any(r["scope"] == scope and r["status"] == "unknown" for r in snapshot["attempts"])
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        # Errors may contain provider text; apply the project's credential redaction before persistence.
        from evaluation.run_mlebench_lite import redact
        clean = json.loads(redact(canonical(result)))
        key = os.environ.get("POPPER_API_KEY", "")
        if key:
            clean = json.loads(canonical(clean).replace(key, "[REDACTED]"))
        write_json(path, clean)
        budget.close()
    return clean


def confirm_cell(root, roster_path, key_path):
    """Called only in a separate, model-free process after every finalist was frozen."""
    roster = read_json(roster_path)
    frozen = roster["finalists"][root.name]
    result_path = root / "confirmation.json"
    key = load_private_key(key_path)
    if result_path.exists():
        receipt = read_json(result_path)
        verify_envelope(receipt, public_key_b64(key))
        return receipt
    if frozen is None:
        return None
    result = read_json(root / "result.json")
    if frozen != result["finalist"]:
        raise ProtocolError("确认前候选发生变化")
    controller = ResearchController(root / "research")
    try:
        state = controller._verify_inputs()
        observation = controller.store.get("observation", frozen["observation_id"])
        if observation["value"] != frozen["value"] or observation["artifact_sha256"] != frozen["artifact_sha256"]:
            raise ProtocolError("冻结开发证据发生变化")
        control = controller.manifest["control_hypothesis_id"]
        design = controller._current_design_id(control)
        baseline_revision = controller._registered_revision(state, design)
        candidate_revision = (controller.revisions.verify(frozen["revision_id"]) if frozen["revision_id"]
                              else controller._registered_revision(state, frozen["design_id"]))
        controller.exp.freeze(selected_config=frozen["config"])
        controller.exp.begin_confirmation(sandboxed=True)
        results, core_results = [], []
        for role, config, design_id, revision in (
            ("control", controller.manifest["configs"][control], design, baseline_revision),
            ("candidate", frozen["config"], frozen["design_id"], candidate_revision)):
            core_result = controller._evaluate_via_worker(config, design_id, revision, "test", sandboxed=True)
            core_results.append(core_result)
            evaluated = controller.evaluator.score_core_result(controller.exp.verify_inputs(), core_result)
            results.append({"role": role, "mean": evaluated["mean"], "std": evaluated["std"],
                "artifact_sha256": evaluated["artifact_sha256"], "service_sha256": evaluated["service_sha256"],
                "revision_id": revision["revision_id"]})
        delta = results[1]["mean"] - results[0]["mean"]
        if controller.manifest["metric"]["direction"] == "min":
            delta = -delta
        final = controller.exp.finalize_confirmation(
            core_results[0], core_results[1])
        payload = {"schema": "popper.cohort-confirmation/v2", "cell": root.name,
            "protocol_sha256": result["protocol_sha256"], "roster_sha256": digest(roster),
            "selection_sha256": digest(frozen), "results": results,
            "prediction_result": evaluate_prediction(controller.manifest["prediction_contract"], delta,
                repeat_unit=controller.manifest["repeat_unit"]),
            "confirmation_consumed_once": True, "trust": "engineering_same_account"}
        receipt = sign_payload(payload, key)
        verify_envelope(receipt, public_key_b64(key))
        write_json(result_path, receipt)
        return receipt
    finally:
        controller.close()


def run_cohort(out, *, workers=3):
    out = Path(out).resolve()
    protocol = read_json(out / "protocol.json")
    validate_protocol(protocol)
    if os.environ.get("POPPER_CHAT_MODEL") != protocol["model"] or not os.environ.get("POPPER_API_KEY"):
        raise ProtocolError("选定模型或凭据不可用")
    if not sandbox.available() or not sandbox.seal_read_available():
        raise ProtocolError("沙箱或保留集封读不可用，真实评测不能执行")
    preflight = ModelRequestBudget(GLOBAL_LEDGER, global_tokens=3000000, global_calls=600,
        scope=out.name + ":preflight", scope_tokens=300000, scope_calls=60)
    try:
        if not (out / "preflight.json").exists():
            call = make_json_client(os.environ["POPPER_CHAT_BASE_URL"], protocol["model"],
                out / "preflight-diagnostics", request_budget=preflight)
            reply = call('Return exactly one JSON object {"ok": true}.', {"purpose": "connection_check"})
            if reply.get("ok") is not True:
                raise ProtocolError("模型连接预检未通过")
            write_json(out / "preflight.json", {"ok": True, "usage": preflight.snapshot()["scope"]})
        cells = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run_cell, cell, protocol, out): cell for cell in protocol["cells"]}
            for future in as_completed(futures):
                result = future.result()
                cells.append(result)
                print(f"development {len(cells)}/54 {result['id']} {result['status']} calls={result['usage']['calls']}", flush=True)
        roster = {"protocol_sha256": protocol["protocol_sha256"],
                  "finalists": {r["id"]: r["finalist"] for r in sorted(cells, key=lambda r: r["id"])}}
        roster_path = out / "finalists.json"
        if roster_path.exists() and read_json(roster_path) != roster:
            raise ProtocolError("候选清单已冻结，不能替换")
        write_json(roster_path, roster)
        private = out / "private" / "confirmation.pem"
        key = load_private_key(private, create=True)
        public = public_key_b64(key)
        for index, result in enumerate(sorted(cells, key=lambda r: r["id"])):
            if result["finalist"] is None:
                result["confirmation_status"] = "not_evaluated"
                continue
            root = out / "cells" / result["id"]
            completed = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m",
                "evaluation.run_research_benchmark", "--confirm-cell", str(root),
                "--roster", str(roster_path), "--signing-key", str(private)],
                cwd=ROOT, capture_output=True, timeout=600, check=False)
            if completed.returncode == 0 and (root / "confirmation.json").exists():
                receipt = read_json(root / "confirmation.json")
                result["confirmation"] = verify_envelope(receipt, public)
                result["confirmation_status"] = "succeeded"
            else:
                result["confirmation_status"] = "infrastructure_failed"
                result["confirmation_error"] = completed.stderr.decode("utf-8", errors="replace")[-3000:]
            print(f"confirmation {index + 1}/54 {result['id']} {result['confirmation_status']}", flush=True)
        summary = {"schema": "popper.research-cohort-report/v2", "protocol": protocol,
            "cells": sorted(cells, key=lambda r: r["id"]), "usage": preflight.snapshot(),
            "public_key": public, "roster_sha256": digest(roster),
            "planned_cells": 54, "development_finalists": sum(r["finalist"] is not None for r in cells),
            "confirmations_succeeded": sum(r.get("confirmation_status") == "succeeded" for r in cells),
            "status": "completed", "ability_gain_claimed": False,
            "exposure": protocol["exposure"], "trust": "engineering_same_account"}
        comparisons = []
        for task in TASKS:
            for repeat in range(3):
                group = {r["strategy"]: r for r in cells if r["task"] == task and r["repeat"] == repeat}
                a, b = group["autonomous_tree"], group["linear_iteration"]
                a_result, b_result = a.get("confirmation"), b.get("confirmation")
                row = {"task": task, "repeat": repeat, "comparable": bool(a_result and b_result),
                       "autonomous_minus_linear_oriented_effect": None}
                if row["comparable"]:
                    row["autonomous_minus_linear_oriented_effect"] = (
                        a_result["prediction_result"]["oriented_effect"] - b_result["prediction_result"]["oriented_effect"])
                comparisons.append(row)
        summary["autonomous_vs_linear"] = comparisons
        summary["strategy_counts"] = {strategy: {
            "planned": 18, "finalists": sum(r["strategy"] == strategy and r["finalist"] is not None for r in cells),
            "confirmed": sum(r["strategy"] == strategy and r.get("confirmation_status") == "succeeded" for r in cells),
            "calls": sum(r["usage"]["calls"] for r in cells if r["strategy"] == strategy),
            "accounted_tokens": sum(r["usage"]["accounted_tokens"] for r in cells if r["strategy"] == strategy)}
            for strategy in STRATEGIES}
        if summary["confirmations_succeeded"] != 54:
            summary["status"] = "completed_with_unconfirmed_or_failed_cells"
        write_json(out / "report.json", summary)
        target = ROOT / "evaluation/runs/overhaul-20261007/report.json"
        write_json(target, summary)
        return summary
    finally:
        preflight.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--confirm-cell", type=Path)
    parser.add_argument("--roster", type=Path)
    parser.add_argument("--signing-key", type=Path)
    args = parser.parse_args()
    if args.confirm_cell:
        confirm_cell(args.confirm_cell, args.roster, args.signing_key)
    elif args.freeze:
        freeze_protocol(args.out)
    elif args.run:
        run_cohort(args.out, workers=args.workers)
    else:
        parser.error("选择 --freeze、--run 或 --confirm-cell")


if __name__ == "__main__":
    main()
