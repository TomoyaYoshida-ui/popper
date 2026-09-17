"""A controlled real-ML trial of model reflection after a fixed first experiment."""
from __future__ import annotations

import json
import runpy
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from popper.core import ProtocolError, initialize, read_json, write_json
from popper.research.controller import ResearchController
from popper.research.actions import RUN_EXPERIMENT, ActionProposal
from popper.research.models import DeepSeekResearchPolicy


class ReflectionTrialPolicy(DeepSeekResearchPolicy):
    def choose(self, context):
        # The first intervention is part of the controlled test setup. All
        # subsequent controls must come from the model's evidence-bound reflection.
        if context["reflections"]:
            raise ProtocolError("验收中禁止绕过持久化反思另选候选")
        candidate = next(c for c in context["candidates"] if c["config"]["model"] == "random_forest")
        return ActionProposal(RUN_EXPERIMENT, "验收预先固定首个随机森林实验；后续动作由模型反思决定。",
                              candidate["hypothesis_id"], source="controlled_trial_setup")


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    trial = ROOT / "evaluation/runs" / f"reflection-ml-{stamp}"
    project, run_dir = trial / "project", trial / "research"
    source = ROOT / "examples/breast-cancer-wisconsin"
    project.mkdir(parents=True)
    for name in ("experiment.json", "model.py", "prepare_data.py"):
        shutil.copyfile(source / name, project / name)
    runpy.run_path(str(project / "prepare_data.py"))["prepare"](project)
    initialize(project)
    policy = ReflectionTrialPolicy("https://api.deepseek.com", "deepseek-v4-flash",
                                   diagnostics_dir=trial / "model-diagnostics")
    original_call, calls = policy._call, 0

    def bounded_call(system, payload):
        nonlocal calls
        if calls >= 4:
            raise ProtocolError("反思验收的 4 次逻辑模型调用额度已耗尽")
        calls += 1
        print(f"Model call {calls}/4", flush=True)
        return original_call(system, payload)

    policy._call = bounded_call
    controller, status, error_type = None, None, None
    try:
        ResearchController.initialize(project, run_dir, policy=policy)
        controller = ResearchController(run_dir, policy=policy)
        status = controller.run(sandboxed=True, max_steps=2)
    except Exception as error:
        error_type = type(error).__name__
        print(f"Trial stopped: {error_type}", flush=True)
        if controller is not None:
            status = controller.status()
    finally:
        if controller is not None:
            controller.close()
    reflections = status["reflections"] if status else []
    revised_controls = [r["next_design_id"] for r in reflections
                        if r["action"] == "add_control" and r["revision"]]
    executed_revised = [o for o in status["observations"]
                        if o["design_id"] in revised_controls] if status else []
    ok = bool(error_type is None and status and status["integrity"]["ok"]
              and reflections and (executed_revised or reflections[-1]["action"] == "stop"))
    diagnostics = [read_json(p) for p in (trial / "model-diagnostics").glob("*.json")]
    summary = {"classification": "controlled_model_reflection_real_ml_trial",
               "claim_limit": "首个实验由验收固定；模型依据真实开发证据修订并执行后续对照或停止。已知分类方法比较，不证明创新或跨任务科研能力。",
               "passed": ok, "error_type": error_type, "status": status,
               "logical_model_calls": calls, "http_attempts": len(diagnostics),
               "usage": [d.get("usage") for d in diagnostics],
               "executed_revised_controls": [o["observation_id"] for o in executed_revised],
               "limits": {"logical_model_calls": 4, "http_attempts": 8,
                          "development_rounds": 2, "holdout": "not_requested"}}
    write_json(trial / "trial-summary.json", summary)
    print(json.dumps({"passed": ok, "summary": str(trial / "trial-summary.json"),
                      "phase": status["phase"] if status else None}, ensure_ascii=False), flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
