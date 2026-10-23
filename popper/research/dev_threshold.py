"""开发集阈值只选择确认候选，不签发科学支持。"""
from __future__ import annotations

from ..core import ProtocolError
from .tree_search import representative_observation


def development_delta(baseline, candidate, direction):
    if direction == "min":
        return baseline - candidate
    if direction == "max":
        return candidate - baseline
    raise ProtocolError("metric.direction 必须为 min/max")


def first_candidate_past_dev_threshold(manifest, observations):
    """返回第一个开发代表观察达到预注册阈值的候选。没有则返回 None。"""
    direction = manifest["metric"]["direction"]
    threshold = manifest["min_meaningful_effect"]
    control_rows = [row for row in observations
                    if row.get("hypothesis_id") == manifest["control_hypothesis_id"]]
    baseline = representative_observation(control_rows, direction)
    if baseline is None or baseline.get("value") is None:
        return None
    for hypothesis_id in manifest["candidate_hypothesis_ids"]:
        rows = [row for row in observations if row.get("hypothesis_id") == hypothesis_id]
        chosen = representative_observation(rows, direction)
        if chosen is None or chosen.get("value") is None:
            continue
        if development_delta(baseline["value"], chosen["value"], direction) >= threshold:
            return hypothesis_id
    return None
