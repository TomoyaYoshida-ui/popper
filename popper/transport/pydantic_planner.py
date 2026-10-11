"""Research planning through Pydantic AI tool output and one correction.

The agent owns structured tool calls, validation feedback and the single retry.
This module keeps the research budget, cancellation deadline and independent
action check. It does not execute experiments or score evidence.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from ..core import ProtocolError, canonical, digest
from ..chat import request_output_limits
from ..request_lifecycle import RequestFailure, RequestLimits
from .logical_operation import LogicalOperation
from .pydantic_http import build_openai_client

_TOOLS = ('execute_candidate', 'stop_planning', 'revise_hypothesis')
_TRANSPORT_CATEGORIES = frozenset({
    'connect_timeout', 'read_timeout', 'total_timeout', 'cancelled',
    'authentication_or_permission', 'temporary_service', 'output_truncated',
    'stream_interrupted', 'connection_failure', 'incompatible_request',
    'response_size_limit', 'request_budget_exhausted', 'usage_unresolved',
    'attempt_budget_exhausted', 'usage_accounting_failure', 'request_recovery_blocked',
})
class ProposalEnvelope(BaseModel):
    """Loose tool payload. Action rules are applied after the provider text is stored."""

    model_config = ConfigDict(extra='allow')
    action: dict[str, Any]
    assessment: Any = None


def _endpoint(base_url):
    parsed = urlparse(base_url)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}):
        raise ProtocolError('模型地址必须是 HTTPS，或本机 HTTP')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProtocolError('模型地址不能含凭据、查询参数或 fragment')
    return base_url


def _cause_with_category(error):
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        category = getattr(current, 'category', None)
        if category in _TRANSPORT_CATEGORIES and isinstance(current, ProtocolError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _instructions():
    from ..research.runtime_protocol import decision_schema
    return (
        'Call exactly one registered tool. The arguments are one object with action and an optional assessment. '
        'action.kind must equal the called tool name and must use the field names in this schema. '
        'Use only identifiers present in the user context.\n'
        + json.dumps(decision_schema(), ensure_ascii=False, sort_keys=True)
    )


def _retry_message(error):
    details = list(getattr(error, 'details', None) or [{'path': 'action', 'rule': 'registered_action_or_evidence'}])
    if any(item.get('rule') == 'union_tag_not_found' for item in details):
        details.append({'path': 'action.kind', 'rule': 'required'})
    return canonical(details)


class _StreamingChatModel:
    """Build after pydantic-ai imports so the offline package check stays local."""

    def __new__(cls, model_name, *, provider, observer, allow_tool_forcing=True):
        from pydantic_ai.messages import ToolCallPart
        from pydantic_ai.models.openai import OpenAIChatModel

        class StreamingOpenAIChatModel(OpenAIChatModel):
            def __init__(self):
                super().__init__(model_name, provider=provider)
                self._observer = observer
                self._allow_tool_forcing = allow_tool_forcing

            def _supports_tool_forcing(self, model_settings, model_request_parameters):
                if not self._allow_tool_forcing:
                    return False
                return super()._supports_tool_forcing(model_settings, model_request_parameters)

            async def request(self, messages, model_settings, model_request_parameters):
                async with self.request_stream(messages, model_settings, model_request_parameters) as streamed:
                    async for _event in streamed:
                        pass
                    response = streamed.get()
                names = [part.tool_name for part in response.parts if isinstance(part, ToolCallPart)]
                self._observer(response, names)
                return response

        return StreamingOpenAIChatModel()


class PydanticPlannerClient:
    """One logical planning request: at most the original call and one correction."""

    def __init__(self, base_url, model, *, api_key=None, request_budget=None,
                 diagnostics_dir=None, request_limits=None, inner_transport=None):
        self.base_url = _endpoint(base_url)
        self.model = model
        self.api_key = api_key if api_key is not None else os.environ.get('POPPER_API_KEY')
        if not self.api_key:
            raise ProtocolError('请通过 POPPER_API_KEY 环境变量提供 key')
        self.request_budget = request_budget
        self.diagnostics_dir = diagnostics_dir
        self.request_limits = request_limits or RequestLimits()
        self.inner_transport = inner_transport

    def decide(self, context, *, validate_action, on_event=None, logical_request_id=None,
               cancel_event=None, on_progress=None):
        try:
            from pydantic_ai import Agent, ModelRetry, ToolOutput
        except ImportError as error:
            raise ProtocolError('研究规划需要安装 pydantic-ai-slim[openai]') from error
        return asyncio.run(self._decide(
            context, validate_action=validate_action, on_event=on_event or (lambda _event: None),
            logical_request_id=logical_request_id, cancel_event=cancel_event, on_progress=on_progress,
            agent_type=Agent, retry_type=ModelRetry, tool_output=ToolOutput))

    async def _decide(self, context, *, validate_action, on_event, logical_request_id,
                      cancel_event, on_progress, agent_type, retry_type, tool_output):
        tool_names = getattr(self, 'tool_names', _TOOLS)
        operation = LogicalOperation(logical_request_id, max_attempts=2, limits=self.request_limits,
                                     cancel_event=cancel_event)
        client, transport = build_openai_client(
            api_key=self.api_key, base_url=self.base_url, operation=operation,
            request_budget=self.request_budget, inner_transport=self.inner_transport,
            diagnostics_dir=self.diagnostics_dir,
            on_progress=on_progress, on_event=None)
        state = {'error': None, 'payload': None, 'seen': set(), 'pending': []}
        pending_lock = threading.Lock()

        def emit(kind, **extra):
            attempt = transport.last_attempt
            attempt_index = attempt.attempt_index if attempt is not None else 0
            event_id = digest({'kind': kind, 'logical_request_id': operation.key, 'attempt': attempt_index})
            event = {'kind': kind, 'event_id': event_id, 'attempt_index': attempt_index,
                     'logical_request_id': operation.key, **extra}
            if kind == 'model_response_rejected':
                event.setdefault('consumed_send', attempt_index >= 1)
                event.setdefault('correction_attempt', attempt_index > 1)
                action = (state.get('payload') or {}).get('action') or {}
                if isinstance(action, dict) and action.get('kind'):
                    event.setdefault('action_kind', action['kind'])
                for item in list(event.get('details') or []):
                    if isinstance(item, dict) and item.get('action_kind'):
                        event.setdefault('action_kind', item['action_kind'])
                        break
            if kind == 'model_response_received' and attempt is not None:
                response = attempt.snapshot(api_key=self.api_key).get('raw_response') or ''
                event['response'] = response
                event['response_sha256'] = digest(response)
            with pending_lock:
                if (kind, event_id) in state['seen']:
                    return
                state['seen'].add((kind, event_id))
                state['pending'].append(event)

        def flush():
            # Tool validation can run off the caller thread. SQLite receipts are written
            # only after control returns to the thread that opened the research store.
            with pending_lock:
                events = list(state['pending'])
                state['pending'].clear()
            for event in events:
                on_event(event)

        def observe(_response, names):
            emit('model_response_received')
            if names and any(name not in tool_names for name in names):
                emit('model_response_rejected', details=[{'path': 'tool', 'rule': 'registered_tool'}])

        from pydantic_ai.providers.openai import OpenAIProvider
        limits = request_output_limits(self.base_url, self.model)
        model = _StreamingChatModel(
            self.model, provider=OpenAIProvider(openai_client=client), observer=observe,
            allow_tool_forcing='thinking_budget' not in limits)
        settings = None
        if 'thinking_budget' in limits:
            settings = {
                'max_tokens': limits['max_completion_tokens'],
                'extra_body': {'enable_thinking': limits['enable_thinking'],
                               'thinking_budget': limits['thinking_budget']},
            }
        agent = agent_type(
            model,
            output_type=[tool_output(ProposalEnvelope, name=name, strict=False, max_retries=1,
                                     description='Propose this registered research action.')
                         for name in tool_names],
            retries=1,
            model_settings=settings,
            instructions=self.instructions() if hasattr(self, 'instructions') else _instructions())

        @agent.output_validator
        async def _validate(_ctx, output: ProposalEnvelope) -> ProposalEnvelope:
            # Async validators stay on the caller's event-loop thread. A sync
            # validator is offloaded by Pydantic AI and cannot read its SQLite
            # research session safely.
            state['error'] = None
            payload = {'action': dict(output.action), 'assessment': output.assessment}
            try:
                from ..research.runtime_protocol import parse_action
                action = self.parse_action(payload['action']) if hasattr(self, 'parse_action') else parse_action(payload['action'])
                if action.kind not in tool_names:
                    raise ProtocolError('Runtime protocol validation failed')
                validate_action(action)
            except ProtocolError as error:
                state['error'] = error
                details = json.loads(_retry_message(error))
                action_kind = payload.get('action', {}).get('kind')
                if isinstance(details, list) and action_kind:
                    for item in details:
                        if isinstance(item, dict):
                            item.setdefault('action_kind', action_kind)
                emit('model_response_rejected', details=details, action_kind=action_kind)
                raise retry_type(_retry_message(error)) from None
            state['payload'] = payload
            emit('model_output_accepted')
            return output

        try:
            await agent.run(canonical(context))
            if state['payload'] is None:
                raise ProtocolError('模型没有返回已接受的研究动作')
            return state['payload']
        except Exception as error:
            transport_error = error if getattr(error, 'category', None) in _TRANSPORT_CATEGORIES else _cause_with_category(error)
            if transport_error is not None and state['payload'] is None:
                raise transport_error from None
            if state['error'] is not None:
                state['error'].category = getattr(state['error'], 'category', None) or 'action_contract_rejected'
                raise state['error'] from None
            # Pydantic AI raises UnexpectedModelBehavior for unparseable tool calls after
            # the stream has already been persisted. Treat that as a contract rejection so
            # the research loop can freeze any completed finalist instead of aborting raw.
            if state['payload'] is None and (
                    type(error).__name__ == 'UnexpectedModelBehavior'
                    or type(error).__module__.startswith('pydantic_ai')):
                mapped = ProtocolError('模型输出无法按已注册动作解析')
                mapped.category = 'action_contract_rejected'
                mapped.details = [{'path': 'action', 'rule': 'parseable_registered_tool'}]
                emit('model_response_rejected', details=mapped.details)
                raise mapped from None
            raise
        finally:
            flush()
            operation.close()
            await client.close()


__all__ = ['PydanticPlannerClient', 'ProposalEnvelope']
