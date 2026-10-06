"""用研究内核跑三道 MLE-bench Lite 小题，并各自最多确认一次。

模型 key 只从已经存在的 POPPER_API_KEY 读取。确认是单独的 controller.confirm，
不写入自治包络。合计 60 万 token、每题 20 万，超过即停止且不再调用模型。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from popper.core import ProtocolError, initialize
from popper.research.controller import ResearchController
from popper.research.models import DeepSeekResearchPolicy
from popper.sandbox import available as sandbox_available

from evaluation.mlebench_lite import (PER_TASK_CAP, TASKS, TOKEN_CAP, data_root,
                                      load_task, materialize, task_ready)

_TOKEN_PREFIX = "KG" + "AT_"
_SECRET = re.compile(re.escape(_TOKEN_PREFIX) + r"[A-Za-z0-9]+|sk-[A-Za-z0-9_\-]+|Bearer\s+\S+", re.I)
REPORT_PATH = Path(__file__).resolve().parent / "runs" / "mlebench-lite" / "report.json"


class TokenBudget:
    """跨题累计的付费调用额度。超限后拒绝下一次模型调用。"""

    def __init__(self, total_cap=TOKEN_CAP, per_task_cap=PER_TASK_CAP):
        self.total_cap = total_cap
        self.per_task_cap = per_task_cap
        self.total_used = 0
        self.calls = 0
        self.task_used = 0
        self._base = 0
        self.stopped = False
        self.stop_reason = None
        self.diagnostics_dir = None

    def begin_task(self):
        self._base = self.total_used
        self.task_used = 0
        self.diagnostics_dir = None
        if self.total_used >= self.total_cap:
            self._stop("合计 token 已达到上限，停止后续题目")

    def wrap(self, call):
        budget = self

        def wrapped(system, payload):
            if (budget.stopped or budget.total_used >= budget.total_cap
                    or budget.task_used >= budget.per_task_cap):
                budget._stop("token 上限已到，停止调用模型")
            budget.calls += 1
            error = None
            result = None
            try:
                result = call(system, payload)
            except Exception as exc:
                error = exc
            finally:
                spent = getattr(call, "tokens_used", None)
                spent = spent() if callable(spent) else 0
                budget.task_used = max(int(spent or 0), _diagnostic_tokens(budget.diagnostics_dir))
                budget.total_used = budget._base + budget.task_used
                if budget.task_used > budget.per_task_cap or budget.total_used > budget.total_cap:
                    budget.stopped = True
                    budget.stop_reason = "token 上限已超过，停止调用模型"
            if budget.stopped and budget.stop_reason:
                raise ProtocolError(budget.stop_reason)
            if error is not None:
                raise error
            return result

        wrapped.tokens_used = lambda: budget.task_used
        return wrapped

    def _stop(self, reason):
        self.stopped = True
        self.stop_reason = reason
        raise ProtocolError(reason)


def _diagnostic_tokens(directory):
    if directory is None or not Path(directory).is_dir():
        return 0
    total = 0
    for path in Path(directory).glob("*.json"):
        try:
            usage = json.loads(path.read_text(encoding="utf-8")).get("usage") or {}
        except (OSError, json.JSONDecodeError):
            continue
        total_tokens = usage.get("total_tokens")
        if type(total_tokens) is not int:
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if type(prompt) is int and type(completion) is int:
                total_tokens = prompt + completion
        if type(total_tokens) is int and total_tokens > 0:
            total += total_tokens
    return total


def _paid_calls(run_dir, logical_calls):
    directory = Path(run_dir) / "model-diagnostics"
    http = len(list(directory.glob("*.json"))) if directory.is_dir() else 0
    return max(int(logical_calls), http)


def redact(text):
    return _SECRET.sub("[REDACTED]", str(text))


def assert_holdout_isolated(manifest):
    holdout = set(manifest["holdout_ids"])
    if len(manifest["holdout_ids"]) != len(holdout):
        raise ValueError("确认集 id 重复")
    if holdout & set(manifest["train_ids"]) or holdout & set(manifest["dev_ids"]):
        raise ValueError("确认集 id 进入了 train 或 dev")


def holdout_ids_absent_from_files(project, manifest):
    holdout = set(manifest["holdout_ids"])
    for name in ("train.json", "dev.json"):
        rows = json.loads((project / name).read_text(encoding="utf-8"))
        found = holdout & {row["id"] for row in rows}
        if found:
            raise ValueError(f"确认集 id 出现在 {name}")


def assert_confirm_outside_envelope(run_dir):
    path = Path(run_dir) / "envelope.json"
    if not path.is_file():
        return
    envelope = json.loads(path.read_text(encoding="utf-8"))
    actions = " ".join(str(item) for item in envelope.get("allowed_actions", []))
    if "confirm" in actions:
        raise ProtocolError("confirm 进入了自治包络")


def file_stat(path):
    path = Path(path)
    size = path.stat().st_size
    if path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {"bytes": size, "records": len(payload)}
    with path.open(encoding="utf-8", errors="replace") as handle:
        lines = sum(1 for _ in handle)
    return {"bytes": size, "lines": lines}


def build_run_report(payload):
    encoded = json.dumps(payload, ensure_ascii=False)
    if _SECRET.search(encoded):
        raise RuntimeError("报告含有密钥片段，拒绝写入")
    return payload


def _execution_mode():
    if sandbox_available():
        return {"sandboxed": True, "trusted_local": False, "execution": "sandbox"}
    return {"sandboxed": False, "trusted_local": True, "execution": "trusted_local"}


def _blank_task(task_id, reason, tokens_used=0, paid_model_calls=0):
    return {
        "task_id": task_id,
        "completed": False,
        "confirmation_calls": 0,
        "confirmation_consumed_once": False,
        "tokens_used": tokens_used,
        "paid_model_calls": paid_model_calls,
        "reason": redact(reason),
    }


def execute_research(project, run_dir, *, policy, budget, sandboxed, trusted_local):
    """跑开发循环；只有到达确认门时才调用一次 confirm。"""
    project, run_dir = Path(project), Path(run_dir)
    initialize(project)
    controller = None
    confirm_calls = 0
    phase = None
    core_phase = None
    error = None
    try:
        ResearchController.initialize(project, run_dir, policy=policy)
        controller = ResearchController(run_dir, policy=policy)
        status = controller.run(trusted_local=trusted_local, sandboxed=sandboxed,
                                auto_confirm=False, autonomous_code=False, max_steps=2)
        phase = status.get("phase")
        if phase == "ready_for_confirmation" and not budget.stopped:
            confirm_calls = 1
            status = controller.confirm(trusted_local=trusted_local, sandboxed=sandboxed)
            phase = status.get("phase")
        core_phase = controller.exp.state()["phase"]
        assert_confirm_outside_envelope(run_dir)
    except Exception as exc:
        error = exc
        if controller is not None:
            try:
                core_phase = controller.exp.state()["phase"]
                phase = controller.status().get("phase")
            except Exception:
                pass
    finally:
        if controller is not None:
            controller.close()
    consumed_once = confirm_calls == 1 and core_phase == "completed"
    return {
        "phase": phase,
        "core_phase": core_phase,
        "confirmation_calls": confirm_calls,
        "confirmation_consumed_once": consumed_once,
        "completed": consumed_once and error is None and phase == "concluded",
        "error": None if error is None else redact(f"{type(error).__name__}: {error}"),
    }


def _policy(run_dir, budget):
    base_url = os.environ.get("POPPER_CHAT_BASE_URL")
    model = os.environ.get("POPPER_CHAT_MODEL")
    if not base_url or not model or not os.environ.get("POPPER_API_KEY"):
        raise ProtocolError("缺少 POPPER_API_KEY、POPPER_CHAT_BASE_URL 或 POPPER_CHAT_MODEL")
    policy = DeepSeekResearchPolicy(base_url, model, run_dir / "model-diagnostics")
    policy._call = budget.wrap(policy._call)
    return policy


def run_task(task_id, raw_root, work_root, budget, execution):
    before_calls = budget.calls
    before_tokens = budget.total_used
    try:
        budget.begin_task()
    except ProtocolError as exc:
        return _blank_task(task_id, str(exc), 0, 0)
    project_root = Path(work_root) / task_id
    run_dir = project_root / "research"
    try:
        rows = load_task(task_id, Path(raw_root) / task_id)
        manifest = materialize(task_id, rows, project_root)
        assert_holdout_isolated(manifest)
        holdout_ids_absent_from_files(project_root / "project", manifest)
        run_dir.mkdir(parents=True, exist_ok=True)
        budget.diagnostics_dir = run_dir / "model-diagnostics"
        policy = _policy(run_dir, budget)
        outcome = execute_research(
            project_root / "project", run_dir, policy=policy, budget=budget,
            sandboxed=execution["sandboxed"], trusted_local=execution["trusted_local"])
    except Exception as exc:
        outcome = {"completed": False, "confirmation_calls": 0,
                   "confirmation_consumed_once": False, "phase": None,
                   "error": redact(f"{type(exc).__name__}: {exc}")}
    tokens_used = budget.total_used - before_tokens
    paid = _paid_calls(run_dir, budget.calls - before_calls)
    reason = outcome.get("error") or budget.stop_reason
    if outcome["completed"]:
        reason = None
    elif reason is None:
        reason = f"研究停在 {outcome.get('phase') or outcome.get('core_phase') or 'unknown'}，确认未完成"
    return {
        "task_id": task_id,
        "completed": bool(outcome["completed"]),
        "confirmation_calls": outcome["confirmation_calls"],
        "confirmation_consumed_once": bool(outcome["confirmation_consumed_once"]),
        "phase": outcome.get("phase"),
        "core_phase": outcome.get("core_phase"),
        "tokens_used": tokens_used,
        "paid_model_calls": paid,
        "reason": None if reason is None else redact(reason),
    }


def assemble_report(task_results, *, files, execution, budget, status, reason):
    calls = [item["confirmation_calls"] for item in task_results]
    report = {
        "schema": "mlebench-lite-run-v1",
        "tasks": list(TASKS),
        "task_results": task_results,
        "files": files,
        "execution": execution,
        "budget_confirmed_tokens": TOKEN_CAP,
        "per_task_token_cap": PER_TASK_CAP,
        "paid_model_calls": sum(item["paid_model_calls"] for item in task_results),
        "tokens_used": budget.total_used,
        "confirmation_calls": calls,
        "confirmation_consumed_once": all(item["confirmation_consumed_once"] for item in task_results)
        and all(count == 1 for count in calls),
        "status": status,
        "reason": None if reason is None else redact(reason),
    }
    return build_run_report(report)


def write_run_report(report, path=REPORT_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main():
    root = data_root()
    missing = [task_id for task_id in TASKS if not task_ready(root, task_id)]
    budget = TokenBudget()
    execution = _execution_mode()
    files = []
    if missing:
        report = assemble_report(
            [_blank_task(task_id, "缺少原始训练文件") for task_id in TASKS],
            files=files, execution=execution["execution"], budget=budget,
            status="blocked_missing_raw_data",
            reason="原始训练文件未齐，未调用模型。")
        write_run_report(report)
        print(report["status"])
        return 2
    for task_id in TASKS:
        name = "train.json" if task_id == "random-acts-of-pizza" else "train.csv"
        files.append({"task_id": task_id, **file_stat(root / task_id / name)})
    work = REPORT_PATH.parent / "work"
    results = []
    for task_id in TASKS:
        if budget.stopped or budget.total_used >= TOKEN_CAP:
            results.append(_blank_task(task_id, "合计 token 上限已到，这一题没有开始"))
            continue
        print(f"run {task_id} tokens_used={budget.total_used}", flush=True)
        results.append(run_task(task_id, root, work, budget, execution))
        print(f"done {task_id} completed={results[-1]['completed']} "
              f"tokens_used={budget.total_used} confirmation_calls={results[-1]['confirmation_calls']}",
              flush=True)
        if budget.stopped:
            continue
    status = "completed" if all(item["completed"] for item in results) else "incomplete"
    reason = None if status == "completed" else "；".join(
        f"{item['task_id']}: {item['reason']}" for item in results if not item["completed"])
    report = assemble_report(results, files=files, execution=execution["execution"],
                             budget=budget, status=status, reason=reason)
    write_run_report(report)
    print(json.dumps({"status": report["status"], "tokens_used": report["tokens_used"],
                      "paid_model_calls": report["paid_model_calls"],
                      "confirmation_consumed_once": report["confirmation_consumed_once"]},
                     ensure_ascii=False), flush=True)
    return 0 if status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
