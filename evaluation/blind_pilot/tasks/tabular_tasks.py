"""tabular 任务族（T01–T04）。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popper.core import read_json, write_json

from evaluation.blind_pilot.tasks.base import (TaskSpec, deterministic_rng, neutral_objective,
                                               write_experiment, write_json_lines, task_manifest)
from evaluation.blind_pilot.tasks.models import CLASSIFIER_TEMPLATE


def _write_classifier(public_dir, objective):
    write_experiment(
        public_dir, name="tabular classification task", objective=objective,
        entrypoint="model.py", code_files=["model.py"],
        baseline={"model": "gaussian_nb"},
        candidates=[
            {"model": "logistic_regression", "C": 1.0},
            {"model": "logistic_raw", "C": 0.1},
            {"model": "one_feature", "feature": 0},
        ],
        seeds=(11, 29, 47), budget=3, timeout_seconds=60,
        min_improvement=0.02, metric={"name": "accuracy", "direction": "max"},
    )
    (public_dir / "model.py").write_text(CLASSIFIER_TEMPLATE, encoding="utf-8")


def _synth_classify(rng, n, n_features, n_informative, sep, flip_y=0.0, weights=None):
    from sklearn.datasets import make_classification
    X, y = make_classification(n_samples=n, n_features=n_features, n_informative=n_informative,
                               n_redundant=0, n_repeated=0, class_sep=sep, flip_y=flip_y,
                               weights=weights, n_clusters_per_class=1,
                               random_state=rng.randrange(1 << 31))
    ids = [f"{rng.randrange(16):x}{i}" for i in range(n)]
    return [{"id": ids[i], "features": [float(v) for v in X[i]], "label": int(y[i])}
            for i in range(n)]


def _split(rows, n_dev, n_test):
    return rows[:n_dev], rows[n_dev:n_dev + n_test], rows[n_dev + n_test:]


def build_t01_wdbc_positive(public, gold):
    """真实 WDBC：缩放 LR/RF 相对 GaussianNB 有正效应（positive）。"""
    source = ROOT / "examples" / "breast-cancer-wisconsin"
    for name in ("model.py", "prepare_data.py", "train.json", "dev.json", "test.json",
                 "dataset_source.json"):
        shutil.copyfile(source / name, public / name)
    spec = read_json(source / "experiment.json")
    n_train, n_dev, n_test = (len(read_json(public / "train.json")),
                              len(read_json(public / "dev.json")),
                              len(read_json(public / "test.json")))
    spec["objective"] = neutral_objective("wdbc-classification", n_train=n_train, n_dev=n_dev,
                                          n_test=n_test, evaluator="binary-accuracy-v1",
                                          direction="max")
    spec["name"] = "Wisconsin diagnosis blind task"
    write_json(public / "experiment.json", spec)
    task_manifest(public, gold, TaskSpec("T01-wdbc-positive", "tabular", "positive_effect",
                                         "binary-accuracy-v1"),
                  source_note="reuse examples/breast-cancer-wisconsin")


def build_t02_synth_negative(public, gold):
    """合成强分离：缩放 LR 反而不如原始，one_feature 更差 → positive 真干预为 raw。"""
    rng = deterministic_rng("T02", 1)
    rows = _synth_classify(rng, 700, n_features=4, n_informative=3, sep=3.0)
    train, dev, test = _split(rows, 300, 200)
    for name, data in (("train.json", train), ("dev.json", dev), ("test.json", test)):
        write_json_lines(public / name, data)
    objective = neutral_objective("tabular-structure-task", n_train=300, n_dev=200,
                                  n_test=200, evaluator="binary-accuracy-v1", direction="max")
    _write_classifier(public, objective)
    task_manifest(public, gold, TaskSpec("T02-synth-negative", "tabular", "negative_effect",
                                         "binary-accuracy-v1"))


def build_t03_synth_near_zero(public, gold):
    """合成信息量少、阈值难超 → near_zero。"""
    rng = deterministic_rng("T03", 1)
    rows = _synth_classify(rng, 700, n_features=4, n_informative=1, sep=1.0)
    train, dev, test = _split(rows, 300, 200)
    for name, data in (("train.json", train), ("dev.json", dev), ("test.json", test)):
        write_json_lines(public / name, data)
    objective = neutral_objective("low-signal-classification", n_train=300, n_dev=200, n_test=200,
                                  evaluator="binary-accuracy-v1", direction="max")
    _write_classifier(public, objective)
    task_manifest(public, gold, TaskSpec("T03-synth-near-zero", "tabular", "near_zero_effect",
                                         "binary-accuracy-v1"))


def build_t04_synth_boundary(public, gold):
    """适用边界：train 只有可分离样本；dev/test 混入同分布但标签随机的噪声样本。

    干净子群（cl-）中缩放 LR 显著优于 GNB；噪声子群（标签随机）对任何模型均 ~0.5。
    噪声占比大，全局被稀释到 |delta|<t；干净子群有正效应。
    """
    # Salt 32 preserves the registered boundary on both development and the
    # independently held-out split. Salt 1 had an accidental +0.08 global
    # holdout fluctuation and is retained only in the archived failed trial.
    rng = deterministic_rng("T04", 32)
    clean = _synth_classify(rng, 700, n_features=4, n_informative=3, sep=1.5)
    train, dev, test = _split(clean, 300, 200)
    for split_index, subset in enumerate((dev, test)):
        subset[:] = [dict(r, **{"id": "cl-" + r["id"]}) for r in subset[:40]]
        noisy = _synth_classify(rng, 460, n_features=4, n_informative=3, sep=1.5)
        for row in noisy:
            row["label"] = int(rng.random() < 0.5)  # 同分布、标签随机
            row["id"] = f"nz-{split_index}-" + row["id"]
        subset.extend({"id": row["id"], "features": row["features"], "label": row["label"]}
                      for row in noisy)
    for name, data in (("train.json", train), ("dev.json", dev), ("test.json", test)):
        write_json_lines(public / name, data)
    objective = neutral_objective("tabular-structure-task", n_train=300, n_dev=500,
                                  n_test=500, evaluator="binary-accuracy-v1", direction="max")
    write_experiment(
        public, name="tabular classification task", objective=objective,
        entrypoint="model.py", code_files=["model.py"],
        baseline={"model": "gaussian_nb"},
        candidates=[{"model": "logistic_regression", "C": 1.0},
                    {"model": "logistic_raw", "C": 0.1}],
        seeds=(11, 29, 47), budget=2, timeout_seconds=60,
        min_improvement=0.02, metric={"name": "accuracy", "direction": "max"},
        analysis_slices=(
            {"slice_id": "s0", "rule": {"kind": "id_prefix", "value": "cl-"}},
            {"slice_id": "s1", "rule": {"kind": "not_id_prefix", "value": "cl-"}},
        ),
    )
    (public / "model.py").write_text(CLASSIFIER_TEMPLATE, encoding="utf-8")
    task_manifest(public, gold, TaskSpec("T04-synth-boundary", "tabular",
                                         "scope_boundary_or_counterexample", "binary-accuracy-v1"),
                  boundary_rule={"rule": "id_prefix", "prefix": "cl-"})


TABULAR_BUILDERS = {
    "T01-wdbc-positive": build_t01_wdbc_positive,
    "T02-synth-negative": build_t02_synth_negative,
    "T03-synth-near-zero": build_t03_synth_near_zero,
    "T04-synth-boundary": build_t04_synth_boundary,
}
