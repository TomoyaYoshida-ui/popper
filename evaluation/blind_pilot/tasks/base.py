"""盲测任务基类：TaskSpec、experiment.json 组装、中性 objective 模板与 denylist 断言。

任务生成必须完全确定性（固定 seed，不依赖网络），并遵守"不把期望方法写入提示"的
独立性要求：objective 使用统一中性模板，不得点名任何候选方法。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from popper.core import ProtocolError, validate_spec, write_json

# 四个受控隐藏证据情形（与 autonomy-v1-protocol.json 一致）
CONTROLLED_EVIDENCE_CONDITIONS = (
    "positive_effect",
    "negative_effect",
    "near_zero_effect",
    "scope_boundary_or_counterexample",
)

# 与 protocol 定义一致的任务族
FAMILIES = ("tabular", "small_time_series", "small_vision")

# objective 不得出现的方法关键词（denylist 烟雾断言，独立人工审查仍为必须项）
OBJECTIVE_DENYLIST = (
    "gaussian", "gauss", "forest", "logistic", "regression", "polynomial",
    "degree", "lag", "imputation", "impute", "knn", "svc", "svm", "tree",
    "boosting", "mlp", "neural", "baseline", "candidate", "config",
    "linear", "quadratic", "sign", "noise",
)

# 统一中性研究问句模板（只描述数据构成，不点名方法）
OBJECTIVE_TEMPLATE = (
    "In this fixed data split, propose and test a falsifiable computational ML "
    "hypothesis. Compare registered interventions with the provided control under "
    "the frozen evaluation contract. Stop or add a discriminating control when "
    "evidence is insufficient. Give an evidence-bounded conclusion without claiming "
    "mechanistic validity, novelty, or general capability. The holdout split is "
    "reserved for final confirmation only."
)


def neutral_objective(name, *, n_train, n_dev, n_test, evaluator, direction):
    """生成中性研究问句：仅描述划分规模与评估契约，不泄露期望方法。"""
    metric_name = "accuracy" if evaluator == "binary-accuracy-v1" else "MSE"
    metric = ("higher accuracy" if direction == "max"
              else "lower error (MSE)")
    return (
        f"{OBJECTIVE_TEMPLATE} Dataset {name!r}: {n_train} training, {n_dev} development, "
        f"{n_test} held-out rows; {metric_name} with a pre-registered "
        f"meaningful-improvement threshold; objective is {metric}."
    )


def assert_objective_neutral(objective):
    """denylist 烟雾断言：objective 不得包含方法/实现关键词（词边界匹配）。"""
    import re
    lowered = objective.casefold()
    hits = [word for word in OBJECTIVE_DENYLIST
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", lowered)]
    if hits:
        raise ProtocolError(f"任务 objective 泄露方法关键词: {sorted(hits)}")


@dataclass(frozen=True)
class TaskSpec:
    """一个受控盲测任务的完整描述。build 负责生成 public/ 与 gold/ 模板。"""

    task_id: str
    family: str
    condition: str
    evaluator_id: str
    build: Callable[[Path, Path], None] = field(default=None, compare=False, repr=False)
    timeout_seconds: int = 60
    seeds: tuple = (11, 29, 47)

    def __post_init__(self):
        if self.family not in FAMILIES:
            raise ProtocolError(f"未知任务族: {self.family}")
        if self.condition not in CONTROLLED_EVIDENCE_CONDITIONS:
            raise ProtocolError(f"未知隐藏证据情形: {self.condition}")
        if self.evaluator_id not in {"mse-v1", "binary-accuracy-v1"}:
            raise ProtocolError(f"未知评估器: {self.evaluator_id}")


def write_experiment(public_dir, *, name, objective, entrypoint, code_files,
                     baseline, candidates, seeds, budget, timeout_seconds,
                     min_improvement, metric, provenance_files=(), analysis_slices=()):
    """组装并预检 experiment.json；目标目录必须已存在。"""
    assert_objective_neutral(objective)
    spec = {
        "name": name, "objective": objective, "entrypoint": entrypoint,
        "code_files": list(code_files), "train": "train.json", "dev": "dev.json",
        "test": "test.json", "baseline": baseline, "candidates": candidates,
        "seeds": list(seeds), "budget": budget, "timeout_seconds": timeout_seconds,
        "min_improvement": min_improvement, "metric": metric,
    }
    if provenance_files:
        spec["provenance_files"] = list(provenance_files)
    if analysis_slices:
        spec["analysis_slices"] = list(analysis_slices)
    validate_spec(spec)
    write_json(public_dir / "experiment.json", spec)
    return spec


def write_json_lines(path, rows):
    """确定性写数据文件：allow_nan=False，固定排序，无 BOM。"""
    write_json(path, rows)


def deterministic_rng(task_id, salt=0):
    """每个任务一个固定种子，保证跨运行逐字节一致。"""
    import hashlib
    import random
    seed = int.from_bytes(hashlib.sha256(f"{task_id}:{salt}".encode()).digest()[:8], "little")
    return random.Random(seed)


def ensure_json_ok(path, evaluator_id):
    """快速自检：数据文件必须能被 popper.core.dataset 接受。"""
    from popper.core import dataset
    rows = dataset(path, evaluator_id)
    return rows


def task_manifest(public_dir, gold_dir, spec, **extra):
    """写 gold/task-manifest.json：生成参数、隐藏条件、边界规则等（不进入 public/）。"""
    import shutil
    from popper.core import file_hash
    files = {name: file_hash(public_dir / name) for name in
             ("experiment.json", "train.json", "dev.json", "test.json")}
    manifest = {
        "schema_version": "1.0", "task_id": spec.task_id, "family": spec.family,
        "condition": spec.condition, "evaluator_id": spec.evaluator_id,
        "seeds": list(spec.seeds), "public_file_hashes": files,
    }
    manifest.update(extra)
    gold_dir.mkdir(parents=True, exist_ok=True)
    write_json(gold_dir / "task-manifest.json", manifest)
    # Refuse to publish a gold condition that the generated data do not
    # actually exhibit. Boundary tasks additionally verify the same candidate
    # on the hidden holdout split before the template can be used by a trial.
    from .validation import verify_condition
    verify_condition(spec.task_id, public_dir, gold_dir, spec,
                     boundary_rule=extra.get("boundary_rule"))


def copy_registered_inputs(public_dir, source_dir, names):
    """从现有示例复制已存在的输入文件（三 json / model.py / prepare_data.py 等）。"""
    import shutil
    for name in names:
        shutil.copyfile(source_dir / name, public_dir / name)
