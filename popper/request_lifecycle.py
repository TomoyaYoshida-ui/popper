"""Bounded stdlib HTTP operations shared by chat and structured research calls."""
from __future__ import annotations
import math
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from .core import ProtocolError


class RequestFailure(ProtocolError):
    def __init__(self, category, *, usage=None, sent=True):
        super().__init__(f'模型调用失败: {category}')
        self.category = category
        self.usage = dict(usage or {})
        self.sent = sent
        self.stream_completed = False


@dataclass(frozen=True)
class RequestLimits:
    connect: float = 15
    idle: float = 60
    total: float = 180
    heartbeat: float = 5

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in
               (self.connect, self.idle, self.total, self.heartbeat)):
            raise ProtocolError('请求时限必须为有限正数')

    @classmethod
    def from_env(cls):
        try:
            return cls(**{key: float(os.environ.get('POPPER_REQUEST_' + key.upper() + '_SECONDS', default))
                          for key, default in (('connect', 15), ('idle', 60), ('total', 180))})
        except ValueError:
            raise ProtocolError('请求时限配置不合法') from None


def failure_category(error):
    if isinstance(error, RequestFailure):
        return error.category
    status = getattr(error, 'code', None)
    if status in (401, 403):
        return 'authentication_or_permission'
    if status in (429, 502, 503, 504):
        return 'temporary_service'
    if status is not None:
        return 'incompatible_request'
    reason = getattr(error, 'reason', error)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'read_timeout'
    if isinstance(reason, (ConnectionError, OSError)):
        return 'connection_failure'
    if type(reason).__name__ in ('RemoteDisconnected', 'IncompleteRead'):
        return 'connection_failure'
    return 'invalid_response'


def cancellable_wait(seconds, cancelled=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancelled is not None and cancelled():
            raise RequestFailure('cancelled', sent=False)
        time.sleep(min(.1, max(0, end - time.monotonic())))


class RequestLifecycle:
    """Caller waits in a bounded loop, including connect and partial-line reads.

    Abandoned urllib operations run only in daemon threads. The response socket
    is shut down on cancellation/deadline; no callback or tool dispatch runs in
    those threads. A late connection is closed before it can be consumed.
    """
    def __init__(self, cancel_event=None, limits=None, on_progress=None):
        self.cancel = cancel_event or threading.Event()
        self.limits = limits or RequestLimits.from_env()
        self.on_progress = on_progress
        self.started = self.last_data = self.last_progress = time.monotonic()
        self.response = None
        self.stopped = threading.Event()
        self.usage = {}
        self.sent = False

    def progress(self, state):
        if self.on_progress is not None and not self.stopped.is_set():
            self.on_progress({'state': state, 'elapsed_seconds': round(time.monotonic()-self.started, 1)})

    def _abort(self):
        self.stopped.set()
        response = self.response
        if response is None:
            return
        def close():
            try:
                sock = response.fp.raw._sock
                sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            try:
                response.close()
            except Exception:
                pass
        threading.Thread(target=close, daemon=True).start()

    def _operation(self, operation, *, connecting=False):
        results = queue.Queue(maxsize=1)
        def run():
            try:
                value = operation()
                if connecting:
                    self.response = value
                    if self.stopped.is_set():
                        self._abort()
                results.put((True, value))
            except Exception as error:
                results.put((False, error))
        if self.cancel.is_set():
            raise RequestFailure('cancelled', usage=self.usage, sent=self.sent)
        threading.Thread(target=run, daemon=True).start()
        operation_start = time.monotonic()
        while True:
            now = time.monotonic()
            category = None
            if self.cancel.is_set():
                category = 'cancelled'
            elif now-self.started >= self.limits.total:
                category = 'total_timeout'
            elif connecting and now-operation_start >= self.limits.connect:
                category = 'connect_timeout'
            elif not connecting and now-self.last_data >= self.limits.idle:
                category = 'read_timeout'
            if category:
                self._abort()
                self.progress('failed')
                raise RequestFailure(category, usage=self.usage, sent=self.sent)
            if now-self.last_progress >= self.limits.heartbeat:
                self.progress('waiting')
                self.last_progress = now
            try:
                ok, value = results.get(timeout=.05)
            except queue.Empty:
                continue
            if not ok:
                self._abort()
                if isinstance(value, (OSError, ConnectionError)) and not hasattr(value, 'code'):
                    raise RequestFailure(failure_category(value), usage=self.usage, sent=self.sent) from None
                raise value
            return value

    def open(self, opener, request):
        self.progress('waiting')
        self.sent = True
        self._operation(lambda: opener.open(request, timeout=self.limits.connect), connecting=True)
        self.last_data = time.monotonic()
        # Connect timeout should not become the streaming idle timeout.
        try:
            self.response.fp.raw._sock.settimeout(self.limits.idle)
        except AttributeError:
            pass
        return self

    def readline(self, size):
        raw = self._operation(lambda: self.response.readline(size))
        if raw:
            self.last_data = time.monotonic()
        return raw

    def close(self):
        self._abort()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
