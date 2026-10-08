"""A bounded logical operation spanning network retries and caller corrections.

This module owns only attempt identity, a shared deadline and cancellation.
Transport send markers and usage settlement remain in RequestLifecycle and
ModelRequestBudget respectively.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
import uuid

from ..core import ProtocolError
from ..request_lifecycle import RequestFailure, RequestLimits


class OperationCancellation:
    """The operation stays cancelled if any participating caller is cancelled."""

    def __init__(self, event=None):
        self._events = [event or threading.Event()]
        self._lock = threading.Lock()

    def bind(self, event):
        if event is not None:
            with self._lock:
                if all(event is not existing for existing in self._events):
                    self._events.append(event)

    def is_set(self):
        with self._lock:
            return any(event.is_set() for event in self._events)


class LogicalOperation:
    """One operation permits at most two dispatch attempts across all callers."""

    def __init__(self, key=None, *, max_attempts=2, limits=None, cancel_event=None):
        if key is not None and (not isinstance(key, str) or not key or len(key) > 256):
            raise ProtocolError("逻辑请求 ID 不合法")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
            raise ProtocolError("逻辑请求最多允许两次尝试")
        self.key = key or uuid.uuid4().hex
        self.max_attempts = max_attempts
        self.limits = limits or RequestLimits.from_env()
        self.started = time.monotonic()
        self.cancel = OperationCancellation(cancel_event)
        self.attempts = 0
        self.last_failure = None
        self._terminal_failure = None
        self._closed = False
        self._lock = threading.Lock()

    @property
    def deadline(self):
        return self.started + self.limits.total

    @property
    def remaining_attempts(self):
        return max(0, self.max_attempts - self.attempts)

    def check(self):
        if self.cancel.is_set():
            raise self.failure("cancelled")
        if time.monotonic() >= self.deadline:
            raise self.failure("total_timeout")
        if self._closed:
            raise ProtocolError("逻辑请求上下文已结束")
        if self._terminal_failure is not None:
            raise self._terminal_failure

    def failure(self, category):
        error = RequestFailure(category, sent=False)
        error.logical_request_id = self.key
        error.attempt_count = self.attempts
        return error

    def claim_attempt(self):
        with self._lock:
            self.check()
            if self.remaining_attempts == 0:
                raise self.failure("attempt_budget_exhausted")
            self.attempts += 1
            return self.attempts

    def record_failure(self, error, *, retryable):
        self.last_failure = error
        if not retryable:
            self._terminal_failure = error

    def record_success(self):
        self.last_failure = None

    def close(self):
        self._closed = True


class LogicalOperationManager:
    """Per-client, context-local operation binding; nesting cannot reset a cap."""

    def __init__(self, limits=None):
        self.limits = limits
        self._active = ContextVar("popper_logical_operation", default=None)

    @property
    def current(self):
        return self._active.get()

    @contextmanager
    def operation(self, key=None, *, max_attempts=2, cancel_event=None):
        if self.current is not None:
            raise ProtocolError("不能嵌套逻辑请求上下文以重置尝试预算")
        operation = LogicalOperation(key, max_attempts=max_attempts,
                                     limits=self.limits, cancel_event=cancel_event)
        token = self._active.set(operation)
        try:
            yield operation
        finally:
            operation.close()
            self._active.reset(token)


__all__ = ["LogicalOperation", "LogicalOperationManager"]
