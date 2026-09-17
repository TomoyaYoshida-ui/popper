"""Prepare deterministic splits from the UCI Pima Indians Diabetes dataset.

零值处理：Pima 的 plas(glucose)/pres/皮肤厚度/insu(insulin)/mass(BMI) 在生理上不可能为 0，
视为缺失值。本脚本只做划分并保留原始 0 值；插补与否由候选配置（model.py 的 --config）决定。
数据经 GitHub 镜像固定 SHA-256 校验下载（UCI 官方 archive.ics.uci.edu 旧版 URL 已下线）。
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

from sklearn.model_selection import train_test_split

MIRROR_URL = ("https://raw.githubusercontent.com/jbrownlee/Datasets/master/"
              "pima-indians-diabetes.data.csv")
EXPECTED_SHA256 = "6bfe5d0f379d17a0e0819b996407e3c09bf80febd4287f2ed212190dfff154af"
# 列为 0 值即视为缺失的列索引（0-based，对应 plas/pres/skin/insu/mass）
MISSING_INDICES = (1, 2, 3, 4, 5)
COLUMN_NAMES = ["preg", "plas", "pres", "skin", "insu", "mass", "pedi", "age", "class"]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")


def fetch_raw():
    request = urllib.request.Request(MIRROR_URL, headers={"User-Agent": "Popper/0.1 research verification"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(5_000_001)
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        raise RuntimeError(f"数据源 SHA-256 不匹配: {digest}")
    rows = []
    for index, line in enumerate(data.decode("utf-8").strip().splitlines()):
        parts = [value.strip() for value in line.split(",")]
        if len(parts) != len(COLUMN_NAMES):
            raise RuntimeError(f"行 {index} 列数异常: {len(parts)}")
        rows.append([float(value) for value in parts])
    return rows


def rows(indices, data, split):
    return [{"id": f"pima-{int(index)}", "features": [float(value) for value in data[index][:8]],
             "label": int(data[index][8])} for index in indices]


def prepare(root):
    root = Path(root)
    if (root / ".popper").exists():
        raise RuntimeError("实验已经初始化；不能替换已登记的数据划分")
    bundle = fetch_raw()
    indices = list(range(len(bundle)))
    train_indices, remaining = train_test_split(
        indices, test_size=0.4, random_state=7, stratify=[row[8] for row in bundle])
    dev_indices, test_indices = train_test_split(
        remaining, test_size=0.5, random_state=7,
        stratify=[bundle[index][8] for index in remaining])
    write_json(root / "train.json", rows(train_indices, bundle, "train"))
    write_json(root / "dev.json", rows(dev_indices, bundle, "dev"))
    write_json(root / "test.json", rows(test_indices, bundle, "test"))
    zero_counts = {COLUMN_NAMES[index]: sum(
        1 for row in bundle if row[index] == 0) for index in MISSING_INDICES}
    write_json(root / "dataset_source.json", {
        "dataset": "Pima Indians Diabetes",
        "provider": "UCI Machine Learning Repository",
        "original_source": "https://archive.ics.uci.edu/dataset/34/pima+indians+diabetes",
        "mirror_url": MIRROR_URL,
        "mirror_sha256": EXPECTED_SHA256,
        "instances": len(bundle),
        "features": 8,
        "class_names": ["0", "1"],
        "zero_as_missing_columns": zero_counts,
        "split": {"train": len(train_indices), "dev": len(dev_indices), "test": len(test_indices)},
        "random_state": 7,
        "use": "M2 真实研究试点（零值插补比较）；不用于临床诊断"
    })


if __name__ == "__main__":
    prepare(Path(__file__).parent)
