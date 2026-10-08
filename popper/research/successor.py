"""预测失败后的后继提案。不降低父阈值，不把父确认集哈希写进提案。"""
from __future__ import annotations

import math
from pathlib import Path

from ..core import ProtocolError, digest, read_json, write_json

SCHEMA = "popper.successor-proposal/v1"
_PROPOSAL_KEYS = frozenset({
    "schema", "surviving_explanation_id", "observation_refs",
    "parent_threshold", "parent_question",
})
_REVISED_KEYS = _PROPOSAL_KEYS | {"revised_mechanism"}


def successor_for_failure(*, explanations, tested_explanation_id, observation_refs,
                          parent_threshold, parent_question, parent_holdout_sha256,
                          revised_mechanism=None, prediction_result=None):
    if not isinstance(explanations, list) or len(explanations) != 2:
        raise ProtocolError("问题书必须有两个预测不同的解释")
    ids = [item["id"] for item in explanations]
    if tested_explanation_id not in ids:
        raise ProtocolError("下一步实验必须指向一个竞争解释")
    surviving = next(item for item in ids if item != tested_explanation_id)
    if (not isinstance(observation_refs, (list, tuple)) or len(observation_refs) != 2
            or any(not isinstance(ref, str) or not ref for ref in observation_refs)):
        raise ProtocolError("后继提案必须引用基线与候选两条 Observation")
    if isinstance(parent_threshold, bool) or not isinstance(parent_threshold, (int, float)):
        raise ProtocolError("后继提案必须带上父阈值")
    if not isinstance(parent_question, str) or not parent_question.strip():
        raise ProtocolError("后继提案必须带上父问题")
    proposal = {
        "schema": SCHEMA,
        "surviving_explanation_id": surviving,
        "observation_refs": list(observation_refs),
        "parent_threshold": parent_threshold,
        "parent_question": parent_question,
    }
    if prediction_result is not None:
        proposal.update(schema="popper.successor-proposal/v2",
                        surviving_explanation_id=None, mechanism_status="unverified",
                        tested_explanation_id=tested_explanation_id,
                        prediction_result=prediction_result)
    if revised_mechanism is not None:
        from .agenda import accept_revised_mechanism
        proposal["revised_mechanism"] = accept_revised_mechanism(
            revised_mechanism, explanations=explanations,
            parent_question=parent_question,
            parent_holdout_sha256=parent_holdout_sha256)
    if parent_holdout_sha256 and parent_holdout_sha256 in _flat(proposal):
        raise ProtocolError("后继提案不能包含父确认集哈希")
    return proposal


def reject_lower_threshold(parent_threshold, new_threshold):
    if not _finite_number(parent_threshold) or not _finite_number(new_threshold):
        raise ProtocolError("后继提案必须带上父阈值")
    if new_threshold < parent_threshold:
        raise ProtocolError("后继研究不能降低最小有意义改善")


def reject_reused_holdout(parent_holdout_sha256, new_holdout_sha256):
    if parent_holdout_sha256 == new_holdout_sha256:
        raise ProtocolError("后继研究不能复用父确认集")


def confirmation_row_set_sha256(rows):
    """确认集行集合的稳定哈希。行相同则哈希相同，不依赖文件字节。"""
    if not isinstance(rows, list) or not rows:
        raise ProtocolError("确认集行集合无法哈希")

    def sort_key(row):
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            return (0, row["id"])
        from ..core import canonical
        return (1, canonical(row))

    return digest(sorted(rows, key=sort_key))


def read_successor_proposal(run_dir):
    """父运行目录没有后继提案时返回 None。有文件但读不懂就拒绝。"""
    path = Path(run_dir) / "successor-proposal.json"
    if not path.is_file():
        return None
    proposal = read_json(path)
    if isinstance(proposal, dict) and proposal.get("schema") == "popper.successor-proposal/v2":
        if (proposal.get("surviving_explanation_id") is not None
                or proposal.get("mechanism_status") != "unverified"
                or not isinstance(proposal.get("prediction_result"), dict)
                or not isinstance(proposal.get("observation_refs"), list)
                or len(proposal["observation_refs"]) != 2
                or not _finite_number(proposal.get("parent_threshold"))):
            raise ProtocolError("后继提案无法读取")
        return proposal
    refs = proposal.get("observation_refs") if isinstance(proposal, dict) else None
    if (not isinstance(proposal, dict) or set(proposal) not in (_PROPOSAL_KEYS, _REVISED_KEYS)
            or proposal["schema"] != SCHEMA
            or not isinstance(proposal["surviving_explanation_id"], str)
            or not proposal["surviving_explanation_id"].strip()
            or not isinstance(proposal["parent_question"], str)
            or not proposal["parent_question"].strip()
            or not isinstance(refs, list) or len(refs) != 2
            or any(not isinstance(item, str) or not item for item in refs)
            or not _finite_number(proposal["parent_threshold"])
            or ("revised_mechanism" in proposal and (
                not isinstance(proposal["revised_mechanism"], str)
                or not proposal["revised_mechanism"].strip()))):
        raise ProtocolError("后继提案无法读取")
    return proposal


def _finite_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def write_successor_proposal(run_dir, proposal):
    path = run_dir / "successor-proposal.json"
    if path.is_file():
        existing = read_json(path)
        if existing != proposal:
            raise ProtocolError("后继提案已存在且内容不同")
        return existing
    write_json(path, proposal)
    return proposal


def _flat(value):
    if isinstance(value, dict):
        return " ".join(_flat(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flat(item) for item in value)
    return str(value)
