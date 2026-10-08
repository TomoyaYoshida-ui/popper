"""Local HTTP/SSE failures retain accounting but never fabricate an answer."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from popper.chat import make_chat_client
from popper.harness.conversation import adapt_conversation_harness
from popper.sessions import AgentSessionStore
from tests import test_campaign_dispatch as fixture


class FailureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.server.requests += 1
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if self.server.mode == 'framing_overhead':
            for _ in range(600):
                self.wfile.write(b': heartbeat ' + b'x' * 2048 + b'\n\n')
        if self.server.mode in {'oversized_line', 'oversized_payload', 'oversized_wire'}:
            try:
                if self.server.mode == 'oversized_line':
                    self.wfile.write(b':' + b'x' * 65537 + b'\n')
                elif self.server.mode == 'oversized_wire':
                    for _ in range(4500):
                        self.wfile.write(b':' + b'x' * 2048 + b'\n')
                else:
                    for _ in range(17):
                        self.wfile.write(('data: '+json.dumps({'choices':[{'delta':{'content':'x'*16384}}]})+'\n\n').encode())
                self.wfile.flush()
            except OSError:
                pass  # Expected client-side protective close; never retry.
            return
        delta = {'reasoning_content': 'synthetic private reasoning, not an assistant answer'}
        if self.server.mode == 'malformed_tool':
            delta = {'tool_calls': [{'index': 0, 'type': 'function', 'function': {
                'name': 'workspace_file_read', 'arguments': '{'}}]}
        if self.server.mode == 'framing_overhead':
            delta = {'content': 'synthetic reply with bounded payload'}
        frames = [{'id': 'synthetic-provider-response', 'choices': [{'delta': delta}]},
                  {'choices': [{'delta': {}, 'finish_reason': 'stop' if self.server.mode == 'framing_overhead' else 'length'}]}]
        if self.server.mode != 'missing_usage':
            frames.append({'choices': [], 'usage': {'prompt_tokens': 80,
                'completion_tokens': 20, 'total_tokens': 100}})
        for frame in frames:
            self.wfile.write(('data: ' + json.dumps(frame) + '\n\n').encode())
        if self.server.mode != 'dropped_after_usage':
            self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


class ProviderFailureAccountingTests(unittest.TestCase):
    def setUp(self):
        fixture.CampaignDispatchTests.setUp(self)
        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), FailureHandler)
        self.provider.mode = 'reasoning_only'
        self.provider.requests = 0
        self.thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        self.thread.start()
        with patch.dict(os.environ, {'POPPER_API_KEY': 'synthetic-placeholder'}):
            client = make_chat_client(f'http://127.0.0.1:{self.provider.server_port}/v1',
                                     'synthetic-model', enable_file_tools=True)
        self.ws.chat_harness = adapt_conversation_harness(client)
        self.ws.chat_model = 'synthetic-model'
        self.session = self.ws.create_agent_session('failure accounting')

    def tearDown(self):
        fixture.CampaignDispatchTests.tearDown(self)
        self.provider.shutdown()
        self.provider.server_close()
        self.thread.join(timeout=2)

    def send(self):
        from popper.api_contracts import new_trace_id
        self.counter += 1
        result = self.ws.launch_session_turn(self.session['session_id'], 'synthetic failure accounting',
                                            f'failure-accounting-{self.counter:04d}', new_trace_id())
        self.assertEqual(0, self.ws.wait_for_jobs(timeout=10))
        store = AgentSessionStore.readonly(self.project)
        try:
            return {'turn': store.get_turn(result['turn']['turn_id'])}
        finally:
            store.close()

    def status(self):
        return fixture.CampaignDispatchTests.status(self)

    def test_complete_reasoning_only_stream_is_failed_and_known_usage_settled(self):
        result = self.send()
        self.assertEqual('failed', result['turn']['status'])
        error = result['turn']['error']
        self.assertEqual('length', error['provider']['finish_reason'])
        self.assertEqual('synthetic-provider-response', error['provider']['response_id'])
        self.assertEqual(100, error['provider']['usage']['total_tokens'])
        self.assertEqual(100, self.status()['used_tokens'])
        self.assertEqual(0, self.status()['unresolved_requests'])
        self.assertEqual(1, self.provider.requests)
        store = AgentSessionStore(self.project)
        try:
            self.assertEqual(100, store.usage_summary(self.session['session_id'])['total_tokens'])
            self.assertNotIn('synthetic private reasoning', str(store.chat_messages(self.session['session_id'])))
        finally:
            store.close()

    def test_missing_usage_stays_unknown_and_blocks_following_dispatch(self):
        self.provider.mode = 'missing_usage'
        self.assertEqual('failed', self.send()['turn']['status'])
        self.assertEqual(1, self.status()['unresolved_requests'])
        self.assertEqual('failed', self.send()['turn']['status'])
        self.assertEqual(1, self.provider.requests)

    def test_partial_stream_usage_is_not_promoted_to_final_accounting(self):
        self.provider.mode = 'dropped_after_usage'
        result = self.send()
        self.assertEqual('failed', result['turn']['status'])
        self.assertNotIn('provider', result['turn']['error'])
        self.assertEqual('partial',result['turn']['error']['transport']['usage_status'])
        self.assertEqual(100,result['turn']['error']['partial_provider_usage']['total_tokens'])
        self.assertEqual(1, self.status()['unresolved_requests'])

    def test_malformed_native_tool_keeps_known_usage_and_no_tool_is_executed(self):
        self.provider.mode = 'malformed_tool'
        self.assertEqual('failed', self.send()['turn']['status'])
        self.assertEqual(100, self.status()['used_tokens'])
        self.assertEqual(0, self.status()['unresolved_requests'])
        store = AgentSessionStore(self.project)
        try:
            self.assertEqual([], store.tool_calls(self.session['session_id']))
        finally:
            store.close()

    def test_large_framing_small_reply_completes_and_settles(self):
        self.provider.mode = 'framing_overhead'
        result = self.send()
        self.assertEqual('completed', result['turn']['status'])
        self.assertEqual(100, self.status()['used_tokens'])
        self.assertEqual(0, self.status()['unresolved_requests'])
        self.assertEqual(1, self.provider.requests)

    def test_oversized_line_keeps_usage_unknown(self):
        self.provider.mode = 'oversized_line'
        self.assertEqual('failed', self.send()['turn']['status'])
        self.assertEqual(1, self.status()['unresolved_requests'])

    def test_oversized_payload_keeps_usage_unknown(self):
        self.provider.mode = 'oversized_payload'
        self.assertEqual('failed', self.send()['turn']['status'])
        self.assertEqual(1, self.status()['unresolved_requests'])

    def test_oversized_wire_keeps_usage_unknown(self):
        self.provider.mode = 'oversized_wire'
        self.assertEqual('failed', self.send()['turn']['status'])
        self.assertEqual(1, self.status()['unresolved_requests'])
