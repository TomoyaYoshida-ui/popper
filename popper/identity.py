"""Platform-level entity identities.

This module is deliberately storage-agnostic.  It gives API and projection
layers one canonical vocabulary while historical experiment/research stores
continue to use their existing identifiers.  Legacy IDs are aliases, not data
that should be rewritten in place.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
import secrets
import uuid

from .core import ProtocolError


class EntityKind(str, Enum):
    ORGANIZATION = "organization"
    PROJECT = "project"
    WORKSPACE = "workspace"
    QUEST = "quest"
    SESSION = "session"
    TURN = "turn"
    TOOL_CALL = "tool_call"
    STUDY = "study"
    HYPOTHESIS = "hypothesis"
    DESIGN = "design"
    OBSERVATION = "observation"
    DECISION = "decision"
    CLAIM = "claim"
    EXPERIMENT_NODE = "experiment_node"
    REVISION = "revision"
    RUN = "run"
    JOB = "job"
    ARTIFACT = "artifact"
    EVALUATION = "evaluation"
    AUDIT_EVENT = "audit_event"
    PRICING_CATALOG = "pricing_catalog"


PREFIXES = {
    EntityKind.ORGANIZATION: "org",
    EntityKind.PROJECT: "prj",
    EntityKind.WORKSPACE: "wsp",
    EntityKind.QUEST: "qst",
    EntityKind.SESSION: "ses",
    EntityKind.TURN: "trn",
    EntityKind.TOOL_CALL: "tcl",
    EntityKind.STUDY: "std",
    EntityKind.HYPOTHESIS: "hyp",
    EntityKind.DESIGN: "dsg",
    EntityKind.OBSERVATION: "obs",
    EntityKind.DECISION: "dec",
    EntityKind.CLAIM: "clm",
    EntityKind.EXPERIMENT_NODE: "exp",
    EntityKind.REVISION: "rev",
    EntityKind.RUN: "run",
    EntityKind.JOB: "job",
    EntityKind.ARTIFACT: "art",
    EntityKind.EVALUATION: "evl",
    EntityKind.AUDIT_EVENT: "evt",
    EntityKind.PRICING_CATALOG: "prc",
}
KINDS_BY_PREFIX = {prefix: kind for kind, prefix in PREFIXES.items()}

_CANONICAL = re.compile(r"^(?P<prefix>[a-z]{3})_(?P<body>[a-z2-7]{26})$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LEGACY = (
    (re.compile(r"^S-[A-Za-z0-9][A-Za-z0-9._-]*$"), EntityKind.STUDY),
    (re.compile(r"^H-[A-Za-z0-9][A-Za-z0-9._-]*$"), EntityKind.HYPOTHESIS),
    (re.compile(r"^D-[A-Za-z0-9][A-Za-z0-9._-]*$"), EntityKind.DESIGN),
    (re.compile(r"^OBS-[A-Za-z0-9][A-Za-z0-9._-]*$"), EntityKind.OBSERVATION),
    (re.compile(r"^DEC-[A-Za-z0-9][A-Za-z0-9._-]*$"), EntityKind.DECISION),
    (re.compile(r"^REV-[A-Za-z0-9][A-Za-z0-9._-]*$"), EntityKind.REVISION),
    (re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9._-]*$"), EntityKind.RUN),
    (re.compile(r"^JOB-[A-Za-z0-9][A-Za-z0-9._-]*$"), EntityKind.JOB),
    (re.compile(r"^C[0-9]{3,}$"), EntityKind.CLAIM),
    (re.compile(r"^w_[0-9a-f]{12}$"), EntityKind.WORKSPACE),
    (re.compile(r"^q_[0-9a-f]{32}$"), EntityKind.QUEST),
)


def _coerce_kind(kind: EntityKind | str) -> EntityKind:
    try:
        return kind if isinstance(kind, EntityKind) else EntityKind(kind)
    except (TypeError, ValueError):
        raise ProtocolError(f"未知实体 kind: {kind!r}") from None


def _base32(value: bytes) -> str:
    return base64.b32encode(value).decode("ascii").rstrip("=").lower()


def new_id(kind: EntityKind | str) -> str:
    """Create a random canonical ID without consulting storage."""
    resolved = _coerce_kind(kind)
    return f"{PREFIXES[resolved]}_{_base32(secrets.token_bytes(16))}"


def content_id(kind: EntityKind | str, value) -> str:
    """Create a stable canonical ID from canonical JSON-compatible content."""
    resolved = _coerce_kind(kind)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProtocolError(f"内容不能生成稳定实体 ID: {error}") from None
    body = _base32(hashlib.sha256(encoded).digest()[:16])
    return f"{PREFIXES[resolved]}_{body}"


@dataclass(frozen=True)
class EntityRef:
    kind: EntityKind
    entity_id: str
    canonical: bool

    def as_dict(self) -> dict:
        return {"kind": self.kind.value, "id": self.entity_id}


def parse_id(value: str, expected_kind: EntityKind | str | None = None) -> EntityRef:
    """Parse a canonical ID or a supported historical alias.

    Bare UUID run IDs are intentionally ambiguous and require
    ``expected_kind=EntityKind.RUN``.  File paths are never accepted as IDs.
    """
    if not isinstance(value, str) or not value:
        raise ProtocolError("实体 ID 必须是非空字符串")
    expected = _coerce_kind(expected_kind) if expected_kind is not None else None

    match = _CANONICAL.fullmatch(value)
    if match:
        kind = KINDS_BY_PREFIX.get(match.group("prefix"))
        if kind is None:
            raise ProtocolError(f"未知实体 ID 前缀: {match.group('prefix')}")
        if expected is not None and kind != expected:
            raise ProtocolError(f"实体 kind 不匹配: 期望 {expected.value}，实际 {kind.value}")
        return EntityRef(kind, value, True)

    for pattern, kind in _LEGACY:
        if pattern.fullmatch(value):
            if expected is not None and kind != expected:
                raise ProtocolError(f"实体 kind 不匹配: 期望 {expected.value}，实际 {kind.value}")
            return EntityRef(kind, value, False)

    if _UUID.fullmatch(value):
        if expected != EntityKind.RUN:
            raise ProtocolError("历史 UUID ID 缺少 run kind 上下文")
        # uuid.UUID performs the final structural validation and normalization check.
        uuid.UUID(value)
        return EntityRef(EntityKind.RUN, value, False)

    if value == "primary" and expected == EntityKind.WORKSPACE:
        return EntityRef(EntityKind.WORKSPACE, value, False)

    raise ProtocolError(f"不支持的实体 ID: {value!r}")
