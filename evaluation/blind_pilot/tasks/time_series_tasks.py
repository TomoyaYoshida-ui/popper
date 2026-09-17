"""small_time_series 任务族（T05–T08）：单特征回归，mse-v1。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.blind_pilot.tasks.base import (TaskSpec, deterministic_rng, neutral_objective,
                                               write_experiment, write_json_lines, task_manifest)
from evaluation.blind_pilot.tasks.models import REGRESSOR_TEMPLATE


def _write_regressor(public_dir, objective, min_improvement=0.1):
    write_experiment(
        public_dir, name="time series regression task", objective=objective,
        entrypoint="model.py", code_files=["model.py"],
        baseline={"degree": 1},
        candidates=[{"degree": 0}, {"degree": 2}, {"degree": 3}],
        seeds=(11, 29, 47), budget=3, timeout_seconds=60,
        min_improvement=min_improvement, metric={"name": "mse", "direction": "min"},
    )
    (public_dir / "model.py").write_text(REGRESSOR_TEMPLATE, encoding="utf-8")


def _series(rng, n, fn, noise=0.05):
    """确定性生成 {id, x, y} 序列；x 均匀分布于 [-3, 3]。"""
    ids = [f"{rng.randrange(16):x}{i}" for i in range(n)]
    rows = []
    for i in range(n):
        x = rng.uniform(-3.0, 3.0)
        rows.append({"id": ids[i], "x": x, "y": fn(x) + rng.gauss(0.0, noise)})
    return rows


def _split(rows, n_dev, n_test):
    return rows[:len(rows) - n_dev - n_test], rows[len(rows) - n_dev - n_test:len(rows) - n_test], \
        rows[len(rows) - n_test:]


def build_t05_ts_positive(public, gold):
    """二次机制：degree2 相对线性基线显著降 MSE → positive。"""
    rng = deterministic_rng("T05", 1)

    def fn(x):
        return 0.5 + 0.7 * x + 1.8 * x * x

    rows = _series(rng, 700, fn)
    train, dev, test = _split(rows, 150, 150)
    for name, data in (("train.json", train), ("dev.json", dev), ("test.json", test)):
        write_json_lines(public / name, data)
    objective = neutral_objective("scalar-time-series", n_train=400, n_dev=150, n_test=150,
                                  evaluator="mse-v1", direction="min")
    _write_regressor(public, objective, min_improvement=0.2)
    task_manifest(public, gold, TaskSpec("T05-ts-positive", "small_time_series", "positive_effect",
                                         "mse-v1"))


def build_t06_ts_negative(public, gold):
    """线性机制：非线性 degree3/abs 变换不降 MSE（甚至更差）→ negative。"""
    rng = deterministic_rng("T06", 1)

    def fn(x):
        return 0.5 + 0.9 * x

    rows = _series(rng, 700, fn)
    train, dev, test = _split(rows, 150, 150)
    for name, data in (("train.json", train), ("dev.json", dev), ("test.json", test)):
        write_json_lines(public / name, data)
    objective = neutral_objective("scalar-time-series", n_train=400, n_dev=150, n_test=150,
                                  evaluator="mse-v1", direction="min")
    # baseline degree1（正确）；候选 degree3（过拟合）、sign 变换（信息丢失）
    write_experiment(
        public, name="time series regression task", objective=objective,
        entrypoint="model.py", code_files=["model.py"],
        baseline={"degree": 1},
        candidates=[{"degree": 0}, {"degree": 3}, {"transform": "sign", "degree": 1}],
        seeds=(11, 29, 47), budget=3, timeout_seconds=60,
        min_improvement=0.1, metric={"name": "mse", "direction": "min"},
    )
    (public / "model.py").write_text(REGRESSOR_TEMPLATE, encoding="utf-8")
    task_manifest(public, gold, TaskSpec("T06-ts-negative", "small_time_series", "negative_effect",
                                         "mse-v1"))


def build_t07_ts_near_zero(public, gold):
    """纯噪声：任何多项式都不显著优于线性 → near_zero。"""
    rng = deterministic_rng("T07", 1)

    def fn(x):
        return 0.0

    rows = _series(rng, 700, fn, noise=1.0)
    train, dev, test = _split(rows, 150, 150)
    for name, data in (("train.json", train), ("dev.json", dev), ("test.json", test)):
        write_json_lines(public / name, data)
    objective = neutral_objective("scalar-time-series", n_train=400, n_dev=150, n_test=150,
                                  evaluator="mse-v1", direction="min")
    _write_regressor(public, objective, min_improvement=0.15)
    task_manifest(public, gold, TaskSpec("T07-ts-near-zero", "small_time_series",
                                         "near_zero_effect", "mse-v1"))


def _series_boundary(rng, n, fn, noise=0.05):
    """确定性生成 {id, x, y}；~90% 样本位于 |x|>1 线性区、~10% 位于 |x|≤1 二次区。"""
    ids = [f"{rng.randrange(16):x}{i}" for i in range(n)]
    rows = []
    for i in range(n):
        if rng.random() < 0.90:
            x = rng.choice([rng.uniform(-3.0, -1.0), rng.uniform(1.0, 3.0)])
        else:
            x = rng.uniform(-1.0, 1.0)
        rows.append({"id": ids[i], "x": x, "y": fn(x) + rng.gauss(0.0, noise)})
    return rows


def build_t08_ts_boundary(public, gold):
    """分段机制：|x|≤1 强二次、|x|>1 真线性；degree2 在低|区域显著，全局不显著 → boundary。"""
    rng = deterministic_rng("T08", 1)

    def fn(x):
        return (0.5 + 3.0 * x * x if abs(x) <= 1.0 else 1.5 * x)

    rows = _series_boundary(rng, 700, fn, noise=0.05)
    train, dev, test = _split(rows, 150, 150)
    for name, data in (("train.json", train), ("dev.json", dev), ("test.json", test)):
        write_json_lines(public / name, data)
    objective = neutral_objective("scalar-time-series", n_train=400, n_dev=150, n_test=150,
                                  evaluator="mse-v1", direction="min")
    # 只保留多项式候选；DummyRegressor(degree0) 在 |x|>1 线性区 MSE 过大，会破坏全局带宽
    write_experiment(
        public, name="time series regression task", objective=objective,
        entrypoint="model.py", code_files=["model.py"],
        baseline={"degree": 1},
        candidates=[{"degree": 2}, {"degree": 3}],
        seeds=(11, 29, 47), budget=2, timeout_seconds=60,
        min_improvement=0.08, metric={"name": "mse", "direction": "min"},
        analysis_slices=(
            {"slice_id": "s0", "rule": {"kind": "abs_x_le", "value": 1.0}},
            {"slice_id": "s1", "rule": {"kind": "abs_x_gt", "value": 1.0}},
        ),
    )
    (public / "model.py").write_text(REGRESSOR_TEMPLATE, encoding="utf-8")
    task_manifest(public, gold, TaskSpec("T08-ts-boundary", "small_time_series",
                                         "scope_boundary_or_counterexample", "mse-v1"),
                  boundary_rule={"rule": "abs_le", "bound": 1.0})


TIME_SERIES_BUILDERS = {
    "T05-ts-positive": build_t05_ts_positive,
    "T06-ts-negative": build_t06_ts_negative,
    "T07-ts-near-zero": build_t07_ts_near_zero,
    "T08-ts-boundary": build_t08_ts_boundary,
}
