"""Offline request-wire checks; no credentials or paid provider requests."""
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from popper.chat import make_chat_client, request_output_limits
from popper.harness.conversation import adapt_conversation_harness
from tests import test_campaign_dispatch as dispatch_fixtures


class ProviderLimitsTests(unittest.TestCase):
    def test_qianwen_reasoning_and_total_output_are_bounded(self):
        expected = {'max_completion_tokens': 4096, 'enable_thinking': True, 'thinking_budget': 1024}
        self.assertEqual(expected, request_output_limits(
            'https://maas.qianwenaiapi.com/compatible-mode/v1', 'qwen3.8-flash'))
        captured = []
        frames = [ {'choices': [{'delta': {'content': 'synthetic answer'}}]},
                   {'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}} ]
        data = ''.join('data: '+json.dumps(f)+'\n\n' for f in frames)+'data: [DONE]\n\n'
        def open_request(request, **kwargs):
            captured.append(json.loads(request.data))
            return io.BytesIO(data.encode())
        with patch.dict(os.environ, {'POPPER_API_KEY': 'synthetic-placeholder'}), patch(
            'popper.chat.urllib.request.build_opener', return_value=SimpleNamespace(open=open_request)):
            client = make_chat_client('https://maas.qianwenaiapi.com/compatible-mode/v1', 'qwen3.8-flash')
            self.assertEqual('synthetic answer', client('synthetic system', [{'role': 'user', 'content': 'synthetic input'}]))
        body = captured[0]
        self.assertNotIn('max_tokens', body)
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual(expected, {key: body[key] for key in expected})
        self.assertEqual(expected, adapt_conversation_harness(client).context_request_limits)

    def test_provider_limits_do_not_leak_to_other_hosts_or_models(self):
        for url, model in [('https://other.example/v1', 'qwen3.8-flash'),
                           ('https://maas.qianwenaiapi.com/compatible-mode/v1', 'other-model'),
                           ('https://maas.qianwenaiapi.com.evil.example/v1', 'qwen3.8-flash')]:
            with self.subTest(url=url, model=model):
                self.assertEqual({'max_tokens': 4096}, request_output_limits(url, model))


class PersistedLimitsTests(unittest.TestCase):
    setUp = dispatch_fixtures.CampaignDispatchTests.setUp
    tearDown = dispatch_fixtures.CampaignDispatchTests.tearDown
    send = dispatch_fixtures.CampaignDispatchTests.send

    def test_budget_and_execution_use_the_same_persisted_limits(self):
        from popper import task_context
        from popper.sessions import AgentSessionStore
        from popper.research.agent_evaluation_budget import AgentEvaluationBudget
        limits = {'max_completion_tokens': 4096, 'enable_thinking': True, 'thinking_budget': 1024}
        self.ws.chat_harness.context_request_limits = limits
        result = self.send()
        self.assertEqual('completed', result['turn']['status'])
        store, ledger = AgentSessionStore(self.project), AgentEvaluationBudget(self.path)
        try:
            frozen = task_context.load_input(store, result['turn']['turn_id'])
            reserved = json.loads(ledger.connection.execute('SELECT snapshot FROM requests').fetchone()[0])
            self.assertEqual(limits, frozen['request_limits'])
            self.assertEqual(frozen, reserved)
            self.assertEqual(1, len(self.calls))
        finally:
            store.close()
            ledger.close()

    def test_changed_limits_reject_queued_snapshot_before_provider_call(self):
        from popper.api_contracts import new_trace_id
        from popper.sessions import AgentSessionStore
        self.ws.chat_harness.context_request_limits = {'max_completion_tokens': 4096, 'thinking_budget': 1024}
        with patch.object(self.ws, '_schedule_agent_job') as schedule:
            launched = self.ws.launch_session_turn(self.session['session_id'], 'synthetic queued input',
                'changed-limits-fixture', new_trace_id())
        self.ws.chat_harness.context_request_limits = {'max_completion_tokens': 4096, 'thinking_budget': 2048}
        self.ws._schedule_agent_job(*schedule.call_args.args, **schedule.call_args.kwargs)
        self.assertEqual(0, self.ws.wait_for_jobs(timeout=10))
        store = AgentSessionStore.readonly(self.project)
        try:
            turn = store.get_turn(launched['turn']['turn_id'])
            self.assertEqual('failed', turn['status'])
            self.assertIn('限制已变化', turn['error']['message'])
        finally:
            store.close()
        self.assertEqual([], self.calls)
