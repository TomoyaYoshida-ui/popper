"""Sole authorization gate for consuming confirmation claims.

Observation/run writers remain capability-issued by ResearchStore. Confirmation
claims are stronger: only code that enters ``authorize_confirmation`` may call
``store.claims.confirm`` / ``confirm_signed``. Adaptive and legacy callers must
not bypass this context.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from ..core import ProtocolError

_CONFIRMATION_AUTHORIZED: ContextVar[bool] = ContextVar(
    "popper_confirmation_authorized", default=False)

REFUSAL = (
    "claims.confirm 只能经 scientific_authority.authorize_confirmation；"
    "禁止未授权旁路消费确认集"
)


def confirmation_authorized() -> bool:
    return bool(_CONFIRMATION_AUTHORIZED.get())


def require_confirmation_authorized() -> None:
    if not confirmation_authorized():
        raise ProtocolError(REFUSAL)


@contextmanager
def authorize_confirmation(*, actor: str):
    """Grant a nested region permission to publish confirmation claims.

    ``actor`` is required for audit clarity at the call site; the store still
    records the Decision actor chosen by the caller.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ProtocolError("authorize_confirmation 需要非空 actor")
    token = _CONFIRMATION_AUTHORIZED.set(True)
    try:
        yield
    finally:
        _CONFIRMATION_AUTHORIZED.reset(token)
