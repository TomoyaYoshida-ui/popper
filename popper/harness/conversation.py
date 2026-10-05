"""Versioned conversation Harness protocol and compatibility adapters.

Conversation Harnesses produce text/tool proposals only. They never receive a
database handle or execute tools; Workstation remains the policy and storage
boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

from ..core import ProtocolError

CONVERSATION_HARNESS_PROTOCOL = "1.0"
KNOWN_CAPABILITIES = frozenset({
    "text", "streaming_text", "cooperative_cancel", "native_tools",
    "provider_usage",
})


@dataclass(frozen=True)
class ConversationHarnessDescriptor:
    name: str
    display_name: str
    protocol_version: str
    capabilities: frozenset[str]
    model: str | None = None

    def __post_init__(self):
        if not self.name or not self.display_name:
            raise ProtocolError("conversation harness 名称不能为空")
        if self.protocol_version != CONVERSATION_HARNESS_PROTOCOL:
            raise ProtocolError(
                f"不支持的 conversation harness protocol: {self.protocol_version}")
        unknown = set(self.capabilities) - KNOWN_CAPABILITIES
        if unknown or "text" not in self.capabilities:
            raise ProtocolError(f"conversation harness capabilities 不合法: {unknown}")

    def as_dict(self):
        result = asdict(self)
        result["capabilities"] = sorted(self.capabilities)
        return result


@runtime_checkable
class ConversationHarness(Protocol):
    descriptor: ConversationHarnessDescriptor

    def invoke(self, system, messages, *, cancel_event=None, on_delta=None):
        """Return text or a structured tool proposal without executing it."""


class CallableConversationHarness:
    """Compatibility boundary for legacy injected callables."""

    def __init__(self, target):
        if not callable(target):
            raise ProtocolError("conversation harness 必须可调用")
        self._target = target
        if hasattr(target, "context_tools"):
            self.context_tools = target.context_tools
        if hasattr(target, "context_request_limits"):
            self.context_request_limits = target.context_request_limits
        capabilities = {"text"}
        if getattr(target, "supports_cancellation", False):
            capabilities.update({"streaming_text", "cooperative_cancel"})
        self.descriptor = ConversationHarnessDescriptor(
            name="chat", display_name="Injected Chat Harness",
            protocol_version=CONVERSATION_HARNESS_PROTOCOL,
            capabilities=frozenset(capabilities),
            model=getattr(target, "model", None))

    def invoke(self, system, messages, *, cancel_event=None, on_delta=None):
        if "cooperative_cancel" in self.descriptor.capabilities:
            return self._target(
                system, messages, cancel_event=cancel_event, on_delta=on_delta)
        return self._target(system, messages)


def adapt_conversation_harness(value):
    if value is None:
        return None
    if isinstance(value, ConversationHarness):
        return value
    return CallableConversationHarness(value)
