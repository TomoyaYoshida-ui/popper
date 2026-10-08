"""Executable predictions are kernel contracts, never model-authored scores."""
from __future__ import annotations

import math
import re

from ..core import ProtocolError, canonical, digest, evaluator_pack

SCHEMA = "popper.prediction/v2"
FIELDS = frozenset({"schema", "evaluator_id", "metric", "unit", "direction",
                    "comparison", "threshold", "scope", "operator"})


def build_prediction(evaluator_id, threshold, *, scope="registered_scope", operator="gte"):
    metric = evaluator_pack(evaluator_id).primary_metric()
    result = {"schema": SCHEMA, "evaluator_id": evaluator_id, "metric": metric.name,
              "unit": metric.unit or metric.name, "direction": metric.direction,
              "comparison": "candidate_vs_frozen_control", "threshold": threshold,
              "scope": scope, "operator": operator}
    return validate_prediction(result)


def validate_prediction(value, *, evaluator_id=None, threshold=None, scope=None):
    if not isinstance(value, dict) or set(value) != FIELDS or value.get("schema") != SCHEMA:
        raise ProtocolError("预测契约字段或版本不合法")
    metric = evaluator_pack(value["evaluator_id"]).primary_metric()
    if (value["metric"] != metric.name or value["unit"] != (metric.unit or metric.name)
            or value["direction"] != metric.direction):
        raise ProtocolError("预测指标、单位或方向与评估器不一致")
    if (type(value["threshold"]) not in (int, float) or not math.isfinite(value["threshold"])
            or value["threshold"] < 0 or value["operator"] not in {"gte", "lt"}
            or value["comparison"] != "candidate_vs_frozen_control"
            or not isinstance(value["scope"], str) or not value["scope"].strip()):
        raise ProtocolError("预测比较、阈值或范围不合法")
    for name, expected in (("evaluator_id", evaluator_id), ("threshold", threshold), ("scope", scope)):
        if expected is not None and value[name] != expected:
            raise ProtocolError(f"预测契约与冻结研究不一致: {name}")
    return dict(value)


def render_prediction(value):
    value = validate_prediction(value)
    comparison = "达到或超过" if value["operator"] == "gte" else "低于"
    return (f"范围 {value['scope']} 内，候选相对冻结对照的 {value['metric']} "
            f"方向统一效应{comparison} {value['threshold']:.8g} {value['unit']}。")


def validate_prediction_text(text, evaluator_id):
    """Reject known metric substitutions in legacy text; do not claim semantic verification."""
    if not isinstance(text, str):
        raise ProtocolError("预测必须是文本")
    metric = evaluator_pack(evaluator_id).primary_metric().name
    if metric != "accuracy" and re.search(r"准确率|accuracy|\bacc\b", text, re.I):
        raise ProtocolError("预测文本的准确率与冻结指标不一致")
    if metric not in {"f1", "macro_f1"} and re.search(r"\bf1\b|宏平均", text, re.I):
        raise ProtocolError("预测文本的 F1 与冻结指标不一致")


def evaluate_prediction(value, effect=None, *, execution_succeeded=True, evidence_sufficient=True,
                        statistical_evidence=None, repeat_unit="train_seed"):
    value = validate_prediction(value)
    if not execution_succeeded or effect is None:
        status = "not_evaluated"
    elif type(effect) not in (int, float) or not math.isfinite(effect):
        raise ProtocolError("预测效应必须是有限数")
    elif not evidence_sufficient:
        status = "inconclusive"
    else:
        met = effect >= value["threshold"]
        status = "met" if (met if value["operator"] == "gte" else not met) else "not_met"
    statistical = (repeat_unit == "analysis_unit" and isinstance(statistical_evidence, dict)
                   and statistical_evidence.get("statistical_claim") is True)
    return {"schema": "popper.prediction-result/v2", "contract_sha256": digest(value),
            "status": status, "execution_succeeded": execution_succeeded,
            "descriptive_threshold_met": None if status in {"inconclusive", "not_evaluated"}
            else effect >= value["threshold"], "statistical_evidence_sufficient": statistical,
            "repeat_unit": repeat_unit, "mechanism_status": "unverified",
            "oriented_effect": effect, "prediction": value}


def research_contract(manifest):
    """Stable authorization identity excludes growing entity and event versions."""
    keys = ("study_id", "family_id", "input_identity", "scorer_id", "scorer_hash",
            "metric", "min_meaningful_effect", "repeat_unit", "repeat_values", "seeds",
            "analysis_slices", "budget_cap", "prediction_contract")
    return digest({key: manifest.get(key) for key in keys})


def validate_design_prediction(design):
    value = design.get("prediction_contract")
    if value is None:
        return None
    validate_prediction(value, evaluator_id=design["scorer_id"],
                        threshold=design["min_meaningful_effect"])
    if value["metric"] != design["metric"] or value["direction"] != design["metric_direction"]:
        raise ProtocolError("实验设计与预测契约不一致")
    return value
