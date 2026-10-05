"""Loopback-only model gateway contract tests; no real provider is contacted."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, canonical
from popper.model_gateway import GatewayBlocked, GatewayConflict, ModelGateway


def frame(value):
    return ("data: " + (value if isinstance(value, str) else json.dumps(value)) + "\n\n").encode()


def complete_frames(*, usage=None, content="test answer", reason="stop"):
    return [frame({"id": "fake-response-1", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}),
            frame({"id": "fake-response-1", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}),
            frame({"choices": [], "usage": usage or {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}),
            frame("[DONE]")]


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.chunks = complete_frames()
        self.started = threading.Event()
        self.release = None
        self.content_type = "text/event-stream"
        self.status = 200
        self.location = None
        self.proxy = self
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                owner.calls.append({"request": json.loads(raw), "raw": raw,
                                    "authorization": self.headers.get("Authorization"), "path": self.path})
                owner.started.set()
                if owner.release is not None:
                    owner.release.wait(5)
                self.send_response(owner.status)
                self.send_header("Content-Type", owner.content_type)
                if owner.location:
                    self.send_header("Location", owner.location)
                self.end_headers()
                for chunk in owner.chunks:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def close(self):
        if self.release:
            self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "gateway.sqlite3"
        self.provider = FakeProvider()
        self.reservations, self.settlements = [], []
        self.key = "gateway-host-only-private-value"
        self.env = patch.dict(os.environ, {"GATEWAY_TEST_KEY": self.key})
        self.env.start()
        self.gateway = self.make_gateway()

    def tearDown(self):
        self.provider.close()
        self.env.stop()
        self.tmp.cleanup()

    def reserve(self, request_id, snapshot):
        self.reservations.append((request_id, copy.deepcopy(snapshot)))
        return {"reservation_id": "reservation-" + request_id}

    def settle(self, request_id, usage):
        self.settlements.append((request_id, copy.deepcopy(usage)))

    def make_gateway(self, **kwargs):
        options = {"reserve": self.reserve, "settle": self.settle,
                   "api_key_env": "GATEWAY_TEST_KEY", "timeout": 3}
        options.update(kwargs)
        return ModelGateway(self.path, self.provider.base_url, "synthetic-model", **options)

    def request(self):
        return {"model": "synthetic-model", "messages": [{"role": "user", "content": "synthetic material"}],
                "tools": [{"type": "function", "function": {"name": "project_read", "parameters": {
                    "type": "object", "properties": {"api_key": {"type": "string"}}}}}],
                "max_tokens": 128, "stream": True, "stream_options": {"include_usage": True}}

    def forward(self, request=None, key="request-1", **kwargs):
        return self.gateway.forward("project-1", "run-1", key, request or self.request(), **kwargs)

    def test_complete_sse_exact_snapshot_and_usage(self):
        events = []
        receipt = self.forward(on_event=events.append)
        self.assertEqual((receipt["state"], receipt["outcome"]), ("settled", "succeeded"))
        self.assertEqual(receipt["usage"], {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        self.assertEqual(events[-1], "[DONE]")
        self.assertEqual(self.provider.calls[0]["path"], "/v1/chat/completions")
        self.assertEqual(self.provider.calls[0]["authorization"], "Bearer " + self.key)
        self.assertEqual(self.reservations[0][1], receipt["snapshot"])
        self.assertEqual(self.provider.calls[0]["raw"], canonical(receipt["snapshot"]["request"]).encode())
        self.assertEqual(self.settlements, [(receipt["request_id"], receipt["usage"])])
        self.assertNotIn(self.key, canonical(receipt))
        self.assertEqual(receipt["result"]["event_count"], 3)
        self.assertNotIn("test answer", canonical(receipt["result"]))

    def test_done_is_relayed_only_after_durable_settlement(self):
        outcomes = []
        def relay(event):
            if event == "[DONE]":
                with sqlite3.connect(self.path) as conn:
                    state = conn.execute("SELECT state FROM gateway_requests").fetchone()[0]
                outcomes.append((state, len(self.settlements)))
        self.forward(on_event=relay)
        self.assertEqual(outcomes, [("settled", 1)])

    def test_credentials_json_strings_redacted_tools_not_rewritten(self):
        original = self.request()
        original["messages"] += [{"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "source_read", "arguments": json.dumps({"secret": "nested private", "plain": "retained", "child": {"password": "double\\escaped\" private"}})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "host credential " + self.key}]
        before = copy.deepcopy(original)
        receipt = self.forward(original)
        wire = self.provider.calls[0]["request"]
        self.assertEqual(original, before)
        self.assertEqual(wire["tools"], original["tools"])
        arguments = wire["messages"][1]["tool_calls"][0]["function"]["arguments"]
        self.assertIsInstance(arguments, str)
        self.assertEqual(json.loads(arguments), {"secret": "[REDACTED]", "plain": "retained", "child": {"password": "[REDACTED]"}})
        self.assertNotIn(self.key, canonical(receipt))
        self.assertNotIn("nested private", self.path.read_bytes().decode("utf-8", "ignore"))
        self.assertIn("[REDACTED]", wire["messages"][2]["content"])

    def test_credential_in_tool_schema_rejected_not_rewritten(self):
        request = self.request()
        request["tools"][0]["function"]["description"] = self.key
        with self.assertRaisesRegex(ProtocolError, "tool definitions"):
            self.forward(request)
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.reservations)

    def test_host_secret_cannot_be_routing_metadata_or_invalid_header(self):
        for position in range(3):
            with self.subTest(position=position):
                identifiers = ["project", "run", "idempotency"]
                identifiers[position] = self.key
                with self.assertRaisesRegex(ProtocolError, "routing metadata"):
                    self.gateway.forward(*identifiers, self.request())
        with patch.dict(os.environ, {"GATEWAY_TEST_KEY": "unsafe\r\nheader"}):
            with self.assertRaisesRegex(ProtocolError, "authorization credential"):
                self.forward()
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.reservations)

    def test_auxiliary_calls_use_same_reservation_and_settlement(self):
        for index, purpose in enumerate(("primary", "title", "compaction", "summary")):
            result = self.forward(key="purpose-" + str(index), purpose=purpose)
            self.assertEqual(result["purpose"], purpose)
            self.assertEqual(result["state"], "settled")
        self.assertEqual(len(self.provider.calls), 4)
        self.assertEqual(len(self.reservations), 4)
        self.assertEqual(len(self.settlements), 4)

    def test_repeat_and_reopen_return_receipt_without_redispatch(self):
        first = self.forward()
        self.gateway = self.make_gateway()
        repeated = self.forward()
        self.assertEqual(repeated, first)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.reservations), 1)
        altered = self.request()
        altered["messages"][0]["content"] = "a different goal"
        with self.assertRaises(GatewayConflict):
            self.forward(altered)
        self.assertEqual(len(self.provider.calls), 1)

    def test_concurrent_duplicate_does_not_forward_twice(self):
        self.provider.release = threading.Event()
        result, errors = [], []
        def invoke():
            try:
                result.append(self.forward())
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertTrue(self.provider.started.wait(3))
        second = self.forward()
        self.assertEqual(second["state"], "sending")
        with self.assertRaisesRegex(GatewayBlocked, "still sending"):
            self.gateway.forward("project-1", "run-other", "parallel", self.request())
        self.provider.release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(result[0]["state"], "settled")

    def test_missing_usage_blocks_same_project_other_run_after_restart(self):
        self.provider.chunks = complete_frames()[:2] + [frame("[DONE]")]
        failed = self.forward()
        self.assertEqual((failed["state"], failed["error"]["kind"]), ("unknown", "unknown_usage"))
        self.assertIsNone(failed["usage"])
        self.assertEqual(self.settlements, [(failed["request_id"], None)])
        self.gateway = self.make_gateway()
        with self.assertRaises(GatewayBlocked):
            self.gateway.forward("project-1", "different-run", "different-id", self.request(), purpose="summary")
        self.assertEqual(self.forward(), failed)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(self.gateway.project_status("project-1")["blocked"])
        # A separate project does not bypass a shared ledger; the injected
        # callback remains responsible for shared-account policy.
        self.provider.chunks = complete_frames()
        other = self.gateway.forward("project-2", "run-2", "new", self.request())
        self.assertEqual(other["state"], "settled")

    def test_truncated_stream_usage_is_not_promoted_to_known(self):
        self.provider.chunks = complete_frames()[:-1]
        receipt = self.forward()
        self.assertEqual(receipt["state"], "unknown")
        self.assertIsNone(receipt["usage"])
        self.assertEqual(receipt["error"]["kind"], "incomplete_stream")
        self.assertFalse(receipt["result"]["stream_completed"])

    def test_invalid_usage_never_becomes_zero(self):
        for index, invalid in enumerate(({"total_tokens": True}, {"total_tokens": -1},
                                         {"total_tokens": 2, "prompt_tokens": 3},
                                         {"total_tokens": 7, "completion_tokens": "2"})):
            with self.subTest(usage=invalid):
                self.provider.chunks = complete_frames(usage=invalid)
                receipt = self.gateway.forward("invalid-project-" + str(index), "run", "bad", self.request())
                self.assertEqual(receipt["state"], "unknown")
                self.assertIsNone(receipt["usage"])

    def test_known_usage_limited_or_empty_reply_still_settles(self):
        for index, (content, reason) in enumerate((("partial", "length"), ("", "stop"), ("", "content_filter"))):
            with self.subTest(reason=reason):
                self.provider.chunks = complete_frames(content=content, reason=reason)
                receipt = self.forward(key="limited-" + str(index))
                self.assertEqual((receipt["state"], receipt["outcome"]), ("settled", "failed"))
                self.assertEqual(receipt["usage"]["total_tokens"], 10)
        self.assertFalse(self.gateway.project_status("project-1")["blocked"])

    def test_cancel_with_complete_known_usage_does_not_reverse_outcome(self):
        cancelled = threading.Event()
        events = []
        def relay(event):
            events.append(event)
            cancelled.set()
        receipt = self.forward(on_event=relay, cancel_event=cancelled)
        self.assertEqual((receipt["state"], receipt["outcome"]), ("settled", "cancelled"))
        self.assertEqual(receipt["usage"]["total_tokens"], 10)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.forward(), receipt)

    def test_disconnect_drains_for_usage_without_changing_cancelled(self):
        def broken_client(event):
            raise BrokenPipeError("local browser closed")
        receipt = self.forward(on_event=broken_client)
        self.assertEqual((receipt["state"], receipt["outcome"]), ("settled", "cancelled"))
        self.assertTrue(receipt["result"]["client_disconnected"])
        self.assertEqual(receipt["usage"]["total_tokens"], 10)

    def test_pre_cancelled_is_not_sent_or_reserved(self):
        cancellation = threading.Event()
        cancellation.set()
        receipt = self.forward(cancel_event=cancellation)
        self.assertEqual((receipt["state"], receipt["outcome"]), ("not_sent", "cancelled"))
        self.assertFalse(self.reservations)
        self.assertFalse(self.provider.calls)

    def test_cancel_during_reservation_stops_before_actual_dispatch(self):
        cancellation = threading.Event()
        def reserve_and_cancel(request_id, snapshot):
            cancellation.set()
            return self.reserve(request_id, snapshot)
        self.gateway = self.make_gateway(reserve=reserve_and_cancel)
        receipt = self.forward(cancel_event=cancellation)
        self.assertEqual((receipt["state"], receipt["outcome"]), ("not_sent", "cancelled"))
        self.assertEqual(receipt["usage"]["total_tokens"], 0)
        self.assertEqual(len(self.reservations), 1)
        self.assertFalse(self.provider.calls)

    def test_budget_rejection_is_not_sent_and_does_not_retry(self):
        def exhausted(*args):
            raise ProtocolError("request ceiling exhausted")
        self.gateway = self.make_gateway(reserve=exhausted)
        receipt = self.forward()
        self.assertEqual(receipt["state"], "not_sent")
        self.assertEqual(receipt["error"]["kind"], "budget_rejected")
        self.assertEqual(self.forward(), receipt)
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.settlements)

    def test_uncertain_reservation_blocks_without_upstream_call(self):
        def uncertain(*args):
            raise RuntimeError("commit might have happened")
        self.gateway = self.make_gateway(reserve=uncertain)
        receipt = self.forward()
        self.assertEqual((receipt["state"], receipt["error"]["kind"]), ("unknown", "reservation_uncertain"))
        self.assertFalse(self.provider.calls)
        with self.assertRaises(GatewayBlocked):
            self.forward(key="next")

    def test_settlement_error_preserves_known_usage_and_blocks(self):
        def settlement_crashed(*args):
            raise RuntimeError("ledger not confirmed")
        self.gateway = self.make_gateway(settle=settlement_crashed)
        receipt = self.forward()
        self.assertEqual((receipt["state"], receipt["error"]["kind"]), ("unknown", "settlement_uncertain"))
        self.assertEqual(receipt["usage"]["total_tokens"], 10)
        with self.assertRaises(GatewayBlocked):
            self.forward(key="next")

    def test_startup_recovery_marks_sending_unknown_no_dispatch(self):
        done = self.forward()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE gateway_requests SET state='sending' WHERE request_id=?", (done["request_id"],))
        self.gateway = self.make_gateway()
        recovered = self.gateway.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertEqual((recovered[0]["state"], recovered[0]["outcome"]), ("unknown", "interrupted"))
        self.assertEqual(len(self.provider.calls), 1)
        with self.assertRaises(GatewayBlocked):
            self.forward(key="next")

    def test_live_recovery_rejected_without_corrupting_dispatch(self):
        self.provider.release = threading.Event()
        output = []
        worker = threading.Thread(target=lambda: output.append(self.forward()))
        worker.start()
        self.assertTrue(self.provider.started.wait(3))
        with self.assertRaises(GatewayBlocked):
            self.gateway.recover_interrupted()
        self.provider.release.set()
        worker.join(5)
        self.assertEqual(output[0]["state"], "settled")

    def test_snapshot_is_immutable_in_database(self):
        receipt = self.forward()
        with sqlite3.connect(self.path) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                conn.execute("UPDATE gateway_requests SET snapshot_json='{}' WHERE request_id=?", (receipt["request_id"],))

    def test_boundaries_and_fixed_endpoint_are_checked_before_reserving(self):
        changes = ({"max_tokens": 0}, {"stream": False}, {"n": 2}, {"model": "other"},
                   {"stream_options": {"include_usage": False}}, {"messages": [{"role": "user", "content": "x" * 65536}]})
        for change in changes:
            with self.subTest(change=list(change)):
                request = self.request()
                request.update(change)
                with self.assertRaises(ProtocolError):
                    self.forward(request)
        for url in ("http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?key=x"):
            with self.assertRaises(ProtocolError):
                ModelGateway(self.path, url, "synthetic-model", reserve=self.reserve, settle=self.settle)
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.reservations)

    def test_non_sse_and_redirect_fail_closed_no_secret_in_receipt(self):
        self.provider.content_type = "application/json"
        self.provider.chunks = [json.dumps({"error": "sensitive " + self.key}).encode()]
        receipt = self.forward()
        self.assertEqual(receipt["state"], "unknown")
        self.assertNotIn(self.key, canonical(receipt))
        self.provider.status = 302
        self.provider.location = "http://127.0.0.1:1/credential-theft"
        redirected = self.gateway.forward("project-2", "run", "redirect", self.request())
        self.assertEqual(redirected["state"], "unknown")
        self.assertEqual(len(self.provider.calls), 2)

    def test_upstream_host_key_echo_and_error_body_do_not_escape(self):
        self.provider.chunks = complete_frames(content="echo " + self.key)
        events = []
        self.forward(on_event=events.append)
        self.assertNotIn(self.key, canonical(events))
        self.assertIn("[REDACTED]", canonical(events))
        self.provider.chunks = [frame({"error": {"message": self.key}, "usage": {"total_tokens": 2}}), frame("[DONE]")]
        events.clear()
        receipt = self.forward(key="provider-error", on_event=events.append)
        self.assertEqual((receipt["state"], receipt["outcome"]), ("settled", "failed"))
        self.assertNotIn(self.key, canonical(events))
        self.assertEqual(receipt["usage"]["total_tokens"], 2)
        self.assertEqual(receipt["error"]["kind"], "provider_error")
        self.assertNotIn(self.key, canonical(receipt))
        self.assertIn("[REDACTED]", receipt["error"]["detail"])

    def test_native_tool_delta_and_arguments_are_not_reinterpreted(self):
        arguments = json.dumps({"api_key": "not-a-host-secret", "text": "exact source"})
        delta = {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {
            "name": "project_edit", "arguments": arguments}}]}
        self.provider.chunks = [frame({"choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls"}]}),
                                frame({"choices": [], "usage": {"total_tokens": 3}}), frame("[DONE]")]
        events = []
        receipt = self.forward(on_event=events.append)
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(events[0]["choices"][0]["delta"], delta)
        self.assertNotIn("exact source", canonical(receipt["result"]))

    def test_split_host_secret_is_not_relayed_in_text_or_tool_arguments(self):
        for index, kind in enumerate(("text", "arguments")):
            with self.subTest(kind=kind):
                parts = [self.key[:12], self.key[12:20], self.key[20:]]
                if kind == "text":
                    deltas = [{"content": part} for part in parts]
                else:
                    deltas = [{"tool_calls": [{"index": 0, "function": {"arguments": part}}]} for part in parts]
                self.provider.chunks = [frame({"choices": [{"delta": delta, "finish_reason": None}]}) for delta in deltas]
                self.provider.chunks += [frame({"choices": [{"delta": {}, "finish_reason": "stop" if kind == "text" else "tool_calls"}]}),
                                         frame({"choices": [], "usage": {"total_tokens": 8}}), frame("[DONE]")]
                events = []
                receipt = self.forward(key="split-secret-" + str(index), on_event=events.append)
                self.assertEqual(receipt["state"], "settled")
                pieces = []
                for event in events:
                    if isinstance(event, dict) and event.get("choices"):
                        delta = event["choices"][0]["delta"]
                        pieces += [delta.get("content", "")] if kind == "text" else [item.get("function", {}).get("arguments", "") for item in delta.get("tool_calls", [])]
                self.assertEqual("".join(pieces), "[REDACTED]")

    def test_guard_preserves_ordinary_delta_prefix_until_finish(self):
        self.provider.chunks = [frame({"choices": [{"delta": {"content": "ending g"}, "finish_reason": None}]}),
                                frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
                                frame({"choices": [], "usage": {"total_tokens": 3}}), frame("[DONE]")]
        events = []
        self.forward(on_event=events.append)
        combined = "".join(event["choices"][0]["delta"].get("content", "") for event in events if isinstance(event, dict) and event.get("choices"))
        self.assertEqual(combined, "ending g")

    def test_escaped_host_credential_is_redacted_after_json_decoding(self):
        unusual_key = 'host-quote-"-slash-\\-secret'
        with patch.dict(os.environ, {"GATEWAY_TEST_KEY": unusual_key}):
            self.provider.chunks = complete_frames(content=unusual_key)
            events = []
            self.forward(on_event=events.append)
            self.assertNotIn(unusual_key, canonical(events))
            self.assertIn("[REDACTED]", canonical(events))

    def test_multiline_sse_and_comments_are_supported(self):
        self.provider.chunks = [b": ping\nevent: message\nid: fake\ndata: {\"choices\": [{\"delta\":\n",
                                b"data: {\"content\": \"answer\"}, \"finish_reason\": \"stop\"}]}\n\n",
                                frame({"usage": {"total_tokens": 4}, "choices": []}), frame("[DONE]")]
        receipt = self.forward()
        self.assertEqual((receipt["state"], receipt["outcome"]), ("settled", "succeeded"))
        self.assertEqual(receipt["usage"]["total_tokens"], 4)


if __name__ == "__main__":
    unittest.main()
