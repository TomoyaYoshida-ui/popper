"""任务注册表：汇总三族 12 个受控任务。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.blind_pilot.tasks.base import TaskSpec
from evaluation.blind_pilot.tasks.tabular_tasks import TABULAR_BUILDERS
from evaluation.blind_pilot.tasks.time_series_tasks import TIME_SERIES_BUILDERS
from evaluation.blind_pilot.tasks.vision_tasks import VISION_BUILDERS


def _spec(task_id, family, condition, evaluator, build, timeout_seconds=60):
    return TaskSpec(task_id=task_id, family=family, condition=condition,
                    evaluator_id=evaluator, build=build, timeout_seconds=timeout_seconds)


def build_task(task_id):
    """按 id 找 build 函数。"""
    for builders in (TABULAR_BUILDERS, TIME_SERIES_BUILDERS, VISION_BUILDERS):
        if task_id in builders:
            return builders[task_id]
    raise KeyError(f"未知任务: {task_id}")


def all_task_ids():
    return sorted([*TABULAR_BUILDERS, *TIME_SERIES_BUILDERS, *VISION_BUILDERS])


def specs():
    """返回全部 12 个 TaskSpec（含任务族/条件/评估器元数据）。"""
    meta = {
        "T01-wdbc-positive": ("tabular", "positive_effect", "binary-accuracy-v1"),
        "T02-synth-negative": ("tabular", "negative_effect", "binary-accuracy-v1"),
        "T03-synth-near-zero": ("tabular", "near_zero_effect", "binary-accuracy-v1"),
        "T04-synth-boundary": ("tabular", "scope_boundary_or_counterexample", "binary-accuracy-v1"),
        "T05-ts-positive": ("small_time_series", "positive_effect", "mse-v1"),
        "T06-ts-negative": ("small_time_series", "negative_effect", "mse-v1"),
        "T07-ts-near-zero": ("small_time_series", "near_zero_effect", "mse-v1"),
        "T08-ts-boundary": ("small_time_series", "scope_boundary_or_counterexample", "mse-v1"),
        "T09-vision-positive": ("small_vision", "positive_effect", "binary-accuracy-v1"),
        "T10-vision-negative": ("small_vision", "negative_effect", "binary-accuracy-v1"),
        "T11-vision-near-zero": ("small_vision", "near_zero_effect", "binary-accuracy-v1"),
        "T12-vision-boundary": ("small_vision", "scope_boundary_or_counterexample", "binary-accuracy-v1"),
    }
    out = []
    for task_id in all_task_ids():
        family, condition, evaluator = meta[task_id]
        out.append(_spec(task_id, family, condition, evaluator, build_task(task_id)))
    return out


def task_spec(task_id):
    for item in specs():
        if item.task_id == task_id:
            return item
    raise KeyError(f"未知任务: {task_id}")
