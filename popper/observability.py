"""Standard-library observability layer for Popper agent/LLM calls (V1.5 BYOK-ready).

Spans are appended as JSONL under the run directory so they can be consumed locally
with zero third-party dependencies. A later V2 exporter can stream the same lines into
a Langfuse backend (langfuse.Client().start_as_current_span / .tag()); nothing here
requires or enforces that install.
"""
from __future__ import annotations

import json
from pathlib import Path

from .core import ProtocolError

try:  # optional backend; never installed by popper and always falls back
    import langfuse  # noqa: F401
    _LANGFUSE_AVAILABLE = True
except ImportError:
    _LANGFUSE_AVAILABLE = False


def _sensitive_field(name):
    """True when a span field name may carry key/credentials."""
    lowered = str(name).lower()
    return lowered == "authorization" or "key" in lowered or "secret" in lowered


class Tracer:
    """Append-only recorder of agent/LLM observability events, degrading locally if
    no backend is configured. Never stores keys or credentials."""

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.obs_dir = self.run_dir / ".popper-obs"
        self.events_path = self.obs_dir / "events.jsonl"
        self.obs_dir.mkdir(parents=True, exist_ok=True)

    def trace(self, span):
        if not isinstance(span, dict):
            raise ProtocolError("span 必须是 dict")
        for name in span:
            if _sensitive_field(name):
                raise ProtocolError(f"span 禁止携带凭据字段: {name}")
        line = json.dumps(span, ensure_ascii=False, allow_nan=False) + "\n"
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return span

    def spans(self):
        if not self.events_path.is_file():
            return []
        spans = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                spans.append(json.loads(line))
        return spans

    def privacy_scan(self):
        """True when every registered span is free of sensitive fields."""
        for span in self.spans():
            if any(_sensitive_field(name) for name in span):
                return False
        return True