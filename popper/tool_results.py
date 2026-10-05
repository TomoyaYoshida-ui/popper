"""Lossless scientific tool summaries with explicitly addressable large details.

Only known presentation/source/log fields are lifted out of provider context.
Unknown fields and scientific observations are retained; the context assembler,
not this module, decides whether the resulting mandatory group fits its limit.
"""
from __future__ import annotations

import hashlib
import re

from .core import ProtocolError, canonical
from .task_context import redact


DETAIL_BYTES = 4096
_DETAIL_FIELDS = frozenset({
    "diff", "content", "replacement", "source", "source_code", "code",
    "stdout", "stderr", "log", "logs", "traceback", "stack_trace",
    "html", "markdown", "rendered", "raw_response", "raw_reply",
})
_SCIENTIFIC_FIELDS = frozenset({"observation", "observations"})


def _pointer(path):
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1")
                           for part in path) if path else ""


def _detail(value, call_id, path):
    body = canonical(value)
    return {"details_reference": {"source_call_id": call_id,
                                  "field_path": _pointer(path),
                                  "bytes": len(body.encode("utf-8")),
                                  "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()},
            "notice": "Large source/log detail is stored separately; this is not a summary or execution result."}


def summarize_tool_result(result, source_call_id):
    """Return redacted structure, retaining identities, versions and metrics.

    Details use JSON Pointer into the immutable result of the originating call.
    There is no partial JSON, numeric rounding, observation truncation or guessed
    relation. A missing/oversized mandatory field remains mandatory.
    """
    if not isinstance(source_call_id, str) or not source_call_id:
        raise ProtocolError("tool result source_call_id 不能为空")
    safe = redact(result)

    def visit(value, path=(), scientific=False):
        if isinstance(value, dict):
            scientific = scientific or "observation_id" in value or value.get("kind") == "observation"
            output = {}
            for key, item in value.items():
                protected = scientific or key in _SCIENTIFIC_FIELDS
                if (not protected and key in _DETAIL_FIELDS
                        and len(canonical(item).encode("utf-8")) > DETAIL_BYTES):
                    output[key] = _detail(item, source_call_id, (*path, key))
                else:
                    output[key] = visit(item, (*path, key), protected)
            return output
        if isinstance(value, list):
            return [visit(item, (*path, index), scientific)
                    for index, item in enumerate(value)]
        return value

    if isinstance(safe, str) and len(canonical(safe).encode("utf-8")) > DETAIL_BYTES:
        return _detail(safe, source_call_id, ())
    return visit(safe)


def read_detail(result, field_path=None):
    """Read one stored result field; disclosure policy belongs to the caller."""
    safe = redact(result)
    if field_path in (None, ""):
        return safe
    if not isinstance(field_path, str) or not field_path.startswith("/"):
        raise ProtocolError("tool detail field_path 必须是 JSON Pointer")
    value = safe
    for part in field_path[1:].split("/"):
        if re.search(r"~(?![01])", part):
            raise ProtocolError("tool detail JSON Pointer 转义不合法")
        key = part.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, list):
                if not key.isdigit() or (len(key) > 1 and key.startswith("0")):
                    raise ValueError
                value = value[int(key)]
            elif isinstance(value, dict):
                value = value[key]
            else:
                raise ValueError
        except (KeyError, IndexError, ValueError):
            raise ProtocolError("tool detail 引用不存在") from None
    return value
