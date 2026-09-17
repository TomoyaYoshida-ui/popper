"""盲测报告聚合：有效闭环判定、按对照/条件/任务族汇总、失败分类、blind-report.json。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popper.core import write_json

from evaluation.blind_pilot.policies import COMPARATORS


def classify_failure(summary):
    """按失败阶段/类型分类，用于失败清单。"""
    if summary.get("status") == "completed":
        return None
    error_type = summary.get("error_type")
    if error_type == "blocked_missing_credential":
        return "blocked_missing_credential"
    if "TimeoutError" in (error_type or ""):
        return "timeout"
    if "Integrity" in (error_type or "") or "REPLAY" in (error_type or ""):
        return "integrity_violation"
    if "ProtocolError" in (error_type or ""):
        return "protocol_error"
    return "infra"


def is_valid_loop(summary):
    """有效性硬门：同 runner._is_valid_loop 语义，report 独立实现保持可测。"""
    if (summary.get("status") != "completed" or not summary.get("integrity_ok")
            or not summary.get("evidence_replay", {}).get("ok")):
        return False
    phase = summary.get("phase")
    if phase in {"concluded", "budget_exhausted"}:
        return True
    if phase == "ready_for_confirmation" and summary["test_exposure"]["consumed"]:
        return True
    return False


def build_report(summaries, *, protocol_sha256=None, run_config=None, planned_trajectories=None):
    """聚合 cell 摘要为 blind-report.json 结构。"""
    valid = [s for s in summaries if is_valid_loop(s)]
    by_comparator, by_condition, by_family = {}, {}, {}
    failures = []
    confirm_modes = {}
    terminal_count = {}
    for summary in summaries:
        comparator, condition, family = (summary["comparator"], summary["condition"],
                                         summary["family"])
        mode = summary.get("confirm_mode", "local")
        confirm_modes[mode] = confirm_modes.get(mode, 0) + 1
        phase = summary.get("phase")
        if phase:
            terminal_count[phase] = terminal_count.get(phase, 0) + 1
        group = by_comparator.setdefault(comparator, {"planned": 0, "valid": 0,
                                                      "conclusion_matched": 0,
                                                      "scientifically_valid": 0,
                                                      "failures": []})
        group["planned"] += 1
        group["valid"] += 1 if is_valid_loop(summary) else 0
        group["conclusion_matched"] += 1 if summary.get("conclusion_matched") else 0
        group["scientifically_valid"] += 1 if (is_valid_loop(summary)
                                                and summary.get("conclusion_matched")) else 0
        cond_group = by_condition.setdefault(condition, {"task_ids": set(), "planned": 0, "valid": 0,
                                                         "conclusion_matched": 0})
        cond_group["task_ids"].add(summary["task_id"])
        cond_group["planned"] += 1
        if is_valid_loop(summary):
            cond_group["valid"] += 1
        if summary.get("conclusion_matched"):
            cond_group["conclusion_matched"] += 1
        fam_group = by_family.setdefault(family, {"planned": 0, "valid": 0})
        fam_group["planned"] += 1
        fam_group["valid"] += 1 if is_valid_loop(summary) else 0
        failure = classify_failure(summary)
        if failure:
            failures.append({"cell": summary["cell"], "stage": summary.get("error_stage"),
                             "classification": failure,
                             "error_type": summary.get("error_type"),
                             "message": summary.get("error_message")})
            group["failures"].append(failures[-1])

    planned = planned_trajectories if planned_trajectories is not None else len(summaries)
    conclusions = [s for s in summaries if s.get("conclusion_matched") is True]
    scientifically_valid = [s for s in valid if s.get("conclusion_matched") is True]
    report = {
        "schema_version": "2.0",
        "protocol_sha256": protocol_sha256,
        "run_config": run_config,
        "planned_trajectories": planned,
        "valid_loops": len(valid),
        "conclusion_matched": len(conclusions),
        "scientifically_valid_loops": len(scientifically_valid),
        "by_comparator": by_comparator,
        "by_condition": {k: {**v, "task_ids": sorted(v["task_ids"])} for k, v in by_condition.items()},
        "by_family": by_family,
        "terminal_phases": terminal_count,
        "failures": failures,
        "confirm_modes_used": confirm_modes,
        "progression_target_met": planned > 0 and len(valid) / planned >= 29 / 36,
        "claims": {
            "progression_target_met": planned > 0 and len(valid) / planned >= 29 / 36,
            "adaptive_gain_claimed": False,
            "scientific_accuracy_claimed": False,
            "claim_limit": "工程受控任务验证，非真隔离盲态、新颖性发现或通用能力声明。"
        },
    }
    return report


def write_report(report, trial_root):
    path = Path(trial_root) / "blind-report.json"
    write_json(path, report)
    return path
