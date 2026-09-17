"""运行单个盲测 cell（task × comparator × run_index），支持 external/local/none 三确认模式。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popper.core import Experiment, ProtocolError, file_hash, initialize, read_json, write_json
from popper.research.controller import ResearchController
from popper.research.models import DeepSeekResearchPolicy, EvidenceDrivenPolicy
from popper.research.confirmation_contracts import load_private_key, public_key_b64
from popper.research.confirmation_service import HoldoutService
from popper.research.confirmation_runner import run_confirmation_bundle

from evaluation.blind_pilot.policies import (FixedPlanPolicy, make_call_limiter,
                                             scripted_plan)
from evaluation.blind_pilot.audit import audited_summary


def _policy_for_comparator(comparator, project, run_dir, spec, *, plan_mode="scripted",
                           model_url=None, model_name=None, diagnostics_dir=None):
    """按对照构造策略。fixed_plan 实验前用 deterministic scripted_plan 固定计划。"""
    if comparator == "fixed_registered_search":
        return EvidenceDrivenPolicy()
    if comparator == "same_model_same_tools_fixed_plan":
        plan = scripted_plan(spec)
        return FixedPlanPolicy(plan, plan_source=plan_mode)
    if comparator == "adaptive_research_controller":
        policy = DeepSeekResearchPolicy(model_url, model_name, diagnostics_dir=diagnostics_dir)
        policy._call_counter = make_call_limiter(policy, logical_calls=32, http_attempts=64)
        return policy
    raise ProtocolError(f"未知对照: {comparator}")


def _new_cell_dir(trial_root, cell):
    return trial_root / f"{cell['task_id']}-{cell['comparator']}-run{cell['run_index']}"


def run_one_cell(cell, *, trial_root, confirm_mode="local", model_url=None,
                 model_name="deepseek-flash", gpu_count=0):
    """执行一个 cell 并写 cell-summary.json。返回 summary dict。

    confirm_mode: external（官方，HoldoutService+签名回执）| local（冒烟，消费本地 test）|
    none（冒烟，到 ready_for_confirmation 即停）。

    external 服务接受生成代码 revision，也接受经真实开发执行绑定的注册实现。
    固定对照仍在工程盲测中显式使用 local 确认，并保留 requested_confirm_mode。
    """
    task_spec = _task_spec(cell["task_id"])
    cell_dir = _new_cell_dir(trial_root, cell)
    cell_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    requested_confirm_mode = confirm_mode
    summary = {
        "schema_version": "1.0", "stamp": stamp, "cell": cell,
        "task_id": cell["task_id"], "family": task_spec.family,
        "condition": task_spec.condition, "evaluator_id": task_spec.evaluator_id,
        "comparator": cell["comparator"], "run_index": cell["run_index"],
        "confirm_mode": confirm_mode, "requested_confirm_mode": requested_confirm_mode,
        "plan_mode": "scripted", "model": None,
        "status": "running", "error_type": None, "error_stage": None,
        "valid_loop": False, "conclusion_matched": None,
        "logical_model_calls": 0, "elapsed_seconds": None,
        "test_exposure": {"provided_to_worker": False, "consumed": False},
    }
    started = time.monotonic()
    controller = None
    try:
        # 1. build 任务模板（缓存到 trial_root/tasks/<id>）
        template = trial_root / "tasks" / cell["task_id"]
        public, gold = template / "public", template / "gold"
        if not public.exists():
            public.mkdir(parents=True)
            task_spec.build(public, gold)
        # 2. 复制 public/ -> cell/project 并初始化
        project = cell_dir / "project"
        shutil.copytree(public, project)
        popper = project / ".popper"
        if popper.exists():
            shutil.rmtree(popper)
        state = initialize(project)
        spec = state["spec"]
        run_dir = cell_dir / "research"
        policy = _policy_for_comparator(
            cell["comparator"], project, run_dir, spec,
            plan_mode=summary["plan_mode"], model_url=model_url,
            model_name=model_name, diagnostics_dir=cell_dir / "model-diagnostics")
        summary["policy_source"] = getattr(policy, "name", type(policy).__name__)
        if cell["comparator"] == "adaptive_research_controller":
            summary["model"] = model_name

        # 3. external 模式：建 HoldoutService + 注册 + runner key
        use_external = confirm_mode == "external"
        enrollment = None
        if use_external:
            service = HoldoutService(cell_dir / "private-service")
            runner_key = load_private_key(cell_dir / "runner.pem", create=True)
            contract = service.register(
                dataset_id=cell["task_id"], dataset_version="1",
                evaluation_group=f"{cell['task_id']}-r{cell['run_index']}",
                train_path=project / spec["train"], dev_path=project / spec["dev"],
                holdout_path=project / spec["test"], evaluator_id=state["evaluator_id"],
                seeds=spec["seeds"], min_effect=spec["min_improvement"],
                runtime_id="blind-pilot", runner_public_key=public_key_b64(runner_key),
                allowed_backend="windows_low_integrity_engineering",
                analysis_slices=spec.get("analysis_slices", []))
            summary["contract_id"] = contract["payload"]["contract_id"]
            service.close()
            enrollment = {"contract": contract, "public_key": service.public_key,
                          "runner_key_path": str(runner_key), "service_dir": str(cell_dir / "private-service")}

        # 4. initialize controller（external 传 contract）
        if enrollment:
            ResearchController.initialize(
                project, run_dir, policy=policy,
                confirmation_contract=enrollment["contract"],
                confirmation_public_key=enrollment["public_key"])
        else:
            ResearchController.initialize(project, run_dir, policy=policy)
        controller = ResearchController(run_dir, policy=policy)

        # 5. run 至开发循环终止 / 待确认
        if cell["comparator"] == "adaptive_research_controller":
            result = controller.run(sandboxed=True, autonomous_code=True, max_steps=8,
                                    auto_confirm=False)
        else:
            # 固定搜索/固定计划：确定性开发搜索
            result = controller.run(sandboxed=False, trusted_local=True, max_steps=8,
                                    auto_confirm=False)
        phase = result["phase"]

        # 6. 确认环节
        if confirm_mode == "local" and phase == "ready_for_confirmation":
            result = controller.confirm(sandboxed=False, trusted_local=True)
            summary["test_exposure"]["consumed"] = True
        elif use_external and phase in {"ready_for_confirmation", "concluded", "budget_exhausted"}:
            prepared = controller.prepare_external_confirmation()
            bundle_dir = prepared["bundle_dir"]
            submission = prepared["submission"]
            # 服务端已关闭，重开；用独立 runner 输出目录
            service = HoldoutService(cell_dir / "private-service")
            runner_key = load_private_key(cell_dir / "runner.pem")
            ticket = service.begin(submission)
            features = service.features(ticket)
            # Keep this path short: Windows worker coverage adds
            # jobs/JOB-*/workspace/_popper_execution_probe.py and otherwise can
            # hit the legacy 260-character boundary before candidate execution.
            runner_out = cell_dir / "xr"
            runner_result = run_confirmation_bundle(
                bundle_dir, ticket, features, service.public_key, runner_key, runner_out)
            # runner 回执由 runner 私钥签名；必须再由 HoldoutService 评分并签发
            # service-signed holdout_result，controller 才接受该确认结论。
            signed_result = service.complete(
                ticket, runner_result["runner_receipt"], runner_result["predictions"])
            result = controller.accept_external_confirmation(signed_result)
            service.close()
            summary["test_exposure"]["consumed"] = True

        status = controller.status() if result else None
        summary.update({
            "status": "completed",
            "phase": status["phase"] if status else phase,
            "integrity_ok": bool(status and status["integrity"]["ok"]),
            "candidate_states": [c["status"] for c in status["candidates"]] if status else [],
            "observations": len(status["observations"]) if status else 0,
            "budget": status["budget"] if status else None,
            "logical_model_calls": getattr(policy, "_call_counter", {}).get("logical", 0),
            "execution_coverage": _execution_coverage(status),
        })
        summary["test_exposure"]["provided_to_worker"] = False
        # Persist a provisional summary because the post-hoc auditor reads terminal phase.
        _write_cell_summary(cell_dir, summary)
        summary = audited_summary(cell_dir, gold, summary)
        summary["valid_loop"] = _is_valid_loop(summary)
        return summary
    except Exception as error:
        summary["status"] = "failed"
        summary["error_type"] = type(error).__name__
        summary["error_stage"] = _current_stage(summary)
        summary["error_message"] = str(error)[:500]
        return summary
    finally:
        if controller is not None:
            controller.close()
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_cell_summary(cell_dir, summary)


def _current_stage(summary):
    # 简化：返回 error 前所在阶段逻辑省略，保持摘要自包含
    return "controller_run"


def _execution_coverage(status):
    """分级覆盖证据：只有实际命中检测到的改动语句（complete/partial）才算观测到执行。

    内核的 `passed` 还会放行 `ambiguous`（无可观测改动，例如只删语句或只改条件/注释），
    因此这里不能直接复用它来声称「改动代码被执行」。
    """
    if not status:
        return None
    return all(g.get("coverage") in {"complete", "partial"}
               for g in status.get("execution_gates", []))


def _is_valid_loop(summary):
    """有效闭环：integrity ok + 终止 phase 正确 + （确认已了结或合法停止）。"""
    if not summary.get("integrity_ok") or not summary.get("evidence_replay", {}).get("ok"):
        return False
    phase = summary.get("phase")
    if phase not in {"concluded", "budget_exhausted"}:
        # ready_for_confirmation 只在已消费确认后算；否则不算完整闭环
        if phase == "ready_for_confirmation" and summary["test_exposure"]["consumed"]:
            return True
        return False
    return True


def _task_spec(task_id):
    from evaluation.blind_pilot.tasks.registry import task_spec
    return task_spec(task_id)


def _write_cell_summary(cell_dir, summary):
    write_json(cell_dir / "cell-summary.json", summary)
