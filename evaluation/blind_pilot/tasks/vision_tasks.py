"""small_vision 任务族（T09–T12）：digits 8×8→64 特征二分类，binary-accuracy-v1。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.blind_pilot.tasks.base import (TaskSpec, deterministic_rng, neutral_objective,
                                               write_experiment, write_json_lines, task_manifest)
from evaluation.blind_pilot.tasks.models import CLASSIFIER_TEMPLATE


def _digits(rng):
    """确定性 digits 数据：data 64 维特征，target 0-9。"""
    from sklearn.datasets import load_digits
    digits = load_digits()
    rows = []
    for i in range(len(digits.data)):
        rows.append({"id": f"{rng.randrange(16):x}{i}", "features": [float(v) for v in digits.data[i]],
                     "digit": int(digits.target[i])})
    rng.shuffle(rows)
    return rows


def _label(rows, predicate):
    for row in rows:
        row["label"] = int(predicate(row["digit"]))
    return rows


def _split(rows, n_train, n_dev, n_test):
    return rows[:n_train], rows[n_train:n_train + n_dev], rows[n_train + n_dev:n_train + n_dev + n_test]


def _write_splits(public, splits):
    """写三划分 json，先剔除 'digit' 内部键（dataset() 只允许 id/features/label）。"""
    for name, data in zip(("train.json", "dev.json", "test.json"), splits):
        clean = [{"id": r["id"], "features": r["features"], "label": r["label"]} for r in data]
        write_json_lines(public / name, clean)


def _write_vision(public, objective, baseline, candidates, min_improvement=0.02,
                  budget=3, analysis_slices=()):
    write_experiment(
        public, name="small vision classification task", objective=objective,
        entrypoint="model.py", code_files=["model.py"],
        baseline=baseline, candidates=candidates,
        seeds=(11, 29, 47), budget=budget, timeout_seconds=90,
        min_improvement=min_improvement, metric={"name": "accuracy", "direction": "max"},
        analysis_slices=analysis_slices,
    )
    (public / "model.py").write_text(CLASSIFIER_TEMPLATE, encoding="utf-8")


def build_t09_vision_positive(public, gold):
    """digit≥5：RF 相对 GaussianNB 显著提升 → positive。"""
    rng = deterministic_rng("T09", 1)
    rows = _label(_digits(rng), lambda d: d >= 5)
    train, dev, test = _split(rows, 800, 300, 300)
    _write_splits(public, (train, dev, test))
    objective = neutral_objective("digit-high-low", n_train=800, n_dev=300, n_test=300,
                                  evaluator="binary-accuracy-v1", direction="max")
    _write_vision(public, objective, {"model": "gaussian_nb"},
                  [{"model": "logistic_regression", "C": 1.0},
                   {"model": "logistic_raw", "C": 1.0},
                   {"model": "one_feature", "feature": 0}],
                  min_improvement=0.02)
    task_manifest(public, gold, TaskSpec("T09-vision-positive", "small_vision", "positive_effect",
                                         "binary-accuracy-v1"))


def build_t10_vision_negative(public, gold):
    """digit==0：one_feature/raw LR 不优于 GNB（raw 可能更差）→ negative。"""
    rng = deterministic_rng("T10", 1)
    rows = _label(_digits(rng), lambda d: d == 0)
    train, dev, test = _split(rows, 800, 300, 300)
    _write_splits(public, (train, dev, test))
    objective = neutral_objective("digit-zero-detection", n_train=800, n_dev=300, n_test=300,
                                  evaluator="binary-accuracy-v1", direction="max")
    _write_vision(public, objective, {"model": "gaussian_nb"},
                  [{"model": "logistic_regression", "C": 0.01},
                   {"model": "one_feature", "feature": 0},
                   {"model": "one_feature", "feature": 1}],
                  min_improvement=0.02)
    task_manifest(public, gold, TaskSpec("T10-vision-negative", "small_vision", "negative_effect",
                                         "binary-accuracy-v1"))


def build_t11_vision_near_zero(public, gold):
    """digit≥5 强分离：以 LR 为基线，多种正则化/实现变体均接近饱和，不显著更好 → near_zero。"""
    rng = deterministic_rng("T11", 1)
    rows = _label(_digits(rng), lambda d: d >= 5)
    train, dev, test = _split(rows, 800, 300, 300)
    _write_splits(public, (train, dev, test))
    objective = neutral_objective("digit-high-low", n_train=800, n_dev=300, n_test=300,
                                  evaluator="binary-accuracy-v1", direction="max")
    _write_vision(public, objective, {"model": "logistic_regression", "C": 1.0},
                  [{"model": "logistic_regression", "C": 0.5},
                   {"model": "logistic_regression", "C": 2.0},
                   {"model": "logistic_raw", "C": 1.0}],
                  min_improvement=0.04)
    task_manifest(public, gold, TaskSpec("T11-vision-near-zero", "small_vision",
                                         "near_zero_effect", "binary-accuracy-v1"))


def build_t12_vision_boundary(public, gold):
    """适用边界：train 只有干净样本；dev/test 混入 ~90% 随机标签噪声样本。

    干净子集（id 前缀 cl-）LR 显著优于 GNB；噪声子集（nz-，标签随机）不可学，
    任何模型均 ~0.5。噪声占比大，全局被拉平，|delta|<t；干净子集有正效应。
    """
    rng = deterministic_rng("T12", 1)
    rows = _label(_digits(rng), lambda d: d >= 5)
    train, dev, test = _split(rows, 800, 300, 300)
    for subset in (dev, test):
        subset[:] = [dict(r, **{"id": "cl-" + r["id"]}) for r in subset[:50]]
        noisy = []
        for i in range(450):
            base = subset[rng.randrange(len(subset))]
            noisy.append({"id": "nz-" + base["id"] + f"-{i}",
                          "features": [float(v) + rng.gauss(0.0, 3.0) for v in base["features"]],
                          "label": int(rng.random() < 0.5)})
        subset.extend(noisy)
    _write_splits(public, (train, dev, test))
    objective = neutral_objective("digit-high-low-noisy", n_train=800, n_dev=500, n_test=500,
                                  evaluator="binary-accuracy-v1", direction="max")
    _write_vision(public, objective, {"model": "gaussian_nb"},
                  [{"model": "logistic_regression", "C": 1.0},
                   {"model": "logistic_raw", "C": 1.0},
                   {"model": "one_feature", "feature": 0}],
                  min_improvement=0.05,
                  analysis_slices=(
                      {"slice_id": "s0", "rule": {"kind": "id_prefix", "value": "cl-"}},
                      {"slice_id": "s1", "rule": {"kind": "not_id_prefix", "value": "cl-"}},
                  ))
    task_manifest(public, gold, TaskSpec("T12-vision-boundary", "small_vision",
                                         "scope_boundary_or_counterexample",
                                         "binary-accuracy-v1"),
                  boundary_rule={"rule": "id_prefix", "prefix": "cl-"})


VISION_BUILDERS = {
    "T09-vision-positive": build_t09_vision_positive,
    "T10-vision-negative": build_t10_vision_negative,
    "T11-vision-near-zero": build_t11_vision_near_zero,
    "T12-vision-boundary": build_t12_vision_boundary,
}
