"""Real HTTP provider -> model client -> durable Workstation -> SSE checks.

The upstream provider is deliberately synthetic. No paid requests are made.
"""
import json
import os
import threading
import unittest
import urllib.request
from unittest.mock import patch

from popper.chat import make_chat_client
from popper.research.agent_evaluation_budget import AgentEvaluationBudget
from scripts.workbench_acceptance_server import MockProvider
from tests import test_server_chat as chat_http


class WorkbenchTransportTests(unittest.TestCase):
    _start = chat_http.ChatHttpTests._start
    _stop = chat_http.ChatHttpTests._stop
    _get = chat_http.ChatHttpTests._get
    _post = chat_http.ChatHttpTests._post

    def setUp(self):
        self.provider = MockProvider()
        self.provider_thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        self.provider_thread.start()
        endpoint = f"http://127.0.0.1:{self.provider.server_port}/v1"
        with patch.dict(os.environ, {"POPPER_API_KEY": "synthetic-test-placeholder"}):
            client = make_chat_client(endpoint, "mock", enable_file_tools=True)
        self._start(chat_llm=client, chat_kwargs={"trusted_local": True})
        (self.root / "notes.txt").write_text("Synthetic marker 42.\n", encoding="utf-8")
        _, session = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "transport acceptance", "harness": "chat"})
        self.session = session["data"]["session_id"]

    def tearDown(self):
        self.provider.release.set()
        self._stop()
        self.provider.shutdown()
        self.provider.server_close()
        self.provider_thread.join(2)
        self.tmp.cleanup()

    def turn(self, text, key="transport-turn-0001"):
        return self._post(f"/api/v1/sessions/{self.session}/turns", {
            "schema_version": "1.0", "idempotency_key": key, "text": text})

    def finished(self):
        self.assertEqual(0, self.server.wait_for_jobs(timeout=10))
        return self._get("/api/chat")[1]

    def test_incremental_reply_and_completion_are_published_over_real_sse(self):
        response = urllib.request.urlopen(self.base + "/api/events?token=" + self.token, timeout=5)
        try:
            self.assertEqual("text/event-stream; charset=utf-8", response.headers["Content-Type"])
            self.assertTrue(response.readline().startswith(b"data: "))
            self.turn("[slow]")
            self.assertTrue(self.provider.started.wait(3))
            frames = []
            for _ in range(30):
                line = response.readline()
                if line.startswith(b"data: "):
                    frame = json.loads(line[6:])
                    frames.append(frame)
                    turns = frame.get("chat", {}).get("turns", [])
                    if any(t.get("assistant_draft") == "合成回复：" for t in turns):
                        break
            else:
                self.fail("partial draft not published through SSE")
            self.provider.release.set()
            chat = self.finished()
            self.assertEqual("completed", chat["turns"][0]["status"])
            self.assertEqual(100, chat["usage"]["total_tokens"])
        finally:
            self.provider.release.set()
            response.close()

    def test_truncated_stream_is_failed_and_partial_draft_is_retained(self):
        self.turn("[drop]")
        chat = self.finished()
        self.assertEqual("failed", chat["turns"][0]["status"])
        self.assertEqual("合成回复：", chat["turns"][0]["assistant_draft"])
        self.assertIn("提前断开", chat["turns"][0]["error"]["message"])
        self.assertEqual(1, len(self.provider.requests))

    def test_provider_failure_retry_is_explicit_and_idempotent(self):
        _, first = self.turn("[fail]")
        turn_id = first["data"]["turn"]["turn_id"]
        self.assertEqual("failed", self.finished()["turns"][0]["status"])
        request = {"schema_version": "1.0", "idempotency_key": "transport-retry-0001"}
        code, retry = self._post(f"/api/v1/turns/{turn_id}/retry", request)
        self.assertEqual(202, code)
        self.finished()
        _, duplicate = self._post(f"/api/v1/turns/{turn_id}/retry", request)
        self.assertEqual(retry["data"]["turn"]["turn_id"], duplicate["data"]["turn"]["turn_id"])
        self.assertEqual(2, len(self.provider.requests))

    def test_budget_rejection_never_reaches_the_provider(self):
        self._post(f"/api/v1/sessions/{self.session}/budget", {"schema_version": "1.0", "token_budget": 1})
        code, rejected = self.turn("must not call")
        self.assertEqual(409, code)
        self.assertEqual("budget_exhausted", rejected["error"]["code"])
        self.assertEqual([], self.provider.requests)

    def test_cancel_and_steer_keep_late_provider_data_out_of_cancelled_turn(self):
        _, first = self.turn("[slow]")
        turn_id = first["data"]["turn"]["turn_id"]
        self.assertTrue(self.provider.started.wait(3))
        code, _ = self._post(f"/api/v1/turns/{turn_id}/steer", {
            "schema_version": "1.0", "idempotency_key": "transport-steer-0001", "text": "new synthetic direction"})
        self.assertEqual(202, code)
        self.provider.release.set()
        chat = self.finished()
        self.assertEqual(["interrupted", "completed"], [t["status"] for t in chat["turns"]])
        self.assertEqual(turn_id, chat["turns"][1]["parent_turn_id"])

    def test_native_tool_proposal_approval_and_result_continuation(self):
        self.turn("[tool]")
        chat = self.finished()
        call = chat["tool_calls"][0]
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(call["preview"]["ok"])
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "synthetic file only"})
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {"schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("completed", executed["data"]["status"])
        chat = self.finished()
        if len(self.provider.requests) == 1:
            self._post(f"/api/v1/turns/{call['turn_id']}/continue", {
                "schema_version": "1.0", "idempotency_key": "transport-continue-001"})
            chat = self.finished()
        self.assertEqual(2, len(self.provider.requests))
        messages = self.provider.requests[-1]["messages"]
        tool = next(item for item in messages if item["role"] == "tool")
        self.assertEqual(call["call_id"], tool["tool_call_id"])
        self.assertIn("Synthetic marker 42", tool["content"])
        self.assertIn("真实工具结果", chat["messages"][-1]["text"])

    def test_campaign_records_actual_streamed_usage(self):
        path = self.root / ".popper/campaign.sqlite"
        self.server.evaluation_budget_path = path
        self.turn("synthetic campaign request")
        self.finished()
        ledger = AgentEvaluationBudget(path)
        try:
            self.assertEqual(100, ledger.status()["used_tokens"])
            self.assertEqual(0, ledger.status()["unresolved_requests"])
        finally:
            ledger.close()

    def test_campaign_truncated_stream_blocks_even_explicit_retry(self):
        path = self.root / ".popper/campaign.sqlite"
        self.server.evaluation_budget_path = path
        _, first = self.turn("[drop]")
        turn_id = first["data"]["turn"]["turn_id"]
        self.assertEqual("failed", self.finished()["turns"][0]["status"])
        self._post(f"/api/v1/turns/{turn_id}/retry", {
            "schema_version": "1.0", "idempotency_key": "campaign-explicit-retry-001"})
        self.finished()
        self.assertEqual(1, len(self.provider.requests))
        ledger = AgentEvaluationBudget(path)
        try:
            self.assertEqual(1, ledger.status()["unresolved_requests"])
        finally:
            ledger.close()
