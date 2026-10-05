"""把三道 MLE-bench Lite 小题准备成研究项目。

公开训练按与 MLE-bench 相同的 sklearn 划分切出 10% 作为确认集，标签只写入
project/test.json。train.json 与 dev.json 不含这些样本。开发集再从剩余公开
训练中切出，供内核做开发计分；它不是比赛的隐藏测试。

真实模型不在本模块里调用。原始文件缺失时只写预算报告，已确认的 60 万 token
上限保持未消耗。
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from sklearn.model_selection import train_test_split

from popper.domains.mlebench_lite import SPOOKY_AUTHORS
from popper.sandbox import seal_read_available

TOKEN_CAP = 600_000
PER_TASK_CAP = 200_000
TASKS = (
    "spooky-author-identification",
    "nomad2018-predict-transparent-conductors",
    "random-acts-of-pizza",
)
_NOMAD_TARGETS = ("formation_energy_ev_natom", "bandgap_energy_ev")
_METRICS = {
    "spooky-author-identification": {"name": "spooky_log_loss", "direction": "min"},
    "nomad2018-predict-transparent-conductors": {"name": "nomad_mean_rmsle", "direction": "min"},
    "random-acts-of-pizza": {"name": "pizza_roc_auc", "direction": "max"},
}


def data_root():
    override = os.environ.get("MLEBENCH_LITE_RAW")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "data" / "mlebench-lite"


def holdout_split(rows):
    """10% 确认集，种子 0；再从公开部分切 20% 开发集，种子 1。"""
    public, private = train_test_split(list(rows), test_size=0.1, random_state=0)
    train, dev = train_test_split(list(public), test_size=0.2, random_state=1)
    train, dev, private = list(train), list(dev), list(private)
    _assert_disjoint(train, dev, private)
    return train, dev, private


def materialize(task_id, rows, dest):
    """写出项目目录。确认标签在 test.json，不出现在 train.json 或 dev.json。"""
    if task_id not in TASKS:
        raise ValueError(f"不是这三道 Lite 小题之一: {task_id}")
    dest = Path(dest)
    project = dest / "project"
    project.mkdir(parents=True, exist_ok=True)
    train, dev, private = holdout_split(rows)
    _write_json(project / "train.json", train)
    _write_json(project / "dev.json", dev)
    _write_json(project / "test.json", private)
    _write_json(project / "experiment.json", _experiment(task_id))
    (project / "model.py").write_text(_MODEL, encoding="utf-8")
    manifest = {
        "task_id": task_id,
        "confirmation_file": "project/test.json",
        "holdout_ids": [row["id"] for row in private],
        "train_ids": [row["id"] for row in train],
        "dev_ids": [row["id"] for row in dev],
    }
    _write_json(dest / "holdout_manifest.json", manifest)
    return manifest


def load_task(task_id, raw_dir):
    raw_dir = Path(raw_dir)
    if task_id == "spooky-author-identification":
        return _load_spooky(raw_dir / "train.csv")
    if task_id == "nomad2018-predict-transparent-conductors":
        return _load_nomad(raw_dir / "train.csv")
    if task_id == "random-acts-of-pizza":
        return _load_pizza(raw_dir / "train.json")
    raise ValueError(f"不是这三道 Lite 小题之一: {task_id}")


def task_ready(root, task_id):
    root = Path(root) / task_id
    if task_id == "random-acts-of-pizza":
        return (root / "train.json").is_file()
    return (root / "train.csv").is_file()


def build_report(root=None):
    root = Path(root) if root is not None else data_root()
    missing = [task_id for task_id in TASKS if not task_ready(root, task_id)]
    ready = not missing
    reason = "三道题的原始训练文件已就位，尚未调用模型。" if ready else (
        "未找到原始训练文件，也没有 Kaggle 凭据。已确认的 token 上限没有消耗。")
    return {
        "schema": "mlebench-lite-budget-v1",
        "tasks": list(TASKS),
        "missing_tasks": missing,
        "data_root": str(root),
        "budget_confirmed_tokens": TOKEN_CAP,
        "per_task_token_cap": PER_TASK_CAP,
        "paid_model_calls": 0,
        "tokens_used": 0,
        "holdout_seal_available": bool(seal_read_available()),
        "status": "ready" if ready else "blocked_missing_raw_data",
        "reason": reason,
    }


def write_report(path, root=None):
    report = build_report(root)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _experiment(task_id):
    return {
        "name": task_id,
        "objective": "在公开训练上开发，隐藏的 10% 只用于一次性确认",
        "entrypoint": "model.py",
        "code_files": ["model.py"],
        "train": "train.json",
        "dev": "dev.json",
        "test": "test.json",
        "baseline": {"model": "constant"},
        "candidates": [{"model": "linear"}],
        "metric": _METRICS[task_id],
        "seeds": [0],
        "budget": 1,
        "timeout_seconds": 120,
        "min_improvement": 0.01,
    }


def _load_spooky(path):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            rows.append({
                "id": record["id"],
                "text": record["text"],
                "label": SPOOKY_AUTHORS.index(record["author"]),
            })
    return rows


def _load_nomad(path):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            features = [float(record[name]) for name in record
                        if name not in {"id", *_NOMAD_TARGETS}]
            rows.append({
                "id": str(record["id"]),
                "features": features,
                "y": [float(record[name]) for name in _NOMAD_TARGETS],
            })
    return rows


def _load_pizza(path):
    rows = []
    for record in json.loads(path.read_text(encoding="utf-8")):
        text = record.get("request_text_edit_aware") or record.get("request_text") or record.get("request_title")
        rows.append({
            "id": str(record["request_id"]),
            "text": text,
            "label": int(record["requester_received_pizza"]),
        })
    return rows


def _assert_disjoint(train, dev, private):
    groups = (train, dev, private)
    ids = [row["id"] for rows in groups for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("确认集与开发样本发生重叠")
    private_ids = {row["id"] for row in private}
    for row in (*train, *dev):
        if row["id"] in private_ids:
            raise ValueError("确认标签进入了开发文件")


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_MODEL = '''"""候选入口。确认集由研究内核单独计分，本文件不读取 test.json。"""
from __future__ import annotations


def main():
    raise SystemExit("候选模型尚未实现")


if __name__ == "__main__":
    main()
'''


def main():
    report_path = Path(__file__).resolve().parent / "runs" / "mlebench-lite" / "report.json"
    report = write_report(report_path)
    print(report["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
