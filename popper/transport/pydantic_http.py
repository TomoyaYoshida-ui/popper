"""Budget and cancellation hooks around the pinned OpenAI SDK's HTTP stack.

The SDK owns SSE framing and Chat Completions parsing.  The decoder proxy only
observes its already-decoded events for provider usage and completion markers;
Pydantic AI owns output validation and its one correction request.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
import uuid

import httpx2 as httpx
from openai import AsyncOpenAI

from ..core import ProtocolError, canonical
from ..request_lifecycle import RequestFailure
from .logical_operation import LogicalOperation


def _category(error: BaseException) -> str:
    if isinstance(error, RequestFailure):
        return error.category
    if isinstance(error, httpx.ConnectTimeout):
        return 'connect_timeout'
    if isinstance(error, httpx.ReadTimeout):
        return 'read_timeout'
    if isinstance(error, httpx.ConnectError):
        return 'connection_failure'
    if isinstance(error, (httpx.ReadError, httpx.RemoteProtocolError)):
        return 'stream_interrupted'
    if isinstance(error, asyncio.CancelledError):
        return 'cancelled'
    return getattr(error, 'category', None) or 'invalid_response'


def _http_failure(status):
    if status in (401, 403):
        return 'authentication_or_permission'
    if status in (408, 409, 429, 500, 502, 503, 504):
        return 'temporary_service'
    return 'incompatible_request'


@dataclass
class AttemptReceipt:
    """An attempt receipt contains no request headers or credentials."""

    logical_request_id: str
    attempt_index: int
    request_sha256: str
    request_id: str | None = None
    sent: bool = False
    connected: bool = False
    transport_phase: str = 'not_started'
    output_started: bool = False
    stream_completed: bool = False
    raw_provider_usage_missing: bool = True
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reasons: list[str] = field(default_factory=list)
    http_status: int | None = None
    is_stream: bool = True
    raw_response: bytearray = field(default_factory=bytearray, repr=False)
    failure_category: str | None = None
    budget_failure_category: str | None = None
    settled: bool = False
    last_data: float = field(default_factory=time.monotonic)

    def observe_json(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        if isinstance(value.get('usage'), dict):
            self.raw_provider_usage_missing = False
            self.usage.update(value['usage'])
        for choice in value.get('choices', []):
            if not isinstance(choice, dict):
                continue
            finish = choice.get('finish_reason')
            if isinstance(finish, str) and finish not in self.finish_reasons:
                self.finish_reasons.append(finish)
            delta = choice.get('delta', choice.get('message', {}))
            if isinstance(delta, dict) and any(delta.get(key) for key in (
                    'content', 'reasoning_content', 'reasoning', 'tool_calls', 'function_call')):
                self.output_started = True
                self.transport_phase = 'generating'

    def failure(self, category: str) -> RequestFailure:
        error = RequestFailure(category, usage=self.usage, sent=self.sent,
                               transport_phase=self.transport_phase,
                               output_started=self.output_started,
                               stream_completed=self.stream_completed)
        error.logical_request_id = self.logical_request_id
        error.attempt_index = self.attempt_index
        return error

    def snapshot(self, *, api_key: str | None = None, include_raw: bool = True) -> dict[str, Any]:
        raw = bytes(self.raw_response)
        result = {
            'logical_request_id': self.logical_request_id, 'attempt': self.attempt_index,
            'request_id': self.request_id, 'request_sha256': self.request_sha256,
            'sent': self.sent, 'http_status': self.http_status,
            'transport_phase': self.transport_phase, 'output_started': self.output_started,
            'stream_completed': self.stream_completed,
            'raw_provider_usage_missing': self.raw_provider_usage_missing,
            'usage': dict(self.usage), 'finish_reasons': list(self.finish_reasons),
            'failure_category': self.failure_category,
            'request_keys': list(getattr(self, 'request_keys', ())),
            'budget_failure_category': self.budget_failure_category,
            'raw_response_sha256': hashlib.sha256(raw).hexdigest(),
            'raw_response_bytes': len(raw), 'settled': self.settled,
        }
        if include_raw:
            value = raw.decode('utf-8', errors='replace')
            if api_key:
                value = value.replace(api_key, '[REDACTED]')
            result['raw_response'] = re.sub(r'sk-[A-Za-z0-9_.-]+', '[REDACTED]', value)
        return result


class BudgetedAsyncTransport(httpx.AsyncBaseTransport):
    """One reservation per dispatch, with no autonomous transport retries."""

    def __init__(self, operation: LogicalOperation, request_budget=None, *, inner=None,
                 diagnostics_dir=None, on_progress=None, on_event=None, api_key=None,
                 wire_limit=8_000_000):
        self.operation = operation
        self.request_budget = request_budget
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)
        self.diagnostics_dir = Path(diagnostics_dir) if diagnostics_dir is not None else None
        self.on_progress = on_progress
        self.on_event = on_event
        self.api_key = api_key
        self.wire_limit = wire_limit
        self.attempts: list[AttemptReceipt] = []
        self.last_attempt: AttemptReceipt | None = None
        self._last_progress = operation.started

    def _progress(self, state, attempt):
        if self.on_progress:
            self.on_progress({'state': state,
                              'elapsed_seconds': round(time.monotonic() - self.operation.started, 1),
                              'transport_phase': attempt.transport_phase,
                              'output_started': attempt.output_started})

    async def _bounded(self, awaitable, attempt, *, connecting=False):
        task = asyncio.ensure_future(awaitable)
        start = time.monotonic()
        try:
            while True:
                try:
                    self.operation.check()
                except RequestFailure as error:
                    raise attempt.failure(error.category) from None
                now = time.monotonic()
                if connecting and not attempt.connected and now - start >= self.operation.limits.connect:
                    raise attempt.failure('connect_timeout')
                if attempt.connected and now - attempt.last_data >= self.operation.limits.idle:
                    raise attempt.failure('read_timeout')
                if now - self._last_progress >= self.operation.limits.heartbeat:
                    self._progress('waiting', attempt)
                    self._last_progress = now
                done, _ = await asyncio.wait({task}, timeout=.05)
                if done:
                    return task.result()
        except BaseException:
            task.cancel()
            # The real HTTPX transport is cancellation-cooperative. A malicious
            # injected transport cannot make the local caller wait indefinitely.
            done, _ = await asyncio.wait({task}, timeout=.1)
            if done:
                try:
                    task.result()
                except BaseException:
                    pass
            else:
                task.add_done_callback(_consume_task_exception)
            raise

    async def handle_async_request(self, request):
        self.operation.check()
        content = await request.aread()
        try:
            body = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise ProtocolError('模型请求必须是 JSON 对象') from None
        if not isinstance(body, dict):
            raise ProtocolError('模型请求必须是 JSON 对象')
        return await self._dispatch(request, body, unsent_retry=False)

    async def _dispatch(self, request, body, *, unsent_retry):
        attempt_index = (self.operation.claim_unsent_retry() if unsent_retry
                         else self.operation.claim_attempt())
        attempt = AttemptReceipt(self.operation.key, attempt_index,
                                 hashlib.sha256(canonical(body).encode()).hexdigest(),
                                 is_stream=body.get('stream') is True)
        if self.request_budget:
            attempt.request_id = self.request_budget.reserve(
                body, logical_request_id=self.operation.key, attempt_index=attempt_index)
        self.attempts.append(attempt)
        self.last_attempt = attempt
        attempt.request_keys = sorted(body)
        attempt.transport_phase = 'connecting'
        # Native HTTPX/httpcore trace callbacks establish the actual dispatch
        # boundary. Unknown injected transports are conservatively marked sent.
        native = isinstance(self.inner, httpx.AsyncHTTPTransport)
        attempt.sent = not native
        async def trace(event, _info):
            if event.endswith('send_request_headers.started'):
                attempt.sent = True
                attempt.connected = True
                attempt.last_data = time.monotonic()
                attempt.transport_phase = 'sent'
            elif event.endswith(('connect_tcp.complete', 'connect_unix_socket.complete', 'start_tls.complete')):
                attempt.connected = True
                attempt.last_data = time.monotonic()
                attempt.transport_phase = 'connected'
        request.extensions['trace'] = trace
        request.extensions['timeout'] = {
            'connect': self.operation.limits.connect, 'read': self.operation.limits.idle,
            'write': self.operation.limits.idle, 'pool': self.operation.limits.connect,
        }
        self._progress('waiting', attempt)
        try:
            self.operation.check()
            response = await self._bounded(self.inner.handle_async_request(request), attempt, connecting=True)
            attempt.sent = True
            attempt.connected = True
            attempt.last_data = time.monotonic()
            attempt.http_status = response.status_code
            attempt.transport_phase = 'receiving'
            self._progress('receiving', attempt)
            if response.status_code >= 300:
                category = ('incompatible_request' if response.status_code < 400
                            else _http_failure(response.status_code))
                collected = bytearray()
                try:
                    async for chunk in response.stream:
                        collected.extend(chunk)
                        if len(collected) >= 4096:
                            break
                except Exception:
                    pass
                attempt.raw_response.extend(collected[:4096])
                try:
                    await response.aclose()
                except Exception:
                    pass
                error = attempt.failure(category)
                self.finalize(attempt, error)
                raise error
            stream = _BudgetedStream(response.stream, self, attempt)
            response.stream = stream
            response.extensions['popper_attempt'] = attempt
            return response
        except BaseException as error:
            if not attempt.settled:
                # Budget and protocol failures already name their category. Wrapping them
                # would hide that category behind a generic transport error.
                failure = error if isinstance(error, ProtocolError) else attempt.failure(_category(error))
                try:
                    self.finalize(attempt, failure)
                except BaseException as finalized:
                    failure = finalized
            else:
                failure = error
            category = getattr(failure, 'category', None) or attempt.failure_category
            if (not unsent_retry and not attempt.sent
                    and category in {'connection_failure', 'connect_timeout'}):
                return await self._dispatch(request, body, unsent_retry=True)
            raise failure

    def finalize(self, attempt, error=None):
        if attempt.settled:
            return
        if error is not None:
            attempt.failure_category = getattr(error, 'category', None) or _category(error)
        elif 'length' in attempt.finish_reasons:
            error = attempt.failure('output_truncated')
            attempt.failure_category = error.category
        elif attempt.is_stream and not attempt.stream_completed:
            error = attempt.failure('stream_interrupted')
            attempt.failure_category = error.category
        attempt.settled = True
        budget_error = None
        try:
            if self.request_budget and attempt.request_id:
                self.request_budget.settle(
                    attempt.request_id, attempt.usage if not attempt.raw_provider_usage_missing else None,
                    reason=attempt.failure_category or 'missing_provider_usage',
                    sent=attempt.sent, complete=attempt.stream_completed,
                    transport_phase=attempt.transport_phase, output_started=attempt.output_started)
        except Exception as caught:
            budget_error = caught
            attempt.budget_failure_category = getattr(caught, 'category', type(caught).__name__)
        event = attempt.snapshot(api_key=self.api_key)
        event['event'] = 'attempt_completed'
        if self.diagnostics_dir:
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            name = (attempt.request_id or uuid.uuid4().hex) + '.json'
            (self.diagnostics_dir / name).write_text(canonical(event) + '\n', encoding='utf-8')
        if self.on_event:
            self.on_event(event)
        self._progress('failed' if error or budget_error else 'completed', attempt)
        if error:
            raise error
        if budget_error:
            raise budget_error

    async def aclose(self):
        await self.inner.aclose()


def _consume_task_exception(task):
    try:
        task.result()
    except BaseException:
        pass


class _BudgetedStream(httpx.AsyncByteStream):
    def __init__(self, inner, transport, attempt):
        self.inner = inner
        self.transport = transport
        self.attempt = attempt
        self.closed = False

    async def __aiter__(self):
        iterator = self.inner.__aiter__()
        try:
            while True:
                try:
                    chunk = await self.transport._bounded(iterator.__anext__(), self.attempt)
                except StopAsyncIteration:
                    if not self.attempt.is_stream:
                        self.attempt.stream_completed = True
                        try:
                            self.attempt.observe_json(json.loads(self.attempt.raw_response))
                        except (ValueError, UnicodeDecodeError):
                            pass
                    # SSE completion and usage are observed by the SDK decoder.
                    self.transport.finalize(self.attempt)
                    return
                self.attempt.last_data = time.monotonic()
                self.attempt.raw_response.extend(chunk)
                if len(self.attempt.raw_response) > self.transport.wire_limit:
                    raise self.attempt.failure('response_size_limit')
                yield chunk
        except BaseException as error:
            if not self.attempt.settled:
                failure = error if isinstance(error, RequestFailure) else self.attempt.failure(_category(error))
                self.transport.finalize(self.attempt, failure)
            raise

    async def abort(self, error):
        await self._close_inner()
        self.transport.finalize(self.attempt, error)

    async def _close_inner(self):
        if self.closed:
            return
        self.closed = True
        task = asyncio.ensure_future(self.inner.aclose())
        done, _ = await asyncio.wait({task}, timeout=.1)
        if not done:
            task.cancel()
            task.add_done_callback(_consume_task_exception)
        else:
            task.result()

    async def aclose(self):
        await self._close_inner()
        if not self.attempt.settled:
            self.transport.finalize(self.attempt)


class _ObservedDecoder:
    """Delegate all framing to the SDK, observing only decoded events."""

    def __init__(self, decoder, attempt):
        self.decoder, self.attempt = decoder, attempt

    def _observe(self, event):
        if event.data.startswith('[DONE]'):
            self.attempt.stream_completed = True
        else:
            try:
                self.attempt.observe_json(event.json())
            except (ValueError, TypeError):
                pass  # The SDK owns the decode error and no action can execute.
        return event

    async def aiter_bytes(self, iterator):
        async for event in self.decoder.aiter_bytes(iterator):
            yield self._observe(event)

    def iter_bytes(self, iterator):
        for event in self.decoder.iter_bytes(iterator):
            yield self._observe(event)


class BudgetedAsyncOpenAI(AsyncOpenAI):
    """Pinned SDK extension points preserve raw usage before normalization."""

    def __init__(self, *, budget_transport, **kwargs):
        if kwargs.get('max_retries', 0) != 0:
            raise ProtocolError('模型 SDK 自动重试必须关闭')
        kwargs['max_retries'] = 0
        self.budget_transport = budget_transport
        super().__init__(**kwargs)

    def _make_sse_decoder(self):
        decoder = super()._make_sse_decoder()
        attempt = self.budget_transport.last_attempt
        if attempt is None:
            raise ProtocolError('流响应没有预算尝试身份')
        return _ObservedDecoder(decoder, attempt)

    def _process_response_data(self, *, data, cast_to, response):
        attempt = response.extensions.get('popper_attempt')
        if attempt is not None:
            attempt.observe_json(data)
        return super()._process_response_data(data=data, cast_to=cast_to, response=response)


def build_openai_client(*, api_key, base_url, operation, request_budget=None,
                        inner_transport=None, diagnostics_dir=None, on_progress=None, on_event=None):
    """Build the SDK client and its receipt owner; caller awaits client.close()."""
    transport = BudgetedAsyncTransport(
        operation, request_budget, inner=inner_transport, diagnostics_dir=diagnostics_dir,
        on_progress=on_progress, on_event=on_event, api_key=api_key)
    client = BudgetedAsyncOpenAI(
        api_key=api_key, base_url=base_url, budget_transport=transport,
        http_client=httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False),
        max_retries=0)
    return client, transport


__all__ = ['AttemptReceipt', 'BudgetedAsyncTransport', 'BudgetedAsyncOpenAI', 'build_openai_client']
