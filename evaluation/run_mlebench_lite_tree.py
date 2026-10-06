"""用自治实验树挑战已经确认过的线性候选。

只比较开发集。线性候选相对常数基线的开发集改善，就是这次搜索的门槛。
确认集已经消费过一次，这里不调用 confirm。合计 token 仍不得超过 60 万。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from popper.core import ProtocolError, initialize
from popper.research.controller import ResearchController

from evaluation.mlebench_lite import TASKS, TOKEN_CAP, data_root, load_task, materialize
from evaluation.run_mlebench_lite import (
    REPORT_PATH, TokenBudget, _paid_calls, _policy, assert_holdout_isolated,
    build_run_report, holdout_ids_absent_from_files, redact, sandbox_available)

TREE_ROOT = REPORT_PATH.parent / "tree"
TREE_REPORT = REPORT_PATH.parent / "tree-report.json"
MAX_STEPS = 8


def oriented_delta(baseline, candidate, direction):
    if direction == "min":
        return baseline - candidate
    if direction == "max":
        return candidate - baseline
    raise ProtocolError(f"未知指标方向: {direction}")


def margin_from_rows(rows, control_id, candidate_id, direction):
    """线性候选必须恰好有一条开发集观察，门槛就是它相对常数基线的改善。"""
    def one(hypothesis_id, label):
        matched = [row["value"] for row in rows
                   if row.get("hypothesis_id") == hypothesis_id and row.get("scope") == "dev"]
        if len(matched) != 1:
            raise ProtocolError(f"{label} 的开发集观察必须恰好一条，实际 {len(matched)} 条")
        return matched[0]

    baseline = one(control_id, "常数基线")
    linear = one(candidate_id, "线性候选")
    delta = oriented_delta(baseline, linear, direction)
    if type(delta) is bool or not isinstance(delta, (int, float)) or delta <= 0:
        raise ProtocolError("线性候选没有超过常数基线，不能作为实验树门槛")
    return {"baseline": baseline, "linear": linear, "delta": delta, "direction": direction}


def load_prior_margin(study_dir):
    study_dir = Path(study_dir)
    manifest = json.loads((study_dir / "research" / "research.json").read_text(encoding="utf-8"))
    direction = manifest["metric"]["direction"]
    control_id = manifest["control_hypothesis_id"]
    candidate_id = manifest["candidate_hypothesis_ids"][0]
    database = sqlite3.connect(study_dir / "research" / "research.sqlite")
    try:
        payloads = database.execute(
            "SELECT payload FROM snapshots WHERE kind = 'observation'").fetchall()
    finally:
        database.close()
    rows = [json.loads(payload) for (payload,) in payloads]
    return margin_from_rows(rows, control_id, candidate_id, direction)


def apply_min_improvement(project, delta):
    path = Path(project) / "experiment.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["min_improvement"] = delta
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return spec


def _network_failure(run_dir):
    folder = Path(run_dir) / "model-diagnostics"
    files = list(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        return False
    latest = max(files, key=lambda path: path.stat().st_mtime)
    try:
        item = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return item.get("status") == "failed" and bool(item.get("network_reason_type"))


def drive_tree(controller, *, max_steps=MAX_STEPS):
    """跑实验树并在确认门前停下。确认函数存在也不调用。"""
    status = controller.run(trusted_local=False, sandboxed=True, auto_confirm=False,
                            autonomous_code=True, search=True, max_steps=max_steps)
    return {"phase": status.get("phase"), "confirmation_calls": 0}


def best_dev_value(study_dir, direction):
    study_dir = Path(study_dir)
    manifest_path = study_dir / "research" / "research.json"
    database_path = study_dir / "research" / "research.sqlite"
    if not manifest_path.is_file() or not database_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate_id = manifest["candidate_hypothesis_ids"][0]
    database = sqlite3.connect(database_path)
    try:
        payloads = database.execute(
            "SELECT payload FROM snapshots WHERE kind = 'observation'").fetchall()
    finally:
        database.close()
    values = []
    for (payload,) in payloads:
        row = json.loads(payload)
        if row.get("hypothesis_id") == candidate_id and row.get("scope") == "dev":
            values.append(row["value"])
    if not values:
        return None
    return min(values) if direction == "min" else max(values)


def beats_linear(best, linear, direction):
    if best is None:
        return False
    return best < linear if direction == "min" else best > linear


def prior_tokens_used(report_path=REPORT_PATH):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    spent = report.get("tokens_used")
    if type(spent) is not int or spent < 0 or spent >= TOKEN_CAP:
        raise ProtocolError("已完成运行的 token 用量无法作为剩余额度的起点")
    return spent


def _blank(task_id, reason, margin=None):
    return {
        "task_id": task_id,
        "best_dev": None,
        "linear_dev": None if margin is None else margin["linear"],
        "baseline_dev": None if margin is None else margin["baseline"],
        "min_improvement": None if margin is None else margin["delta"],
        "direction": None if margin is None else margin["direction"],
        "beats_linear": False,
        "phase": None,
        "confirmation_calls": 0,
        "tokens_used": 0,
        "paid_model_calls": 0,
        "reason": redact(reason),
    }


def run_tree_task(task_id, raw_root, tree_root, prior_root, budget):
    before_calls = budget.calls
    before_tokens = budget.total_used
    margin = None
    try:
        budget.begin_task()
    except ProtocolError as exc:
        return _blank(task_id, str(exc))
    project_root = Path(tree_root) / task_id
    run_dir = project_root / "research"
    try:
        margin = load_prior_margin(Path(prior_root) / task_id)
        project = project_root / "project"
        if not (project / ".popper" / "state.db").is_file():
            rows = load_task(task_id, Path(raw_root) / task_id)
            manifest = materialize(task_id, rows, project_root)
            apply_min_improvement(project, margin["delta"])
            assert_holdout_isolated(manifest)
            holdout_ids_absent_from_files(project, manifest)
            initialize(project)
        run_dir.mkdir(parents=True, exist_ok=True)
        budget.diagnostics_dir = run_dir / "model-diagnostics"
        policy = _policy(run_dir, budget)
        # 基线一次，搜索最多 MAX_STEPS 次。cap 在研究族登记后不能再改。
        ResearchController.initialize(
            project_root / "project", run_dir, policy=policy, budget_cap=MAX_STEPS + 1)
        controller = ResearchController(run_dir, policy=policy)
        try:
            outcome = None
            for attempt in range(3):
                try:
                    outcome = drive_tree(controller)
                except ProtocolError as exc:
                    if "模型调用失败" not in str(exc) or attempt == 2:
                        raise
                    time.sleep(20 * (attempt + 1))
                    continue
                if (best_dev_value(project_root, margin["direction"]) is not None
                        or not _network_failure(run_dir) or attempt == 2):
                    break
                time.sleep(20 * (attempt + 1))
        finally:
            controller.close()
        best = best_dev_value(project_root, margin["direction"])
        reason = None
        if not beats_linear(best, margin["linear"], margin["direction"]):
            reason = "实验树的最佳开发集节点没有超过线性候选" if best is not None else "实验树没有留下开发集分数"
    except Exception as exc:
        outcome = {"phase": None}
        best = None if margin is None else best_dev_value(project_root, margin["direction"])
        reason = redact(f"{type(exc).__name__}: {exc}")
        if best is not None and beats_linear(best, margin["linear"], margin["direction"]):
            reason = None
    tokens_used = budget.total_used - before_tokens
    return {
        "task_id": task_id,
        "best_dev": best,
        "linear_dev": None if margin is None else margin["linear"],
        "baseline_dev": None if margin is None else margin["baseline"],
        "min_improvement": None if margin is None else margin["delta"],
        "direction": None if margin is None else margin["direction"],
        "beats_linear": bool(margin and beats_linear(best, margin["linear"], margin["direction"])),
        "phase": outcome.get("phase"),
        "confirmation_calls": 0,
        "tokens_used": tokens_used,
        "paid_model_calls": _paid_calls(run_dir, budget.calls - before_calls),
        "reason": reason,
    }


def assemble_tree_report(results, *, spent_before, budget):
    report = {
        "schema": "mlebench-lite-tree-v1",
        "tasks": results,
        "budget_confirmed_tokens": TOKEN_CAP,
        "tokens_already_used": spent_before,
        "tokens_used": budget.total_used - spent_before,
        "tokens_used_total": budget.total_used,
        "paid_model_calls": sum(item["paid_model_calls"] for item in results),
        "confirmation_calls": 0,
        "status": "completed" if all(item["beats_linear"] for item in results) else "incomplete",
    }
    return build_run_report(report)


def write_tree_report(report, path=TREE_REPORT):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _finished_rows():
    if not TREE_REPORT.is_file():
        return {}
    report = json.loads(TREE_REPORT.read_text(encoding="utf-8"))
    return {item["task_id"]: item for item in report.get("tasks", [])
            if item.get("best_dev") is not None and item.get("confirmation_calls") == 0}


def main():
    if not sandbox_available():
        raise ProtocolError("实验树搜索只能在沙箱中运行")
    spent = prior_tokens_used()
    carried = spent
    previous_tasks = {}
    if TREE_REPORT.is_file():
        previous = json.loads(TREE_REPORT.read_text(encoding="utf-8"))
        previous_tasks = {item["task_id"]: item for item in previous.get("tasks", [])}
        previous_total = previous.get("tokens_used_total")
        if type(previous_total) is int and spent <= previous_total < TOKEN_CAP:
            carried = previous_total
    remaining = TOKEN_CAP - carried
    budget = TokenBudget(total_cap=TOKEN_CAP, per_task_cap=remaining)
    budget.total_used = carried
    prior = REPORT_PATH.parent / "work"
    finished = _finished_rows()
    results = []
    for task_id in TASKS:
        if task_id in finished:
            results.append(finished[task_id])
            print(f"keep {task_id} best_dev={finished[task_id]['best_dev']} "
                  f"confirmation_calls=0", flush=True)
            continue
        if budget.stopped or budget.total_used >= TOKEN_CAP:
            results.append(_blank(task_id, "合计 token 上限已到，这一题没有开始"))
            continue
        print(f"tree {task_id} tokens_used={budget.total_used}", flush=True)
        row = run_tree_task(task_id, data_root(), TREE_ROOT, prior, budget)
        old = previous_tasks.get(task_id)
        if old is not None:
            row["tokens_used"] += old.get("tokens_used") or 0
            row["paid_model_calls"] += old.get("paid_model_calls") or 0
        results.append(row)
        last = results[-1]
        print(f"done {task_id} beats_linear={last['beats_linear']} best_dev={last['best_dev']} "
              f"linear_dev={last['linear_dev']} tokens_used={budget.total_used} "
              f"confirmation_calls={last['confirmation_calls']}", flush=True)
    report = assemble_tree_report(results, spent_before=spent, budget=budget)
    write_tree_report(report)
    print(json.dumps({"status": report["status"], "tokens_used": report["tokens_used"],
                      "tokens_used_total": report["tokens_used_total"],
                      "paid_model_calls": report["paid_model_calls"],
                      "confirmation_calls": report["confirmation_calls"]},
                     ensure_ascii=False), flush=True)
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
