"""矩阵编排：build/run 盲测矩阵（task × comparator × run_index），幂等续跑。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popper.core import read_json

from evaluation.blind_pilot.policies import COMPARATORS
from evaluation.blind_pilot.runner import _new_cell_dir, run_one_cell


def build_matrix(task_ids, comparators, runs_per_task):
    """返回全部 cell 定义列表。"""
    cells = []
    for task_id in task_ids:
        for comparator in comparators:
            for run_index in range(1, runs_per_task + 1):
                cells.append({"task_id": task_id, "comparator": comparator,
                              "run_index": run_index})
    return cells


def cell_done(trial_root, cell):
    summary = _new_cell_dir(trial_root, cell) / "cell-summary.json"
    return summary.is_file() and read_json(summary).get("status") == "completed"


def run_matrix(cells, *, trial_root, confirm_mode="local", model_url=None,
               model_name="deepseek-flash", resume=True, gpu_count=0):
    """逐 cell 运行；resume=True 跳过已完成 cell。返回 cell 摘要列表。"""
    trial_root = Path(trial_root)
    trial_root.mkdir(parents=True, exist_ok=True)
    summaries = []
    for cell in cells:
        if resume and cell_done(trial_root, cell):
            summaries.append(read_json(_new_cell_dir(trial_root, cell) / "cell-summary.json"))
            continue
        summary = run_one_cell(cell, trial_root=trial_root, confirm_mode=confirm_mode,
                               model_url=model_url, model_name=model_name, gpu_count=gpu_count)
        summaries.append(summary)
    return summaries