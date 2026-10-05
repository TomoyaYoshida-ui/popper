"""Real HTTP SSE provider + native tools + Workstation HTTP + real Worker.

Scientific suggestions are synthetic fixtures, not real model evidence. Local
approval calls emulate the user only for disposable test projects.
"""
import json
import os
import threading
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from popper.chat import make_chat_client
from popper.harness.conversation import adapt_conversation_harness
from popper.research.agent_evaluation_budget import AgentEvaluationBudget
from popper.sessions import AgentSessionStore
from tests import test_agent_revision_tools as revision_fixture


class ScriptedHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        fixture = self.server.fixture
        fixture.inputs.append(body["messages"])
        reply = fixture.responses.pop(0) if fixture.responses else "核对真实工具结果后停止。"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        frames = [{"choices": [{"delta": {"content": "合成流式提案："}}]}]
        if isinstance(reply, dict):
            for index, call in enumerate(reply["tool_calls"]):
                args = json.dumps(call["arguments"], ensure_ascii=False)
                frames.extend([{"choices": [{"delta": {"tool_calls": [{"index": index,
                    "type": "function", "function": {"name": call["name"], "arguments": args[:10]}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": index,
                        "function": {"arguments": args[10:]}}]}}]}])
        else:
            frames.append({"choices": [{"delta": {"content": reply}}]})
        frames.append({"choices": [], "usage": {"prompt_tokens": 80,
                       "completion_tokens": 20, "total_tokens": 100}})
        for frame in frames:
            self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class RevisionTransportTests(unittest.TestCase):
    def setUp(self):
        revision_fixture.AgentRevisionToolsTests.setUp(self)
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
        self.provider.fixture = self
        self.provider_thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        self.provider_thread.start()
        with patch.dict(os.environ, {"POPPER_API_KEY": "synthetic-placeholder"}):
            client = make_chat_client(f"http://127.0.0.1:{self.provider.server_port}/v1",
                                     "local-synthetic", enable_file_tools=True)
        self.ws.chat_harness = adapt_conversation_harness(client)
        self.ws.chat_model = "local-synthetic"
        self.session = self.ws.create_agent_session("native transport campaign")
        self.ledger_path = self.root / "shared-campaign.sqlite"
        self.ws.evaluation_budget_path = self.ledger_path
        self.ws_thread = threading.Thread(target=self.ws.serve_forever, daemon=True)
        self.ws_thread.start()

    def tearDown(self):
        self.ws.wait_for_jobs(timeout=30)
        self.ws.shutdown()
        self.ws_thread.join(2)
        self.provider.shutdown()
        self.provider.server_close()
        self.provider_thread.join(2)
        revision_fixture.AgentRevisionToolsTests.tearDown(self)

    def post(self, path, payload):
        request = urllib.request.Request(f"http://127.0.0.1:{self.ws.server_port}" + path,
            data=json.dumps({"schema_version": "1.0", **payload}).encode(),
            headers={"Content-Type": "application/json", "X-Popper-Token": self.ws.token})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)["data"]
        except urllib.error.HTTPError as error:
            self.fail(f"HTTP {error.code}: {error.read().decode('utf-8')}")

    def propose(self, tool, args):
        self.ws.wait_for_jobs(timeout=30)
        self.responses.append({"text": "proposal", "tool_calls": [
            {"name": tool.replace(".", "_"), "arguments": args}]})
        result = self.post(f"/api/v1/sessions/{self.session['session_id']}/turns", {
            "text": "合成协议提案", "idempotency_key": f"http-revision-{len(self.inputs):04d}"})
        self.ws.wait_for_jobs(timeout=30)
        store = AgentSessionStore(self.project)
        try:
            return next(call for call in store.tool_calls(self.session["session_id"])
                        if call["turn_id"] == result["turn"]["turn_id"])
        finally:
            store.close()

    def approve_execute(self, call):
        self.post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "decision": "approve", "reason": "synthetic isolated fixture only"})
        return self.post(f"/api/v1/tool-calls/{call['call_id']}/execute", {})

    def test_native_revision_job_observation_continuation_and_shared_ledger(self):
        revision_fixture.AgentRevisionToolsTests.test_actual_revision_job_observation_and_receipt_reconciliation(self)
        self.ws.wait_for_jobs(timeout=30)
        ledger = AgentEvaluationBudget(self.ledger_path)
        try:
            self.assertEqual(len(self.inputs), ledger.status()["request_count"])
            self.assertEqual(100 * len(self.inputs), ledger.status()["used_tokens"])
            self.assertEqual(0, ledger.status()["unresolved_requests"])
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main()
