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
        ok = (result["integrity"]["ok"]
              and len(result["observations"]) == 2
              and result["code_revisions"] == 2
              and sum(r["status"] == "implementation_failed" for r in receipts) == 1
              and sum(r["status"] == "succeeded" for r in receipts) == 3
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
