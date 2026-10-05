"""Storage-neutral primitives for Popper's append-only event chains.

Domain stores own transactions, schemas, authorization and state replay.  This
module owns the one invariant they must share: canonical forward hash linking.
Keeping that boundary small avoids turning domain validation into a generic
lowest-common-denominator event store.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .core import digest


GENESIS_HASH = "0" * 64


def chained_body(fields: Mapping[str, Any], previous_hash: str, *,
                 previous_key: str = "previous_hash") -> tuple[dict[str, Any], str]:
    """Return the exact canonical event body and its content hash."""
    if not isinstance(previous_hash, str) or len(previous_hash) != 64:
        raise ValueError("previous hash must be a 64-character digest")
    if previous_key in fields:
        raise ValueError(f"event fields already contain {previous_key}")
    body = {**fields, previous_key: previous_hash}
    return body, digest(body)


def verify_hash_chain(records: Iterable[Any], body_for: Callable[[Any], Mapping[str, Any]],
                      previous_for: Callable[[Any], str], hash_for: Callable[[Any], str],
                      *, label: str = "事件链", previous_key: str = "previous_hash",
                      seq_for: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """Verify a global forward chain without interpreting domain transitions."""
    previous = GENESIS_HASH
    count = 0
    for record in records:
        recorded_previous = previous_for(record)
        body, expected_hash = chained_body(
            body_for(record), recorded_previous, previous_key=previous_key)
        del body  # The digest is the contract; callers may reconstruct separately.
        if recorded_previous != previous or expected_hash != hash_for(record):
            seq = seq_for(record) if seq_for is not None else count + 1
            return {"ok": False, "events": count,
                    "reason": f"{label}在 seq={seq} 损坏"}
        previous = hash_for(record)
        count += 1
    return {"ok": True, "events": count, "head": previous}
