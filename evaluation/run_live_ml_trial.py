"""One bounded model-driven ML development round; never consumes holdout."""
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
from popper.research.execution import EXECUTED_COVERAGE
from popper.research.models import DeepSeekResearchPolicy


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    trial = ROOT / "evaluation/runs" / f"live-ml-{stamp}"
    project, run_dir = trial / "project", trial / "research"
    source = ROOT / "examples/breast-cancer-wisconsin"
    project.mkdir(parents=True)
    for name in ("experiment.json", "model.py", "prepare_data.py"):
        shutil.copyfile(source / name, project / name)
    # Freeze a real implementation task before the model sees it. The original
    # showcase already implements every candidate, so it cannot test code writing.
    model_path = project / "model.py"
    source_code = model_path.read_text(encoding="utf-8")
    start = source_code.index("def estimator(")
    end = source_code.index("\n\ndef main():", start)
    baseline_only = '''def estimator(config, seed):
    if config == {"model": "gaussian_nb"}:
        return GaussianNB()
    raise NotImplementedError("Implement the registered candidate using its frozen configuration")
'''
    model_path.write_text(source_code[:start] + baseline_only + source_code[end:], encoding="utf-8")
    runpy.run_path(str(project / "prepare_data.py"))["prepare"](project)
    initialize(project)
    policy = DeepSeekResearchPolicy("https://api.deepseek.com", "deepseek-v4-flash",
                                    diagnostics_dir=trial / "model-diagnostics")
    original_call = policy._call
    calls = 0

    def bounded_call(system, payload):
        nonlocal calls
        if calls >= 4:
            raise ProtocolError("本次验收的 4 次逻辑模型调用额度已耗尽")
        calls += 1
        print(f"Model call {calls}/4", flush=True)
        return original_call(system, payload)

    policy._call = bounded_call
    controller = None
    result = None
    error_type = None
    try:
        ResearchController.initialize(project, run_dir, policy=policy)
        controller = ResearchController(run_dir, policy=policy)
        result = controller.run(sandboxed=True, autonomous_code=True, max_steps=1)
    except Exception as error:
        error_type = type(error).__name__
        print(f"Trial stopped: {error_type}", flush=True)
        if controller is not None:
            result = controller.status()
    finally:
        if controller is not None:
            controller.close()
    diagnostics = [read_json(p) for p in sorted((trial / "model-diagnostics").glob("*.json"))]
    receipts = [read_json(p) for p in sorted((run_dir / "jobs").glob("JOB-*/receipt.json"))]
    revisions = list((run_dir / "revisions").glob("REV-*/revision.json"))
    entrypoint_changed = any(
        (p.parent / "code" / read_json(p)["entrypoint"]).read_text(encoding="utf-8")
        != (project / read_json(p)["entrypoint"]).read_text(encoding="utf-8")
        for p in revisions)
    # 冻结基线也经 `_evaluate_registered_via_worker` 逐 seed 跑（identity 是
    # registered_implementation_dev），其回执按设计不带 execution_gate——只有真正改过代码的
    # 候选才 require_edit_coverage。所以「改动语句真的执行了」这条主张只对带 gate 的那批回执
    # 成立：按项目的 seeds 数逐条要求，而不是对全部 succeeded 回执一刀切。原先的
    # `sum(succeeded) == 3` 是基线还不走 worker 时留下的期望值，会把正常的 6 条回执判失败。
    seeds = read_json(project / "experiment.json")["seeds"]
    generated = [r for r in receipts if "execution_gate" in r]
    ok = bool(error_type is None and result and result["integrity"]["ok"]
              and len(result["observations"]) >= 2 and receipts
              and all(r["status"] == "succeeded" for r in receipts)
              and len(generated) == len(seeds)
              and all(r["execution_gate"]["coverage"] in EXECUTED_COVERAGE
                      for r in generated))
    summary = {"classification": "model_driven_registered_ml_development_trial",
               "task_setup": "baseline_only_scaffold_before_initialization",
               "claim_limit": "模型生成假设、选择登记候选并修改代码；计分及实验后阈值决策为确定性内核。单轮公开数据开发验证，不证明创新或通用自主科研能力。",
               "passed": ok, "error_type": error_type, "status": result,
               "passed_scope": "executed_changed_statements",
               "generated_changes_executed": ok,
               "entrypoint_text_changed": entrypoint_changed,
               "autonomous_implementation_verified": False,
               "implementation_verification_note": "逐 seed 至少一处检测到的改动语句实际执行（coverage 为 complete/partial）；同进程轨迹并非对抗证明，也不证明改变影响预测或科学机制成立。",
               "requested_model": policy.model, "logical_model_calls": calls,
               "http_attempts": len(diagnostics),
               "usage": [d.get("usage") for d in diagnostics],
               "worker_receipts": receipts,
               "limits": {"logical_calls": 4, "http_attempts": 8,
                          "development_rounds": 1, "auto_repairs": 1,
                          "holdout": "not_requested"}}
    write_json(trial / "trial-summary.json", summary)
    print(json.dumps({"passed": ok, "summary": str(trial / "trial-summary.json"),
                      "phase": result["phase"] if result else None}, ensure_ascii=False), flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
