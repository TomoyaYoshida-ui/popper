import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from popper.chat import ChatCancelled, ChatReplyError, make_chat_client
from popper.core import ProtocolError
from popper.model_budget import ModelRequestBudget
from popper.request_lifecycle import RequestLimits, RequestFailure
from popper.scoop import make_json_client


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.bodies.append(body)
        mode = self.server.mode
        try:
            if mode == 'headers': time.sleep(2)
            if mode == 'auth':
                self.send_response(401); self.end_headers(); return
            if mode == 'retry' and len(self.server.bodies) == 1:
                self.send_response(503); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            if mode == 'metadata':
                self.wfile.write(b'event: message\nid: synthetic\nretry: 1000\n')
            if mode == 'partial':
                self.wfile.write(b'data: {'); self.wfile.flush(); time.sleep(2); return
            if mode == 'heartbeat':
                for _ in range(30):
                    self.wfile.write(b': heartbeat\n\n'); self.wfile.flush(); time.sleep(.03)
                return
            tool = {'tool_calls': [{'index': 0, 'type': 'function', 'function':
                    {'name': 'workspace_file_read', 'arguments': '{"path":"a.py"}'}}]}
            text = '{"ok":true}' if mode != 'invalid_json' or len(self.server.bodies)>1 else '{'
            frames = [{'choices': [{'delta': tool if mode == 'tool_length' else {'content': text},
                       'finish_reason': 'length' if mode == 'tool_length' else 'stop'}]}]
            if mode != 'missing_usage': frames.append({'choices': [], 'usage': {'total_tokens': 17}})
            for frame in frames:
                if mode == 'metadata':
                    self.wfile.write(('data: '+json.dumps(frame,indent=2).replace('\n','\ndata: ')+'\n\n').encode())
                else: self.wfile.write(('data: '+json.dumps(frame)+'\n\n').encode())
            if mode != 'dropped_after_usage': self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError): pass


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.server.daemon_threads = True
        self.server.mode = 'success'; self.server.bodies = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
    def chat(self, limits=None):
        return make_chat_client(self.url, 'local', api_key='synthetic-placeholder', request_limits=limits)
    def test_valid_tool_json_is_rejected_when_truncated(self):
        self.server.mode='tool_length'
        with self.assertRaises(ChatReplyError) as error: self.chat()('test', [])
        self.assertEqual('length', error.exception.finish_reason)
        self.assertEqual(17, error.exception.usage['total_tokens'])
    def test_interruption_preserves_received_usage(self):
        self.server.mode='dropped_after_usage'
        with self.assertRaises(RequestFailure) as error: self.chat()('test', [])
        self.assertEqual(17, error.exception.usage['total_tokens'])
    def test_cancel_during_connect_and_partial_line(self):
        for mode in ('headers','partial'):
            with self.subTest(mode=mode):
                self.server.mode=mode; event=threading.Event()
                timer=threading.Timer(.1,event.set); timer.start(); start=time.monotonic()
                with self.assertRaises(ChatCancelled): self.chat()('test', [],cancel_event=event)
                self.assertLess(time.monotonic()-start, 1)
                timer.cancel()
    def test_heartbeat_does_not_reset_total_deadline(self):
        self.server.mode='heartbeat'; progress=[]; start=time.monotonic()
        with self.assertRaises(RequestFailure) as error:
            self.chat(RequestLimits(connect=.5,idle=.4,total=.25,heartbeat=.05))('test',[],on_progress=progress.append)
        self.assertEqual('total_timeout',error.exception.category)
        self.assertLess(time.monotonic()-start,1)
        self.assertTrue(any(p['state']=='waiting' for p in progress))
    def test_partial_line_idle_timeout(self):
        self.server.mode='partial'
        with self.assertRaises(RequestFailure) as error:
            self.chat(RequestLimits(connect=.5,idle=.15,total=1))('test',[])
        self.assertEqual('read_timeout',error.exception.category)
    def test_json_stream_repairs_once_and_accounts_both_attempts(self):
        self.server.mode='invalid_json'
        with tempfile.TemporaryDirectory() as tmp:
            budget=ModelRequestBudget(Path(tmp)/'calls.db',global_tokens=100000,global_calls=2,
                scope='one',scope_tokens=100000,scope_calls=2)
            call=make_json_client(self.url,'local',api_key='synthetic-placeholder',request_budget=budget)
            self.assertTrue(call('return json',{})['ok'])
            self.assertEqual(2,len(self.server.bodies))
            self.assertTrue(all(b['stream'] for b in self.server.bodies))
            self.assertEqual(34,budget.tokens_used); budget.close()
    def test_unknown_usage_prevents_second_send(self):
        self.server.mode='missing_usage'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'calls.db'
            budget=ModelRequestBudget(path,global_tokens=100000,global_calls=2,
                scope='one',scope_tokens=100000,scope_calls=2)
            call=make_json_client(self.url,'local',api_key='synthetic-placeholder',request_budget=budget)
            with self.assertRaises(ProtocolError): call('return json',{})
            row=budget.snapshot()['attempts'][0]
            self.assertEqual('unknown',row['usage_status'])
            self.assertGreater(budget.tokens_used,0)
            with self.assertRaises(ProtocolError): call('return json',{})
            self.assertEqual(1,len(self.server.bodies)); budget.close()

    def test_auth_failure_is_not_retried(self):
        self.server.mode='auth'
        with self.assertRaises(RequestFailure) as error:
            make_json_client(self.url,'local',api_key='synthetic-placeholder')('json',{})
        self.assertEqual('authentication_or_permission', error.exception.category)
        self.assertEqual(1,len(self.server.bodies))

    def test_temporary_failure_retries_only_once(self):
        self.server.mode='retry'
        self.assertTrue(make_json_client(self.url,'local',api_key='synthetic-placeholder')('json',{})['ok'])
        self.assertEqual(2,len(self.server.bodies))

    def test_cancel_during_retry_wait(self):
        self.server.mode='retry'; event=threading.Event()
        timer=threading.Timer(.15,event.set); timer.start(); start=time.monotonic()
        with self.assertRaises(RequestFailure) as error:
            make_json_client(self.url,'local',api_key='synthetic-placeholder')('json',{},cancel_event=event)
        self.assertEqual('cancelled', error.exception.category)
        self.assertLess(time.monotonic()-start,1)
        self.assertEqual(1,len(self.server.bodies)); timer.cancel()

    def test_sse_metadata_and_multiline_data_work_for_both_clients(self):
        self.server.mode='metadata'
        self.assertEqual('{"ok":true}',self.chat()('test',[]))
        self.assertTrue(make_json_client(self.url,'local',api_key='synthetic-placeholder')('json',{})['ok'])

    def test_response_headers_use_idle_timeout_after_tcp_connect(self):
        self.server.mode='headers'
        with self.assertRaises(RequestFailure) as error:
            self.chat(RequestLimits(connect=.1,idle=.2,total=1))('test',[])
        self.assertEqual('read_timeout',error.exception.category)

    def test_windows_registry_proxy_does_not_override_explicit_environment_routing(self):
        from unittest.mock import patch
        with patch('urllib.request.getproxies',return_value={'http':'http://127.0.0.1:1'}), \
             patch('urllib.request.getproxies_environment',return_value={}):
            self.assertEqual('{"ok":true}',self.chat()('test',[]))

    def test_connection_refusal_is_proven_not_sent_and_can_retry_under_strict_budget(self):
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            budget=ModelRequestBudget(Path(tmp)/'calls.db',global_tokens=100000,global_calls=1,
                scope='strict',scope_tokens=100000,scope_calls=1)
            call=make_json_client(f'http://127.0.0.1:{port}/v1','local',api_key='synthetic-placeholder',request_budget=budget)
            with self.assertRaises(RequestFailure): call('json',{})
            snapshot=budget.snapshot()
            self.assertEqual(2,len(snapshot['attempts']))
            self.assertTrue(all(r['usage_status']=='not_sent' for r in snapshot['attempts']))
            self.assertEqual(0,snapshot['global']['calls']); self.assertEqual(0,budget.tokens_used)
            budget.close()

    def test_missing_usage_does_not_lock_unrelated_interactive_turns(self):
        self.server.mode='missing_usage'
        with tempfile.TemporaryDirectory() as tmp:
            budget=ModelRequestBudget(Path(tmp)/'calls.db',global_tokens=100000,global_calls=2,
                scope='interactive',scope_tokens=100000,scope_calls=2,unknown_usage_policy='reserve')
            call=make_json_client(self.url,'local',api_key='synthetic-placeholder',request_budget=budget)
            self.assertTrue(call('json',{},logical_request_id='one')['ok'])
            self.assertTrue(call('json',{},logical_request_id='two')['ok'])
            with self.assertRaises(ProtocolError): call('json',{},logical_request_id='three')
            self.assertEqual(2,len(self.server.bodies))
            self.assertEqual(0,budget.snapshot()['global']['known_tokens'])
            self.assertGreater(budget.tokens_used,0); budget.close()

    def test_retry_before_output_retains_each_attempt_bound(self):
        self.server.mode='retry'
        with tempfile.TemporaryDirectory() as tmp:
            budget=ModelRequestBudget(Path(tmp)/'calls.db',global_tokens=100000,global_calls=2,
                scope='interactive',scope_tokens=100000,scope_calls=2,unknown_usage_policy='reserve')
            call=make_json_client(self.url,'local',api_key='synthetic-placeholder',request_budget=budget)
            self.assertTrue(call('json',{},logical_request_id='one')['ok'])
            rows=budget.snapshot()['attempts']
            self.assertEqual([1,2],[r['attempt_index'] for r in rows])
            self.assertEqual(['unknown','settled'],[r['status'] for r in rows])
            self.assertGreater(budget.tokens_used,17); budget.close()

    def test_interrupted_generation_is_not_replayed_and_keeps_partial_usage(self):
        self.server.mode='dropped_after_usage'
        with tempfile.TemporaryDirectory() as tmp:
            budget=ModelRequestBudget(Path(tmp)/'calls.db',global_tokens=100000,global_calls=2,
                scope='interactive',scope_tokens=100000,scope_calls=2,unknown_usage_policy='reserve')
            call=make_json_client(self.url,'local',api_key='synthetic-placeholder',request_budget=budget)
            with self.assertRaises(RequestFailure): call('json',{},logical_request_id='one')
            self.assertEqual(1,len(self.server.bodies))
            row=budget.snapshot()['attempts'][0]
            self.assertEqual('partial',row['usage_status'])
            self.assertEqual(17,row['reported_tokens']); self.assertIsNone(row['used_tokens'])
            self.assertEqual(1,row['output_started']); budget.close()
