"""Bounded stdlib HTTP operations shared by chat and structured research calls."""
from __future__ import annotations
import math
import http.client
import os
import queue
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass
from .core import ProtocolError


def usage_total(usage):
    """Normalize reported counts; never infer tokens from text or elapsed time."""
    if not isinstance(usage,dict):
        return None
    total=usage.get('total_tokens')
    if type(total) is int and total>=0:
        return total
    a,b=usage.get('prompt_tokens'),usage.get('completion_tokens')
    return a+b if type(a) is int and type(b) is int and a>=0 and b>=0 else None


class RequestFailure(ProtocolError):
    def __init__(self, category, *, usage=None, sent=True, transport_phase=None,
                 output_started=False, stream_completed=False):
        messages = {'stream_interrupted': '模型流提前断开；保留部分回复与用量记录',
                    'connect_timeout': '模型连接超时', 'read_timeout': '等待模型响应超时',
                    'total_timeout': '模型调用达到总时限', 'cancelled': '模型调用已取消',
                    'authentication_or_permission': '模型认证或权限失败',
                    'output_truncated': '模型输出达到长度上限'}
        super().__init__('模型调用失败: ' + messages.get(category, category))
        self.category = category
        self.usage = dict(usage or {})
        self.sent = sent
        self.stream_completed = stream_completed
        self.transport_phase = transport_phase
        self.output_started = output_started


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


class SSEReader:
    """Normalize complete SSE events, including metadata and multiline data."""
    def __init__(self, response, *, wire_limit=8_000_000, line_limit=65536):
        self.response = response
        self.wire_limit, self.line_limit = wire_limit, line_limit
        self.wire = 0

    def readline(self, _size):
        parts, event_bytes = [], 0
        while True:
            raw = self.response.readline(self.line_limit+1)
            if not raw:
                return b''  # Never accept an unterminated event at EOF.
            self.wire += len(raw)
            if len(raw)>self.line_limit or self.wire>self.wire_limit:
                raise RequestFailure('response_size_limit', usage=self.response.usage)
            line=raw.decode('utf-8').rstrip('\r\n')
            if not line:
                if parts:
                    return ('data: '+'\n'.join(parts)+'\n').encode('utf-8')
                continue
            if line.startswith(':'):
                continue
            if line.startswith(('event:', 'id:', 'retry:')):
                continue
            if not line.startswith('data:'):
                raise RequestFailure('invalid_sse', usage=self.response.usage)
            value=line[5:]
            if value.startswith(' '): value=value[1:]
            event_bytes += len(value.encode('utf-8'))
            if event_bytes>1_000_000:
                raise RequestFailure('response_size_limit', usage=self.response.usage)
            parts.append(value)

    def close(self):
        self.response.close()


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
        self.connected = threading.Event()
        self.connection = None
        self.transport_phase = 'not_started'
        self.output_started = False
        self.stream_completed = False
        self._transport_lock = threading.Lock()

    def failure(self, category):
        return RequestFailure(category, usage=self.usage, sent=self.sent,
                              transport_phase=self.transport_phase,
                              output_started=self.output_started,
                              stream_completed=self.stream_completed)

    def observe_output(self, piece):
        if isinstance(piece, str) and piece:
            self.output_started = True
            self.transport_phase = 'generating'

    def progress(self, state):
        if self.on_progress is not None and not self.stopped.is_set():
            self.on_progress({'state': state, 'elapsed_seconds': round(time.monotonic()-self.started, 1),
                              'transport_phase':self.transport_phase,'output_started':self.output_started})

    def _abort(self):
        with self._transport_lock:
            self.stopped.set()
        response = self.response
        def close():
            try:
                sock = self.connection.sock if self.connection is not None else response.fp.raw._sock
                sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            try:
                if response is not None: response.close()
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
            raise self.failure('cancelled')
        threading.Thread(target=run, daemon=True).start()
        operation_start = time.monotonic()
        while True:
            now = time.monotonic()
            category = None
            if self.cancel.is_set():
                category = 'cancelled'
            elif now-self.started >= self.limits.total:
                category = 'total_timeout'
            elif connecting and not self.connected.is_set() and now-operation_start >= self.limits.connect:
                category = 'connect_timeout'
            elif (not connecting or self.connected.is_set()) and now-self.last_data >= self.limits.idle:
                category = 'read_timeout'
            if category:
                self._abort()
                self.progress('failed')
                raise self.failure(category)
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
                    raise self.failure(failure_category(value)) from None
                raise value
            return value

    def open(self, opener, request):
        if self.cancel.is_set():
            raise RequestFailure('cancelled', sent=False)
        if isinstance(opener, urllib.request.OpenerDirector):
            lifecycle = self
            def connection_factory(base):
                class Connection(base):
                    def connect(self):
                        lifecycle.connection = self
                        lifecycle.transport_phase = 'connecting'
                        super().connect()
                        lifecycle.connected.set()
                        lifecycle.last_data = time.monotonic()
                        self.sock.settimeout(lifecycle.limits.idle)
                        if lifecycle.stopped.is_set():
                            self.close()
                            raise lifecycle.failure('cancelled')
                        lifecycle.transport_phase = 'connected'

                    def send(self, data):
                        # Proxy CONNECT traffic is not an application/model request.
                        if self._tunnel_host and not lifecycle.connected.is_set():
                            return super().send(data)
                        if self.sock is None:
                            self.connect()
                        with lifecycle._transport_lock:
                            if lifecycle.stopped.is_set() or lifecycle.cancel.is_set():
                                raise lifecycle.failure('cancelled')
                            # Mark BEFORE writing: a partial failed write remains ambiguous.
                            lifecycle.sent = True
                            lifecycle.transport_phase = 'sending'
                        return super().send(data)
                return Connection
            class HTTP(urllib.request.HTTPHandler):
                def http_open(self, req):
                    result = self.do_open(connection_factory(http.client.HTTPConnection), req)
                    lifecycle.transport_phase = 'response_headers'
                    return result
            class HTTPS(urllib.request.HTTPSHandler):
                def https_open(self, req):
                    result = self.do_open(connection_factory(http.client.HTTPSConnection), req,
                                          context=self._context, check_hostname=self._check_hostname)
                    lifecycle.transport_phase = 'response_headers'
                    return result
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args, **_kwargs):
                    raise ProtocolError('模型接口重定向被拒绝')
            # Match the previous httpx client: honor explicit environment
            # proxies, not an unrelated Windows browser registry proxy.
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(urllib.request.getproxies_environment()),
                NoRedirect(), HTTP(), HTTPS())
        else:
            # An injected transport provides no proof that it has not sent bytes.
            self.sent = True
            self.transport_phase = 'sending'
        self.progress('waiting')
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
            if not self.output_started:
                self.transport_phase = 'streaming'
        return raw

    def close(self):
        self._abort()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
