"""Authorization gates for confirmation claims and scientific evidence writes.

Observation/run writers remain capability-issued by ResearchStore. Stronger
scientific mutations require an explicit context:

- ``authorize_confirmation`` — ``claims.confirm`` / ``confirm_signed``
- ``authorize_evidence_write`` — ``record_judgment`` and conclusive hypothesis
  statuses (supported / contradicted)

Adaptive worker scheduling may stay local; publishing scores/judgments must use
``scientific_execution`` under ``authorize_evidence_write``.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from ..core import ProtocolError

_CONFIRMATION_AUTHORIZED: ContextVar[bool] = ContextVar(
    "popper_confirmation_authorized", default=False)
_EVIDENCE_AUTHORIZED: ContextVar[bool] = ContextVar(
    "popper_evidence_write_authorized", default=False)

REFUSAL = (
    "claims.confirm 只能经 scientific_authority.authorize_confirmation；"
    "禁止未授权旁路消费确认集"
)
EVIDENCE_REFUSAL = (
    "record_judgment / 终态假设状态只能经 "
    "scientific_authority.authorize_evidence_write；禁止未授权旁路写科学结论"
)


def confirmation_authorized() -> bool:
    return bool(_CONFIRMATION_AUTHORIZED.get())


def evidence_write_authorized() -> bool:
    return bool(_EVIDENCE_AUTHORIZED.get())


def require_confirmation_authorized() -> None:
    if not confirmation_authorized():
        raise ProtocolError(REFUSAL)


def require_evidence_write_authorized() -> None:
    if not evidence_write_authorized():
        raise ProtocolError(EVIDENCE_REFUSAL)


def _require_actor(actor: str, label: str) -> None:
    if not isinstance(actor, str) or not actor.strip():
        raise ProtocolError(f"{label} 需要非空 actor")


@contextmanager
def authorize_confirmation(*, actor: str):
    """Grant permission to publish confirmation claims."""
    _require_actor(actor, "authorize_confirmation")
    token = _CONFIRMATION_AUTHORIZED.set(True)
    # Confirmation implies evidence writes in the same region.
    evidence = _EVIDENCE_AUTHORIZED.set(True)
    try:
        yield
    finally:
        _EVIDENCE_AUTHORIZED.reset(evidence)
        _CONFIRMATION_AUTHORIZED.reset(token)


@contextmanager
def authorize_evidence_write(*, actor: str):
    """Grant permission to record judgments and conclusive hypothesis statuses."""
    _require_actor(actor, "authorize_evidence_write")
    token = _EVIDENCE_AUTHORIZED.set(True)
    try:
        yield
    finally:
        _EVIDENCE_AUTHORIZED.reset(token)
