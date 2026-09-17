"""Evidence-bound model reflection; validation never mutates research state.

The controller owns measurements, effect sizes, budgets and hypothesis versions.
The model may explain development evidence and propose scientific text for an
untried registered control, but cannot supply measurements or alter experiments.
"""
from __future__ import annotations

import copy
import json
import math

from ..core import ProtocolError
from .actions import (ADD_CONTROL, REQUEST_CONFIRMATION,
                      REQUEST_SCOPE_BOUNDARY_CONFIRMATION, STOP)


_RESPONSE_FIELDS = {"action", "rationale", "alternative_explanation",
                    "next_hypothesis_id", "revision", "evidence_refs"}
_REVISION_TEXT = {"mechanism", "applicability"}
_REVISION_LISTS = {"predictions", "falsification", "alternatives"}
_OBSERVATION_FIELDS = ("observation_id", "run_id", "scorer_id", "value", "unit",
                       "uncertainty", "scope", "artifact_sha256", "selector",
                       "evaluator_service_hash", "trust")
_HYPOTHESIS_FIELDS = ("hypothesis_id", "status", "version", "parent_version",
                      "mechanism", "applicability", "predictions", "falsification",
                      "alternatives", "config", "design_id")


def _text(value, field, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ProtocolError(f"reflection {field} 必须是非空字符串，长度不超过 {limit}")
    return value.strip()


def _number(value, field, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (nonnegative and value < 0)):
        raise ProtocolError(f"reflection {field} 必须是有限{'非负' if nonnegative else ''}数值")
    return value


def _string_list(value, field, item_limit=2000, count_limit=16):
    if not isinstance(value, list) or not 1 <= len(value) <= count_limit:
        raise ProtocolError(f"reflection {field} 必须是包含 1–{count_limit} 项的非空数组")
    return [_text(item, field, item_limit) for item in value]


def _boundary_detected(effect, threshold, slice_effects):
    if abs(effect) >= threshold or not isinstance(slice_effects, list) or len(slice_effects) < 2:
        return False
    values = [item.get("effect") for item in slice_effects if isinstance(item, dict)]
    return (len(values) == len(slice_effects)
            and all(type(value) in (int, float) and math.isfinite(value) for value in values)
            and max(values) >= threshold and min(values) < threshold)


def _allowed_actions(effect, threshold, remaining, available, boundary=False):
    if effect >= threshold:
        # A supported development result is not a terminal scientific conclusion.
        # The only valid next action is the preregistered independent confirmation.
        return [REQUEST_CONFIRMATION]
    if boundary:
        return [REQUEST_SCOPE_BOUNDARY_CONFIRMATION]
    # One failed/near-zero candidate cannot justify a statement about the
    # remaining registered search space. With executable budget left, the
    # controller must run a discriminating control before it may stop.
    return [ADD_CONTROL] if remaining and available >= 1 else [STOP]


def _observation(row):
    if (not isinstance(row, dict) or not isinstance(row.get("scope"), str)
            or not (row["scope"] == "dev" or row["scope"].startswith("dev:slice:"))):
        raise ProtocolError("reflection 只能读取 dev 观察，不能读取 holdout/confirmation")
    _text(row.get("observation_id"), "observation_id", 256)
    _text(row.get("run_id"), "run_id", 256)
    _number(row.get("value"), "observation.value")
    if row.get("uncertainty") is not None:
        _number(row["uncertainty"], "observation.uncertainty", nonnegative=True)
    # Artifact paths, arbitrary metadata and execution logs never enter prompts.
    return {key: copy.deepcopy(row[key]) for key in _OBSERVATION_FIELDS if key in row}


def _hypothesis(row):
    _text(row.get("hypothesis_id"), "hypothesis_id", 256)
    version = row.get("version", 1)
    if type(version) is not int or version < 1:
        raise ProtocolError("reflection hypothesis.version 必须是正整数")
    result = {key: copy.deepcopy(row[key]) for key in _HYPOTHESIS_FIELDS if key in row}
    result["version"] = version
    return result


def build_reflection_context(context, baseline, observation, hypothesis_id):
    """Project authoritative study snapshots into a development-only model prompt.

    Both compared observations must already exist in the supplied study context.
    The two evidence IDs are mandatory references in the response. Other dev
    observations are background; they cannot substitute for this comparison.
    """
    if not all(isinstance(row, dict) for row in (context, baseline, observation)):
        raise ProtocolError("reflection context/baseline/observation 必须为对象")
    study = context.get("study", {})
    study_id = context.get("study_id") or study.get("study_id")
    if not study_id:
        # Older build_context snapshots omit study_id from the Study payload.
        study_id = baseline.get("study_id")
    _text(study_id, "study_id", 256)
    if any(row.get("study_id") != study_id for row in (baseline, observation)):
        raise ProtocolError("reflection 不允许引用其他 study 的观察")

    candidates = [row for row in context.get("candidates", [])
                  if isinstance(row, dict) and row.get("study_id", study_id) == study_id]
    current = [row for row in candidates if row.get("hypothesis_id") == hypothesis_id]
    if len(current) != 1:
        raise ProtocolError("reflection 当前 hypothesis_id 不存在或不唯一")
    local_observations = [row for row in context.get("observations", [])
                          if isinstance(row, dict) and row.get("study_id") == study_id
                          and isinstance(row.get("scope"), str)
                          and (row["scope"] == "dev" or row["scope"].startswith("dev:slice:"))]
    dev_by_id = {}
    for row in local_observations:
        projected = _observation(row)
        if projected["observation_id"] in dev_by_id:
            raise ProtocolError("reflection dev observation_id 不唯一")
        dev_by_id[projected["observation_id"]] = projected
    compared = [_observation(row) for row in (baseline, observation)]
    for row in compared:
        if dev_by_id.get(row["observation_id"]) != row:
            raise ProtocolError("reflection 比较值必须来自当前 study 的权威 dev 观察")
    if compared[0]["observation_id"] == compared[1]["observation_id"]:
        raise ProtocolError("reflection baseline 与 candidate 必须是不同观察")

    runs = {row["run_id"]: row for row in context.get("runs", [])
            if isinstance(row, dict) and row.get("study_id") == study_id}
    candidate_run = runs.get(observation["run_id"])
    if candidate_run and candidate_run.get("design_id") != current[0].get("design_id"):
        raise ProtocolError("reflection candidate 观察不属于当前 hypothesis 的 design")
    observed_designs = {runs[row["run_id"]].get("design_id") for row in local_observations
                        if row["run_id"] in runs}
    remaining = [_hypothesis(row) for row in candidates
                 if row.get("status") == "untested" and row.get("hypothesis_id") != hypothesis_id
                 and row.get("design_id") not in observed_designs]
    if len({row["hypothesis_id"] for row in remaining}) != len(remaining):
        raise ProtocolError("reflection 剩余 hypothesis_id 不唯一")

    metric = context.get("metric", {})
    direction = metric.get("direction")
    if direction not in {"min", "max"}:
        raise ProtocolError("reflection metric.direction 必须为 min/max")
    threshold = _number(context.get("min_meaningful_effect"), "threshold", nonnegative=True)
    available = _number(context.get("budget", {}).get("available"),
                        "budget.available", nonnegative=True)
    difference = observation["value"] - baseline["value"]
    effect = _number(-difference if direction == "min" else difference, "effect")
    slice_effects = []
    evidence_refs = [row["observation_id"] for row in compared]
    for scope in sorted({row["scope"] for row in local_observations
                         if row["scope"].startswith("dev:slice:") and
                         row.get("hypothesis_id") in {baseline["hypothesis_id"],
                                                      observation["hypothesis_id"]}}):
        control = next((row for row in local_observations
                        if row["scope"] == scope
                        and row.get("hypothesis_id") == baseline["hypothesis_id"]), None)
        candidate = next((row for row in local_observations
                          if row["scope"] == scope
                          and row.get("hypothesis_id") == observation["hypothesis_id"]), None)
        if control is None or candidate is None:
            continue
        raw = candidate["value"] - control["value"]
        slice_effect = -raw if direction == "min" else raw
        slice_effects.append({"scope": scope, "effect": slice_effect,
                              "baseline": _observation(control),
                              "candidate": _observation(candidate)})
        evidence_refs.extend([control["observation_id"], candidate["observation_id"]])
    boundary = _boundary_detected(effect, threshold, slice_effects)
    return {"study_id": study_id, "question": study.get("question", ""),
            "metric": {"name": metric.get("name"), "direction": direction},
            "threshold": threshold, "effect": effect, "budget_available": available,
            "baseline": compared[0], "candidate": compared[1],
            "hypothesis": _hypothesis(current[0]), "remaining_candidates": remaining,
            "observations": list(dev_by_id.values()),
            "slice_effects": slice_effects, "boundary_detected": boundary,
            "evidence_refs": evidence_refs,
            "allowed_actions": _allowed_actions(effect, threshold, remaining, available, boundary)}


def parse_reflection(response, context):
    """Validate model output against a built reflection context; return a fresh dict.

    An add_control revision must supply at least two predictions intended to
    distinguish the mechanism from the alternative explanation. This contract
    checks structure, not independent semantic validity of those predictions.
    Other scientific text fields are optional and retain their current values
    when omitted. Registered configs, measurements, IDs and versions are frozen.
    """
    if not isinstance(response, dict) or set(response) != _RESPONSE_FIELDS:
        raise ProtocolError("reflection 字段必须恰为 action/rationale/alternative_explanation/"
                            "next_hypothesis_id/revision/evidence_refs")
    try:
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ProtocolError("reflection 必须为有限 JSON 对象") from None
    if len(encoded.encode("utf-8")) > 64_000:
        raise ProtocolError("reflection 响应不能超过 64KB")

    effect = _number(context.get("effect"), "effect")
    threshold = _number(context.get("threshold"), "threshold", nonnegative=True)
    available = _number(context.get("budget_available"), "budget_available", nonnegative=True)
    remaining = context.get("remaining_candidates", [])
    boundary = _boundary_detected(effect, threshold, context.get("slice_effects"))
    if context.get("boundary_detected") is not boundary:
        raise ProtocolError("reflection boundary flag is inconsistent with slice evidence")
    allowed = _allowed_actions(effect, threshold, remaining, available, boundary)
    if context.get("allowed_actions") != allowed:
        raise ProtocolError("reflection 上下文动作与证据/预算不一致")
    action = response["action"]
    if not isinstance(action, str) or action not in allowed:
        raise ProtocolError("reflection action 不受当前证据或预算允许")

    next_id = response["next_hypothesis_id"]
    if action == ADD_CONTROL:
        valid_ids = {row["hypothesis_id"] for row in remaining
                     if row.get("status") == "untested"}
        if not isinstance(next_id, str) or next_id not in valid_ids:
            raise ProtocolError("reflection next_hypothesis_id 必须指向剩余未测候选")
    elif next_id is not None:
        raise ProtocolError("reflection 只有 add_control 可以指定 next_hypothesis_id")

    refs = response["evidence_refs"]
    if (not isinstance(refs, list) or not refs or len(refs) > 16
            or not all(isinstance(ref, str) and 0 < len(ref) <= 256 for ref in refs)
            or len(set(refs)) != len(refs)
            or set(refs) != set(context["evidence_refs"])):
        raise ProtocolError("reflection evidence_refs 必须精确引用 baseline 与 candidate 的 dev 证据")
    # Do not strip identifiers: even whitespace variants are unavailable IDs.
    normalized = {"action": action,
                  "rationale": _text(response["rationale"], "rationale"),
                  "alternative_explanation": _text(response["alternative_explanation"],
                                                   "alternative_explanation"),
                  "next_hypothesis_id": next_id, "revision": None,
                  "evidence_refs": list(context["evidence_refs"])}
    revision = response["revision"]
    if action == ADD_CONTROL and (not isinstance(revision, dict)
                                 or "predictions" not in revision):
        raise ProtocolError("reflection add_control 必须修订 predictions 并提供至少两个区分性预测")
    if revision is not None:
        if (action != ADD_CONTROL or not isinstance(revision, dict) or not revision
                or not set(revision) <= _REVISION_TEXT | _REVISION_LISTS):
            raise ProtocolError("reflection revision 只能为 add_control 候选的非空科学文本修订")
        normalized["revision"] = {
            key: (_text(value, f"revision.{key}") if key in _REVISION_TEXT else
                  _string_list(value, f"revision.{key}")) for key, value in revision.items()}
        if len({value.casefold() for value in normalized["revision"]["predictions"]}) < 2:
            raise ProtocolError("reflection add_control 必须提供至少两个区分性预测")
    return normalized
