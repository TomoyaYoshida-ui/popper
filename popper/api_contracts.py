"""Stable API v1 response contracts.

Legacy workstation endpoints intentionally keep their historical response
shape.  New `/api/v1/*` endpoints use these envelopes so clients never need to
branch on translated exception text.
"""
from __future__ import annotations

import base64
from enum import Enum
import secrets

from .core import ProtocolError


API_SCHEMA_VERSION = "1.0"


class ErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    VERSION_CONFLICT = "version_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_TRANSITION = "invalid_transition"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTEGRITY_FAILED = "integrity_failed"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    EXECUTION_FAILED = "execution_failed"


def new_trace_id() -> str:
    body = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=").lower()
    return "trc_" + body


def success(data, *, trace_id: str, event_cursor: int | None = None) -> dict:
    _trace(trace_id)
    meta = {"trace_id": trace_id}
    if event_cursor is not None:
        if type(event_cursor) is not int or event_cursor < 0:
            raise ProtocolError("event_cursor 必须是非负整数")
        meta["event_cursor"] = event_cursor
    return {"schema_version": API_SCHEMA_VERSION, "ok": True,
            "data": data, "meta": meta}


def failure(code: ErrorCode | str, message: str, *, trace_id: str,
            details: dict | None = None, retryable: bool = False) -> dict:
    _trace(trace_id)
    try:
        resolved = code if isinstance(code, ErrorCode) else ErrorCode(code)
    except (TypeError, ValueError):
        raise ProtocolError(f"未知 API 错误码: {code!r}") from None
    if not isinstance(message, str) or not message.strip():
        raise ProtocolError("API 错误 message 不能为空")
    if details is not None and not isinstance(details, dict):
        raise ProtocolError("API 错误 details 必须是对象")
    return {
        "schema_version": API_SCHEMA_VERSION,
        "ok": False,
        "error": {
            "code": resolved.value,
            "message": message,
            "details": details or {},
            "retryable": bool(retryable),
        },
        "meta": {"trace_id": trace_id},
    }


def _trace(value: str) -> None:
    if not isinstance(value, str) or not value.startswith("trc_") or len(value) < 8:
        raise ProtocolError("trace_id 格式不合法")

