"""Exercise the upstream agent through real local OpenAI-compatible SSE HTTP.

No provider requests, controller execution permission or historical data is used
by the model transport.  The integration fixture below separately verifies the
handoff to the existing independently evaluated controller.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip('pydantic_ai', reason='agent extra required')

from popper.core import ProtocolError, canonical, digest, initialize
from popper.model_budget import ModelRequestBudget
from popper.request_lifecycle import RequestFailure, RequestLimits
from popper.transport.pydantic_planner import PydanticPlannerClient


SECRET = 'confidential_fixture_value'
HID = 'H-registered-candidate'
REF = 'OBS-independent-evidence'


def execute(candidate_id=HID, *, assessment=None):
    return {'action': {'kind': 'execute_candidate', 'candidate_id': candidate_id,
                       'rationale': 'Compare the registered candidate', 'evidence_refs': []},
            'assessment': assessment}


def stop():
    return {'action': {'kind': 'stop_planning', 'rationale': 'Stop after the available evidence',
                       'evidence_refs': [REF]}, 'assessment': None}


def revise():
    return {'action': {'kind': 'revise_hypothesis', 'hypothesis_id': HID,
                       'mechanism': 'A revised explanatory mechanism',
                       'applicability': 'The registered development scope',
                       'predictions': ['The unchanged registered comparison'],
                       'rationale': 'Revise in response to independently checked evidence',
                       'evidence_refs': [REF]}, 'assessment': None}


def response(tool, payload, *, finish_reason='tool_calls', usage=True, done=True):
    return {'tool': tool, 'payload': payload, 'finish_reason': finish_reason,
            'usage': usage, 'done': done}


class ScriptedHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.bodies.append(request)
        number = len(self.server.bodies) - 1
        script = self.server.scripts[min(number, len(self.server.scripts) - 1)]
        if script.get('status'):
            body = json.dumps({'error': {'message': SECRET, 'type': 'provider_error'}}).encode()
            self.send_response(script['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            if script.get('heartbeat'):
                for _ in range(80):
                    self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
                    time.sleep(.025)
                return
            arguments = canonical(script['payload'])
            middle = len(arguments) // 2
            base = {'id': f'chatcmpl-local-{number}', 'object': 'chat.completion.chunk',
                    'created': 1, 'model': 'local-model'}
            frames = [
                {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [
                    {'index': 0, 'id': f'call-local-{number}', 'type': 'function',
                     'function': {'name': script['tool'], 'arguments': arguments[:middle]}}]},
                              'finish_reason': None}]},
                {'choices': [{'index': 0, 'delta': {'tool_calls': [
                    {'index': 0, 'function': {'arguments': arguments[middle:]}}]},
                              'finish_reason': None}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': script['finish_reason']}]},
            ]
            if script['usage']:
                frames.append({'choices': [], 'usage': {'prompt_tokens': 11,
                                                       'completion_tokens': 6, 'total_tokens': 17}})
            for frame in frames:
                self.wfile.write(('data: ' + json.dumps({**base, **frame}) + '\n\n').encode())
                self.wfile.flush()
            if script['done']:
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


@pytest.fixture
def server():
    instance = ThreadingHTTPServer(('127.0.0.1', 0), ScriptedHandler)
    instance.daemon_threads = True
    instance.bodies = []
    instance.scripts = [response('execute_candidate', execute())]
    thread = threading.Thread(target=lambda: instance.serve_forever(poll_interval=.025), daemon=True)
    thread.start()
    instance.url = f'http://127.0.0.1:{instance.server_port}/v1'
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


@pytest.fixture
def budget(tmp_path):
    ledger = ModelRequestBudget(tmp_path / 'model-calls.sqlite', global_tokens=500000,
        global_calls=9, scope='offline-planner', scope_tokens=500000, scope_calls=9)
    yield ledger
    ledger.close()


def planner(server, budget, tmp_path, **kwargs):
    return PydanticPlannerClient(server.url, 'local-model', api_key='synthetic-placeholder',
        request_budget=budget, diagnostics_dir=tmp_path / 'diagnostics', **kwargs)


def context():
    return {'objective': 'Choose among the preregistered candidates',
            'candidates': [{'hypothesis_id': HID, 'version': 1, 'executed': False}],
            'raw_evidence': [{'hypothesis_id': HID, 'scope': 'dev', 'evidence_refs': [REF]}],
            'remaining_experiments': 1, 'primary_scope_id': 'dev'}


def validate(action):
    candidate = getattr(action, 'candidate_id', getattr(action, 'hypothesis_id', HID))
    if candidate != HID:
        error = ProtocolError('Unregistered execution identity')
        error.details = [{'path': 'action.candidate_id', 'rule': 'registered_candidate'}]
        raise error
    if any(reference != REF for reference in action.evidence_refs):
        error = ProtocolError('Unknown evidence identity')
        error.details = [{'path': 'action.evidence_refs', 'rule': 'available_evidence'}]
        raise error


def events_of(events, kind):
    return [event for event in events if event['kind'] == kind]


@pytest.mark.parametrize('tool,payload', [
    ('execute_candidate', execute()), ('stop_planning', stop()), ('revise_hypothesis', revise()),
])
def test_real_sse_parses_each_registered_tool_without_executing_it(server, budget, tmp_path, tool, payload):
    server.scripts = [response(tool, payload)]
    client = planner(server, budget, tmp_path)
    validations, events = [], []
    def check(action):
        validate(action)
        validations.append(action.kind)
    assert client.decide(context(), validate_action=check, on_event=events.append,
                         logical_request_id='typed-output') == payload
    assert validations == [payload['action']['kind']]
    assert len(server.bodies) == 1
    request = server.bodies[0]
    assert request['stream'] is True
    assert {tool['function']['name'] for tool in request['tools']} == {
        'execute_candidate', 'stop_planning', 'revise_hypothesis'}
    assert request.get('response_format') != {'type': 'json_object'}
    assert budget.snapshot()['global']['known_tokens'] == 17
    assert not list(tmp_path.rglob('research.sqlite'))
    assert len(events_of(events, 'model_response_received')) == 1
    assert len(events_of(events, 'model_output_accepted')) == 1


@pytest.mark.parametrize('fault', ['candidate', 'evidence', 'unknown_tool', 'schema'])
def test_invalid_proposal_is_a_tool_result_then_one_bounded_correction(server, budget, tmp_path, fault):
    bad = execute()
    name = 'execute_candidate'
    if fault == 'candidate':
        bad['action']['candidate_id'] = SECRET
    elif fault == 'evidence':
        bad['action']['evidence_refs'] = [SECRET]
    elif fault == 'unknown_tool':
        name = 'unregistered_tool'
    else:
        del bad['action']['rationale']
        bad['action'][SECRET] = SECRET
    server.scripts = [response(name, bad), response('execute_candidate', execute())]
    events = []
    result = planner(server, budget, tmp_path).decide(context(), validate_action=validate,
        on_event=events.append, logical_request_id='one-correction')
    assert result == execute()
    assert len(server.bodies) == 2
    assert budget.snapshot()['global']['known_tokens'] == 34
    attempts = budget.snapshot()['attempts']
    assert [attempt['attempt_index'] for attempt in attempts] == [1, 2]
    assert {attempt['logical_request_id'] for attempt in attempts} == {'one-correction'}
    received = events_of(events, 'model_response_received')
    assert len(received) == 2
    assert [event['attempt_index'] for event in received] == [1, 2]
    assert len({event['event_id'] for event in received}) == 2
    for event in received:
        assert event['response_sha256'] == digest(event['response'])
        assert event['response']  # Complete provider message persists before validation.
    assert len(events_of(events, 'model_response_rejected')) >= 1
    correction = [message for message in server.bodies[1]['messages'] if message['role'] == 'tool']
    assert correction
    assert SECRET not in json.dumps(correction)
    assert len(events_of(events, 'model_output_accepted')) == 1


@pytest.mark.parametrize('tool,bad,expected,ctx', [
    ('revise_hypothesis', {**revise()['action'], 'evidence_refs': []},
     {'path': 'action.evidence_refs', 'rule': 'evidence_required'}, None),
    ('revise_hypothesis', {**revise()['action'], 'evidence_refs': [SECRET]},
     {'path': 'action.evidence_refs', 'rule': 'unknown_evidence'}, None),
    ('execute_candidate', execute(SECRET)['action'],
     {'path': 'action.candidate_id', 'rule': 'unexecuted_registered_candidate'}, None),
    ('revise_hypothesis', {**revise()['action'], 'hypothesis_id': SECRET},
     {'path': 'action.hypothesis_id', 'rule': 'unexecuted_registered_candidate'}, None),
    ('execute_candidate', execute()['action'],
     {'path': 'action', 'rule': 'experiment_limit'},
     {**context(), 'remaining_experiments': 0}),
])
def test_runtime_rejection_reaches_one_correction_without_secret(server, budget, tmp_path, tool, bad, expected, ctx):
    from popper.research.runtime import ResearchRuntime

    class _Host:
        _reject = ResearchRuntime._reject
        _validate_refs = ResearchRuntime._validate_refs
        _validate_action = ResearchRuntime._validate_action

    host = _Host()
    facts = ctx or context()
    correction_payload = stop() if expected['rule'] == 'experiment_limit' else execute()
    server.scripts = [response(tool, {'action': bad, 'assessment': None}),
                      response(correction_payload['action']['kind'], correction_payload)]
    planner(server, budget, tmp_path).decide(
        facts, validate_action=lambda action: host._validate_action(action, facts),
        logical_request_id='runtime-feedback')
    correction = [message for message in server.bodies[1]['messages'] if message['role'] == 'tool']
    text = json.dumps(correction)
    assert expected['path'] in text and expected['rule'] in text
    assert SECRET not in text
    assert len(server.bodies) == 2


def test_two_semantic_rejections_keep_precise_safe_receipts_and_no_third_send(server, budget, tmp_path):
    server.scripts = [response('execute_candidate', execute(SECRET))]
    events = []
    with pytest.raises(ProtocolError) as failure:
        planner(server, budget, tmp_path).decide(context(), validate_action=validate,
            on_event=events.append, logical_request_id='terminal-rejection')
    assert failure.value.category == 'action_contract_rejected'
    assert failure.value.details == [{'path': 'action.candidate_id', 'rule': 'registered_candidate'}]
    assert SECRET not in str(failure.value)
    assert len(server.bodies) == 2
    assert len(events_of(events, 'model_response_received')) == 2
    rejected = events_of(events, 'model_response_rejected')
    assert len(rejected) == 2
    assert rejected[0]['consumed_send'] is True
    assert rejected[0]['correction_attempt'] is False
    assert rejected[1]['correction_attempt'] is True
    assert rejected[0].get('action_kind') == 'execute_candidate'
    assert not events_of(events, 'model_output_accepted')
    assert budget.snapshot()['global']['known_tokens'] == 34


def test_unexpected_model_behavior_maps_to_contract_rejection(server, budget, tmp_path, monkeypatch):
    import pydantic_ai

    class Boom(Exception):
        pass

    Boom.__name__ = 'UnexpectedModelBehavior'
    Boom.__module__ = 'pydantic_ai.exceptions'
    events = []
    original = pydantic_ai.Agent.run

    async def raise_behavior(self, *args, **kwargs):
        raise Boom('tool arguments were not valid JSON')

    monkeypatch.setattr(pydantic_ai.Agent, 'run', raise_behavior)
    server.scripts = [response('execute_candidate', execute())]
    with pytest.raises(ProtocolError) as failure:
        planner(server, budget, tmp_path).decide(context(), validate_action=validate,
            on_event=events.append, logical_request_id='sdk-behavior')
    assert failure.value.category == 'action_contract_rejected'
    assert failure.value.details == [{'path': 'action', 'rule': 'parseable_registered_tool'}]
    assert len(events_of(events, 'model_response_rejected')) == 1
    monkeypatch.setattr(pydantic_ai.Agent, 'run', original)


def test_assessment_is_preserved_for_independent_runtime_validation(server, budget, tmp_path):
    bad = execute(assessment={'score': 999, 'judgment': 'supported'})
    server.scripts = [response('execute_candidate', bad)]
    result = planner(server, budget, tmp_path).decide(context(), validate_action=validate)
    assert result == bad
    assert len(server.bodies) == 1


def test_parseable_length_tool_is_rejected_before_semantic_validation(server, budget, tmp_path):
    server.scripts = [response('execute_candidate', execute(), finish_reason='length')]
    events = []
    def forbidden(_action):
        pytest.fail('truncated model tool must never reach action validation or execution')
    with pytest.raises(RequestFailure) as failure:
        planner(server, budget, tmp_path).decide(context(), validate_action=forbidden, on_event=events.append)
    assert failure.value.category == 'output_truncated'
    assert len(server.bodies) == 1
    assert not events_of(events, 'model_output_accepted')
    assert budget.snapshot()['global']['known_tokens'] == 17


@pytest.mark.parametrize('usage,done', [(False, True), (True, False)])
def test_missing_or_unfinished_usage_stops_with_reserved_cost(server, budget, tmp_path, usage, done):
    server.scripts = [response('execute_candidate', execute(), usage=usage, done=done)]
    events = []
    with pytest.raises(ProtocolError):
        planner(server, budget, tmp_path).decide(context(), validate_action=validate, on_event=events.append)
    snapshot = budget.snapshot()
    assert len(server.bodies) == 1
    assert snapshot['global']['known_tokens'] == 0
    assert snapshot['global']['unresolved_reserved_tokens'] > 0
    assert not events_of(events, 'model_output_accepted')
    if usage:
        assert snapshot['attempts'][0]['reported_tokens'] == 17


@pytest.mark.parametrize('error_type', ['ConnectError', 'ConnectTimeout'])
def test_unsent_connection_failure_retries_once_then_succeeds(server, budget, tmp_path, error_type):
    import httpx2 as httpx

    class FailOnce(httpx.AsyncHTTPTransport):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            if self.calls == 1:
                raise getattr(httpx, error_type)('refused before headers')
            return await super().handle_async_request(request)

    transport = FailOnce()
    server.scripts = [response('execute_candidate', execute())]
    action = planner(server, budget, tmp_path, inner_transport=transport).decide(
        context(), validate_action=validate)
    snapshot = budget.snapshot()
    assert transport.calls == 2
    assert len(server.bodies) == 1
    assert action == execute()
    assert [item['status'] for item in snapshot['attempts']] == ['not_sent', 'settled']
    assert snapshot['attempts'][0]['usage_status'] == 'not_sent'
    assert snapshot['attempts'][1]['usage_status'] == 'known'
    assert snapshot['global']['unresolved_reserved_tokens'] == 0


def test_unsent_retry_leaves_the_semantic_correction(server, budget, tmp_path):
    import httpx2 as httpx

    class FailOnce(httpx.AsyncHTTPTransport):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError('refused before headers')
            return await super().handle_async_request(request)

    transport = FailOnce()
    server.scripts = [response('execute_candidate', execute(SECRET)),
                      response('execute_candidate', execute())]
    action = planner(server, budget, tmp_path, inner_transport=transport).decide(
        context(), validate_action=validate)
    assert transport.calls == 3
    assert len(server.bodies) == 2
    assert action == execute()
    assert [item['status'] for item in budget.snapshot()['attempts']] == ['not_sent', 'settled', 'settled']


def test_sent_connection_failure_is_not_resent(server, budget, tmp_path):
    import httpx2 as httpx

    class AlreadySent(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            raise httpx.ConnectError('reset after send')

    transport = AlreadySent()
    with pytest.raises(RequestFailure) as failure:
        planner(server, budget, tmp_path, inner_transport=transport).decide(
            context(), validate_action=validate)
    assert failure.value.category == 'connection_failure'
    assert transport.calls == 1
    assert server.bodies == []
    assert len(budget.snapshot()['attempts']) == 1


@pytest.mark.parametrize('status,category', [(401, 'authentication_or_permission'), (503, 'temporary_service')])
def test_provider_error_has_no_hidden_sdk_retry_or_secret_public_error(server, budget, tmp_path, status, category):
    server.scripts = [{'status': status}]
    with pytest.raises(RequestFailure) as failure:
        planner(server, budget, tmp_path).decide(context(), validate_action=validate)
    assert failure.value.category == category
    assert SECRET not in str(failure.value)
    assert len(server.bodies) == 1
    assert budget.snapshot()['global']['calls'] == 1


@pytest.mark.parametrize('cancel', [True, False])
def test_continuous_heartbeats_respect_cancel_and_total_deadline(server, budget, tmp_path, cancel):
    server.scripts = [{'heartbeat': True}]
    event = threading.Event()
    timer = threading.Timer(.15, event.set) if cancel else None
    limits = RequestLimits(connect=.5, idle=.6, total=.3, heartbeat=.05)
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RequestFailure) as failure:
            planner(server, budget, tmp_path, request_limits=limits).decide(
                context(), validate_action=validate, cancel_event=event)
        assert failure.value.category == ('cancelled' if cancel else 'total_timeout')
        assert time.monotonic() - started < 2
        assert len(server.bodies) == 1
        assert budget.snapshot()['global']['unresolved_reserved_tokens'] > 0
    finally:
        if timer:
            timer.cancel()


def test_budget_exhaustion_blocks_the_network_dispatch(server, tmp_path):
    ledger = ModelRequestBudget(tmp_path / 'single-call.sqlite', global_tokens=500000,
        global_calls=1, scope='offline-planner', scope_tokens=500000, scope_calls=1)
    try:
        ledger.settle(ledger.reserve({'messages': [], 'max_tokens': 10}), {'total_tokens': 1})
        with pytest.raises(ProtocolError) as failure:
            planner(server, ledger, tmp_path).decide(context(), validate_action=validate)
        assert failure.value.category == 'request_budget_exhausted'
        assert server.bodies == []
        assert ledger.snapshot()['global']['calls'] == 1
    finally:
        ledger.close()


@pytest.fixture
def controller(tmp_path):
    from popper.capabilities import extra_available
    if not extra_available('analysis'):
        pytest.skip('analysis extra required for independent polynomial evaluator')
    from test_unit_comparison import polynomial_project
    from popper.research.controller import ResearchController
    project = tmp_path / 'polynomial'
    polynomial_project(project)
    initialize(project)
    ResearchController.initialize(project, tmp_path / 'research')
    instance = ResearchController(tmp_path / 'research')
    yield instance
    instance.close()


def test_controller_handoff_persists_rejected_and_accepted_responses_without_duplicate_experiment(
        server, budget, tmp_path, controller):
    runtime = controller.planning_runtime(max_experiments=1)
    hid = controller.manifest['candidate_hypothesis_ids'][0]
    server.scripts = [response('execute_candidate', execute(SECRET)),
                      response('execute_candidate', execute(hid, assessment={'score': 999}))]
    client = planner(server, budget, tmp_path)
    result = runtime.run(client, trusted_local=True)
    assert result['planner']['status'] == 'stopped'
    assert result['planner']['reason'] == 'experiment_limit'
    assert len(result['development']['executions']) == 1
    assert result['finalist']['hypothesis_id'] == hid
    assert result['assessment_rejections'] == 1
    assert result['agent_assessments'] == []
    assert result['integrity']['ok']
    assert len(controller.store.list('run')) == 2  # baseline plus candidate, one seed each
    assert len(runtime.records('model_response_received')) == 2
    assert len(runtime.records('model_response_rejected')) == 1
    before = {kind: len(controller.store.list(kind)) for kind in ('run', 'observation', 'runtime_record')}
    restored = controller.planning_runtime(max_experiments=1).run(client, trusted_local=True)
    assert restored == result
    assert before == {kind: len(controller.store.list(kind)) for kind in before}
    assert len(server.bodies) == 2
    assert budget.snapshot()['global']['known_tokens'] == 34

