"""声明式任务契约。

只编译已经在指标登记表里的评估器。计分函数直接调用手写域包，不另写一套公式。
"""
from __future__ import annotations

from ..core import ProtocolError, digest, evaluator_pack, score
from ..domains.protocol import IMPLEMENTED_SHAPES, REPEAT_UNITS
from .metrics import require_metric

CONTRACT_KEYS = frozenset({
    "target_column", "task_shape", "metric", "repeat_unit", "split_constraints", "id_column",
})
SPLIT_KEYS = frozenset({"group_by", "no_overlap"})


def _validate(contract):
    if not isinstance(contract, dict) or set(contract) != CONTRACT_KEYS:
        raise ProtocolError("任务契约字段不完整")
    if not isinstance(contract["target_column"], str) or not contract["target_column"]:
        raise ProtocolError("目标列不在数据卡内")
    if not isinstance(contract["id_column"], str) or not contract["id_column"]:
        raise ProtocolError("id 列必须是无缺失的字符串或整数")
    if contract["task_shape"] not in IMPLEMENTED_SHAPES:
        raise ProtocolError("任务形状不在已实现集合内")
    entry = require_metric(contract["metric"])
    if entry["task_shape"] != contract["task_shape"]:
        raise ProtocolError("指标与任务形状不兼容")
    if contract["repeat_unit"] not in REPEAT_UNITS:
        raise ProtocolError("重复单位不在受控词汇表内")
    if contract["task_shape"] == "aligned_prediction" and contract["repeat_unit"] != "train_seed":
        raise ProtocolError("重复单位与任务形状不兼容")
    constraints = contract["split_constraints"]
    if not isinstance(constraints, dict) or set(constraints) != SPLIT_KEYS:
        raise ProtocolError("切分约束无法机器判定")
    if not isinstance(constraints["group_by"], str) or constraints["no_overlap"] is not True:
        raise ProtocolError("切分约束无法机器判定")
    return entry


class CompiledContract:
    def __init__(self, contract, source):
        self.contract = contract
        self.source_evaluator_id = source["evaluator_id"]
        self.metric = dict(source["metric"])
        self.evaluator_id = "contract-" + digest(contract)[:24]
        self.evaluator_hash = digest({
            "contract": contract,
            "source_evaluator_id": self.source_evaluator_id,
            "source_evaluator_hash": source["evaluator_hash"],
        })

    def score(self, rows, predictions):
        return score(rows, predictions, self.source_evaluator_id)

    def validate_rows(self, rows, split):
        return evaluator_pack(self.source_evaluator_id).validate_rows(rows, split)

    def validate_splits(self, sets):
        return evaluator_pack(self.source_evaluator_id).validate_splits(sets)


def compile_contract(contract):
    return CompiledContract(contract, _validate(contract))


def inspect_contract(contract):
    compiled = compile_contract(contract)
    return {
        "evaluator_id": compiled.evaluator_id,
        "evaluator_hash": compiled.evaluator_hash,
        "source_evaluator_id": compiled.source_evaluator_id,
        "metric": compiled.metric,
    }
