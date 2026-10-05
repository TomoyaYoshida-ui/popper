"""Application-owned Agent runtime contract, separate from proposal Harnesses.

These records describe a remote runtime, not scientific outcomes. A runtime
session becoming idle, or accepting a prompt/abort, is never evidence that a
Popper Job completed. The host owns persistence, permission, budgets and tool
execution; implementations must not resend an uncertain write automatically.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Iterator, Protocol, runtime_checkable

from ..core import ProtocolError

AGENT_RUNTIME_PROTOCOL = "1.0"


def _identifier(value, name):
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(char) < 32 for char in value)):
        raise ProtocolError(f"Agent runtime {name} 不合法")
    return value


@dataclass(frozen=True)
class RuntimeDescriptor:
    name: str
    protocol_version: str
    expected_version: str
    event_replay_supported: bool = False

    def __post_init__(self):
        _identifier(self.name, "name")
        _identifier(self.expected_version, "expected_version")
        if self.protocol_version != AGENT_RUNTIME_PROTOCOL:
            raise ProtocolError("不支持的 Agent runtime protocol")

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class RuntimeBinding:
    project_id: str
    session_id: str
    directory: str
    runtime_session_id: str
    native_project_id: str

    def __post_init__(self):
        for name in ("project_id", "session_id", "runtime_session_id", "native_project_id"):
            _identifier(getattr(self, name), name)
        if (not isinstance(self.directory, str) or not self.directory
                or len(self.directory) > 32768 or "\x00" in self.directory):
            raise ProtocolError("Agent runtime directory 不合法")

    @property
    def sha256(self):
        """Fingerprint to compare with the host's persisted binding, not a MAC."""
        data = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def as_dict(self):
        return {**asdict(self), "sha256": self.sha256}


@dataclass(frozen=True)
class RuntimeReceipt:
    binding: RuntimeBinding
    message_id: str
    delivery: str = "accepted"


@dataclass(frozen=True)
class RuntimeCancellation:
    binding: RuntimeBinding
    abort_requested: bool


@dataclass(frozen=True)
class RuntimeCursor:
    """Bounded deduplication checkpoint, explicitly not an upstream replay token."""
    last_event_id: str | None = None
    recent_events: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if self.last_event_id is not None:
            _identifier(self.last_event_id, "last_event_id")
        if len(self.recent_events) > 256:
            raise ProtocolError("Agent runtime cursor 过大")
        entries = []
        for entry in self.recent_events:
            if (not isinstance(entry, (tuple, list)) or len(entry) != 2
                    or not isinstance(entry[0], str) or len(entry[0]) > 600
                    or not isinstance(entry[1], str) or len(entry[1]) != 64
                    or any(c not in "0123456789abcdef" for c in entry[1])):
                raise ProtocolError("Agent runtime cursor 不合法")
            entries.append(tuple(entry))
        object.__setattr__(self, "recent_events", tuple(entries))


@dataclass(frozen=True)
class RuntimeEvent:
    binding: RuntimeBinding
    event_id: str
    type: str
    properties: dict
    cursor: RuntimeCursor


@dataclass(frozen=True)
class RuntimeSnapshot:
    binding: RuntimeBinding
    status: dict
    messages: tuple[dict, ...]
    history_complete: bool
    event_replay_supported: bool = False


class RuntimeError(ProtocolError):
    def __init__(self, message, *, code, binding=None, operation=None, message_id=None):
        super().__init__(message)
        self.code = code
        self.binding = binding
        self.operation = operation
        self.message_id = message_id


class RuntimeDeliveryUnknown(RuntimeError):
    """The host must persist this barrier and reconcile; never auto-retry."""


class RuntimeStreamError(RuntimeError):
    def __init__(self, message, *, code, binding, cursor):
        super().__init__(message, code=code, binding=binding, operation="events")
        self.cursor = cursor
        self.requires_reconcile = True


@runtime_checkable
class AgentRuntime(Protocol):
    descriptor: RuntimeDescriptor

    def start(self, project_id, session_id, directory, prompt, message_id,
              runtime_session_id=None, *, system=None, model=None, agent=None,
              tools=None) -> RuntimeReceipt:
        """Accept one prompt; a receipt does not imply successful execution."""

    def events(self, binding: RuntimeBinding, *, cursor=None,
               cancel_event=None) -> Iterator[RuntimeEvent]:
        """Read events once. The host reconciles after any stream gap."""

    def cancel(self, binding: RuntimeBinding) -> RuntimeCancellation:
        """Request abort without inventing a terminal state."""

    def resume(self, binding: RuntimeBinding, prompt, message_id, **kwargs) -> RuntimeReceipt:
        """Explicit new prompt after checking the existing runtime session."""

    def reconcile(self, binding: RuntimeBinding, *, limit=500) -> RuntimeSnapshot:
        """Read native messages/status without sending prompts or executing tools."""
