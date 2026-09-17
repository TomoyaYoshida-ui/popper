"""Prepare deterministic splits from scikit-learn's real Wisconsin dataset."""
from __future__ import annotations

import json
from pathlib import Path

import sklearn
from sklearn.datasets import load_breast_cancer
from sklearn.model_selection import train_test_split


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")


def rows(indices, data, labels, split):
    return [{"id": f"wdbc-{int(index)}", "features": [float(value) for value in data[index]],
             "label": int(labels[index])} for index in indices]


def prepare(root):
    root = Path(root)
    if (root / ".popper").exists():
        raise RuntimeError("实验已经初始化；不能替换已登记的数据划分")
    bundle = load_breast_cancer()
    indices = list(range(len(bundle.target)))
    train_indices, remaining = train_test_split(
        indices, test_size=0.4, random_state=20260908, stratify=bundle.target)
    dev_indices, test_indices = train_test_split(
        remaining, test_size=0.5, random_state=20260909,
        stratify=[bundle.target[index] for index in remaining])
    write_json(root / "train.json", rows(train_indices, bundle.data, bundle.target, "train"))
    write_json(root / "dev.json", rows(dev_indices, bundle.data, bundle.target, "dev"))
    write_json(root / "test.json", rows(test_indices, bundle.data, bundle.target, "test"))
    write_json(root / "dataset_source.json", {
        "dataset": "Breast Cancer Wisconsin (Diagnostic)",
        "provider": "scikit-learn.datasets.load_breast_cancer",
        "original_source": "UCI Machine Learning Repository / WDBC",
        "scikit_learn_version": sklearn.__version__,
        "instances": len(bundle.target),
        "features": len(bundle.feature_names),
        "class_names": [str(value) for value in bundle.target_names],
        "split": {"train": len(train_indices), "dev": len(dev_indices), "test": len(test_indices)},
        "random_states": {"train_remaining": 20260908, "dev_test": 20260909},
        "use": "工程接入评测；不用于临床诊断"
    })


if __name__ == "__main__":
    prepare(Path(__file__).parent)
