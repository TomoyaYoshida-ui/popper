import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from popper.core import canonical
from popper.demo import initialize_demo
from popper.server import Workstation
from popper.sessions import AgentSessionStore
from popper import task_context as context


class TaskContextHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        initialize_demo(self.root, "ode-convergence")
        self.seen = []
        self.server = Workstation(("127.0.0.1", 0), self.root, trusted_local=True,
                                  chat_llm=lambda system, messages: self.seen.append((system, messages)) or "synthetic response")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.wid = self.server.workspaces.current_wid()
        store = AgentSessionStore(self.root)
        try:
            self.source = store.create_session("旧讨论", "chat", "mock")["session_id"]
            self.target = store.create_session("新讨论", "chat", "mock")["session_id"]
            turn, _ = store.start_turn(self.source, "研究数值收敛；禁止重复确认资源", "old-task-turn-key", actor="local_user", trace_id="trc_context123")
            store.complete_turn(turn["turn_id"], "先读真实证据")
        finally:
            store.close()

    def tearDown(self):
        self.server.wait_for_jobs(timeout=30)
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, data=None, token=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if token: headers["X-Popper-Token"] = self.server.token
        if origin: headers["Origin"] = origin
        req = urllib.request.Request(self.base + path, data=canonical(data).encode() if data is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def candidate(self):
        code, body = self.request('/api/v1/context/preview', {"schema_version": "1.0", "workspace_id": self.wid, "session_id": self.target, "source_session_id": self.source})
        self.assertEqual(200, code, body)
        return body["data"]

    def adopt(self, candidate=None):
        candidate = candidate or self.candidate()
        request = {"schema_version": "1.0", "workspace_id": self.wid, "op": "adopt", "preview": candidate, "title": "合成 ODE 接续",
                   "fields": {name: "研究数值收敛，禁止重复独立确认" if name == "goal" else "" for name in context.FIELDS},
                   "expected_binding_version": candidate["expected_binding_version"], "idempotency_key": "adopt-task-http-key"}
        return self.request(f'/api/v1/sessions/{self.target}/context', request), request

    def test_http_preview_no_task_or_model_call_and_auth_origin_guards(self):
        candidate = self.candidate()
        self.assertEqual("source_excerpts", candidate["quality"])
        code, result = self.request(f'/api/v1/context/tasks?workspace_id={self.wid}')
        self.assertEqual(200, code); self.assertEqual([], result["data"]["tasks"]); self.assertEqual([], self.seen)
        self.assertEqual(403, self.request(f'/api/v1/context/tasks?workspace_id={self.wid}', token=False)[0])
        self.assertEqual(403, self.request('/api/v1/context/preview', {"schema_version": "1.0", "workspace_id": self.wid, "session_id": self.target}, origin="http://foreign.invalid")[0])

    def test_adopt_idempotence_and_detach_does_not_delete(self):
        (code, body), request = self.adopt()
        self.assertEqual(200, code, body)
        self.assertEqual(body["data"], self.request(f'/api/v1/sessions/{self.target}/context', request)[1]["data"])
        store = AgentSessionStore(self.root)
        try:
            store.update_session_metadata(self.source, store.get_session(self.source)["updated"], title="成功后来源变化")
        finally: store.close()
        self.assertEqual(body["data"], self.request(f'/api/v1/sessions/{self.target}/context', request)[1]["data"])
        self.assertEqual([], self.seen)
        detach = {"schema_version": "1.0", "workspace_id": self.wid, "op": "detach", "expected_binding_version": 1, "idempotency_key": "detach-task-http-key"}
        self.assertEqual(200, self.request(f'/api/v1/sessions/{self.target}/context', detach)[0])
        _, body = self.request(f'/api/v1/sessions/{self.target}/context?workspace_id={self.wid}')
        self.assertIsNone(body["data"]["selected"]); self.assertEqual(1, len(body["data"]["tasks"]))

    def test_new_session_model_sees_confirmed_intent_and_exact_reserved_snapshot(self):
        (code, body), _ = self.adopt(); self.assertEqual(200, code, body)
        with patch.object(self.server, '_schedule_agent_job', return_value=True):
            result = self.server.launch_session_turn(self.target, "核对下一步", "new-task-http-turn", "trc_new123", self.wid)
        store = AgentSessionStore(self.root)
        try:
            frozen = context.load_input(store, result["turn"]["turn_id"])
            self.assertIn("禁止重复独立确认", canonical(frozen["messages"]))
            self.assertEqual(self.server._reserve_context(frozen), result["job"]["reserved_tokens"])
            store.update_session_metadata(self.source, store.get_session(self.source)["updated"], title="已更新旧标题")
        finally:
            store.close()
        self.server._run_session_turn(self.root, self.target, result["turn"]["turn_id"], "trc_new123", "must not be used")
        self.assertEqual((frozen["system"], frozen["messages"]), self.seen[-1])

    def test_late_source_and_binding_conflict_preserves_revisions(self):
        candidate = self.candidate()
        store = AgentSessionStore(self.root)
        try:
            store.update_session_metadata(self.source, store.get_session(self.source)["updated"], title="来源变化")
        finally: store.close()
        (code, body), _ = self.adopt(candidate)
        self.assertEqual(409, code, body)
        _, body = self.request(f'/api/v1/context/tasks?workspace_id={self.wid}')
        self.assertEqual([], body["data"]["tasks"])

    def test_source_disclosure_is_scoped_and_still_pending_approval(self):
        (code, body), _ = self.adopt(); self.assertEqual(200, code, body)
        store = AgentSessionStore(self.root)
        try:
            turn, _ = store.start_turn(self.target, "查看来源", "source-read-http-turn", actor="local_user", trace_id="trc_read123")
            store.complete_turn(turn["turn_id"], "请求读取交接来源")
            version = store.get_session(self.source)["updated"]
        finally: store.close()
        result = self.server.propose_agent_tool(self.target, turn["turn_id"], "context.source", {"source_session_id": self.source, "expected_version": version}, "source-read-tool-key", "trc_read123")
        call = result["tool_call"]
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(self.server._agent_tool_public(call)["preview"]["ok"])
        with self.assertRaises(Exception): self.server.execute_agent_tool(call["call_id"])
        changed = {**call, "arguments": {"source_session_id": self.target, "expected_version": version}}
        self.assertFalse(self.server._agent_tool_public(changed)["preview"]["ok"])

    def test_running_session_context_write_is_rejected(self):
        candidate = self.candidate()
        store = AgentSessionStore(self.root)
        try:
            turn, _ = store.start_turn(self.target, "仍在运行", "running-handoff-key", actor="local_user", trace_id="trc_running123")
        finally: store.close()
        (code, body), _ = self.adopt(candidate)
        self.assertEqual(409, code, body)

    def test_browser_numeric_roundtrip_of_research_preview_keeps_version_guard(self):
        facts = [{"study_id": "S-synthetic", "state_version": 1, "budget": {"available": 3.0}, "observations": 5, "claims": []}]
        with patch.object(self.server, '_context_facts', return_value=facts):
            code, response = self.request('/api/v1/context/preview', {"schema_version": "1.0", "workspace_id": self.wid, "session_id": self.target, "study_ids": ["S-synthetic"]})
            self.assertEqual(200, code)
            candidate = response["data"]
            candidate["facts"][0]["budget"]["available"] = 3
            (code, response), request = self.adopt(candidate)
            self.assertEqual(200, code, response)
            code, response = self.request('/api/v1/context/preview', {"schema_version": "1.0", "workspace_id": self.wid, "session_id": self.target, "task_id": response["data"]["binding"]["task_id"]})
            candidate = response["data"]
            candidate["facts"][0]["budget"]["available"] = 4
            request.update(preview=candidate, expected_binding_version=1, idempotency_key="changed-budget-handoff")
            self.assertEqual(409, self.request(f'/api/v1/sessions/{self.target}/context', request)[0])

    def test_confirmed_handoff_tool_continuation_reserves_and_executes_exact_input(self):
        (code, body), _ = self.adopt(); self.assertEqual(200, code, body)
        store = AgentSessionStore(self.root)
        try:
            turn, _ = store.start_turn(self.target, "查看交接来源", "continued-source-turn", actor="local_user", trace_id="trc_continue123")
            store.complete_turn(turn["turn_id"], "请求读取来源")
            source_version = store.get_session(self.source)["updated"]
        finally: store.close()
        call = self.server.propose_agent_tool(self.target, turn["turn_id"], "context.source", {"source_session_id": self.source, "expected_version": source_version}, "continued-source-tool", "trc_continue123")["tool_call"]
        self.server.decide_agent_tool(call["call_id"], "approve", "允许披露合成来源")
        with patch.object(self.server, '_schedule_agent_job', return_value=True):
            completed = self.server.execute_agent_tool(call["call_id"])
            continued = self.server.continue_session_after_tools(turn["turn_id"])
        self.assertEqual("completed", completed["status"])
        store = AgentSessionStore(self.root)
        try:
            frozen = context.load_input(store, continued["turn"]["turn_id"])
            self.assertIn("禁止重复独立确认", canonical(frozen["messages"]))
            self.assertEqual(self.server._reserve_context(frozen), continued["job"]["reserved_tokens"])
            calls = {item["id"] for message in frozen["messages"] for item in message.get("tool_calls", [])}
            results = {message["tool_call_id"] for message in frozen["messages"] if message["role"] == "tool"}
            self.assertEqual({call["call_id"]}, calls)
            self.assertEqual(calls, results)
        finally: store.close()
        self.server._run_session_turn(self.root, self.target, continued["turn"]["turn_id"], "trc_continue123", "not the frozen prompt")
        self.assertEqual((frozen["system"], frozen["messages"]), self.seen[-1])
