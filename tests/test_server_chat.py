"""工作台自由对话 HTTP 测试。

验证六件事：
- 未配置 BYOK 端点时 /api/chat 如实 configured=false，POST 被诚实拒绝（不发假回复）；
- 注入 fake 模型后发送：用户消息与助手回复真实落盘 <当前根>/.popper/sessions.sqlite，
  且 system prompt 只注入真实实验状态；
- 模型调用失败时保留用户消息并写入带 error 标记的助手条目（如实记录失败）；
- 空消息 / 超限请求体被拒绝；
- 历史（重启 Workstation）不丢，切换工作区根后对话按根隔离；
- make_chat_client 的安全约束（非 HTTPS/非本机地址、缺 POPPER_API_KEY 即 fail fast）。
"""
import json
import runpy
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from popper.chat import make_chat_client
from popper.core import Experiment, ProtocolError, initialize
from popper.demo import initialize_demo
from popper.identity import EntityKind, new_id
from popper.research import ResearchController
from popper.research.store import ResearchStore
from popper.research.readout import controller_routing_readiness, read_research_state
from popper.research.contracts import ExperimentDesign, Hypothesis
from popper.sessions import AgentSessionStore
from popper.server import Workstation
from popper.workspace import Workspace
from popper.workspace_files import ProjectFiles

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


def _make_project(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, root / name)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](root)
    initialize(root)
    return root


class ChatHttpTests(unittest.TestCase):
    def test_agent_source_patch_requires_approval_and_exposes_review_diff(self):
        self._start(chat_llm=lambda system, messages: "准备审查源码补丁",
                    chat_kwargs={"trusted_local": True})
        source = self.root / "analysis.py"
        source.write_text("value = 1\n", encoding="utf-8")
        before = ProjectFiles(self.root).read("analysis.py")
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "源码补丁", "harness": "chat"})
        session_id = created["data"]["session_id"]
        _, turn = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "source-patch-turn-001",
            "text": "将 value 改为 2"})
        turn_id = turn["data"]["turn"]["turn_id"]
        arguments = {"path": "analysis.py", "expected_sha256": before["sha256"],
                     "old_text": "value = 1", "new_text": "value = 2"}
        endpoint = f"/api/v1/sessions/{session_id}/tool-calls"
        code, proposed = self._post(endpoint, {
            "schema_version": "1.0", "turn_id": turn_id,
            "tool_name": "workspace.file_replace", "arguments": arguments,
            "idempotency_key": "source-patch-proposal-001"})
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        self.assertEqual("pending_approval", proposed["data"]["tool_call"]["status"])
        _, chat = self._get("/api/chat")
        public = next(item for item in chat["tool_calls"] if item["call_id"] == call_id)
        self.assertIn("-value = 1", public["preview"]["file_edit"]["diff"])
        self.assertIn("+value = 2", public["preview"]["file_edit"]["diff"])
        self.assertEqual("value = 1\n", source.read_text(encoding="utf-8"))
        code, _ = self._post(f"/api/v1/tool-calls/{call_id}/execute",
                             {"schema_version": "1.0"})
        self.assertEqual(409, code)
        code, _ = self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "已审查 diff"})
        self.assertEqual(200, code)
        code, executed = self._post(f"/api/v1/tool-calls/{call_id}/execute",
                                    {"schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("completed", executed["data"]["status"])
        self.assertEqual("value = 2\n", source.read_text(encoding="utf-8"))
        self.assertEqual(executed["data"]["result"],
                         ProjectFiles(self.root).edit_receipt(call_id, arguments))

    def test_agent_source_patch_receipt_reconciles_after_write_before_tool_finish(self):
        self._start(chat_llm=lambda system, messages: "等待源码审批",
                    chat_kwargs={"trusted_local": True})
        source = self.root / "analysis.py"
        source.write_text("value = 1\n", encoding="utf-8")
        arguments = {"path": "analysis.py",
                     "expected_sha256": ProjectFiles(self.root).read("analysis.py")["sha256"],
                     "old_text": "value = 1", "new_text": "value = 2"}
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "崩溃恢复", "harness": "chat"})
        session_id = created["data"]["session_id"]
        _, turn = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "file-recover-turn-001",
            "text": "审查源码"})
        _, proposed = self._post(f"/api/v1/sessions/{session_id}/tool-calls", {
            "schema_version": "1.0", "turn_id": turn["data"]["turn"]["turn_id"],
            "tool_name": "workspace.file_replace", "arguments": arguments,
            "idempotency_key": "file-recover-tool-001"})
        call_id = proposed["data"]["tool_call"]["call_id"]
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "已审查 diff"})
        store = AgentSessionStore(self.root)
        try:
            store.claim_tool_call(call_id)
            applied = ProjectFiles(self.root).apply_replace(call_id, arguments)
            receipt_path = self.root / ".popper" / "file-edit-receipts" / (call_id + ".json")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_path.write_text(json.dumps({**receipt, "status": "prepared"}),
                                    encoding="utf-8")
            self.server._reconcile_agent_tool_receipts(self.root, store)
            recovered = store.get_tool_call(call_id)
            self.assertEqual("completed", recovered["status"])
            self.assertEqual(applied, recovered["result"])
            self.assertEqual("value = 2\n", source.read_text(encoding="utf-8"))
        finally:
            store.close()

    def test_agent_source_patch_recovers_after_real_workstation_restart(self):
        self._start(chat_llm=lambda system, messages: "等待用户审查",
                    chat_kwargs={"trusted_local": True})
        source = self.root / "analysis.py"
        source.write_text("value = 1\n", encoding="utf-8")
        arguments = {"path": "analysis.py",
                     "expected_sha256": ProjectFiles(self.root).read("analysis.py")["sha256"],
                     "old_text": "value = 1", "new_text": "value = 2"}
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "重启恢复", "harness": "chat"})
        session_id = created["data"]["session_id"]
        _, turn = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "file-restart-turn-001",
            "text": "审查源码"})
        _, proposed = self._post(f"/api/v1/sessions/{session_id}/tool-calls", {
            "schema_version": "1.0", "turn_id": turn["data"]["turn"]["turn_id"],
            "tool_name": "workspace.file_replace", "arguments": arguments,
            "idempotency_key": "file-restart-tool-001"})
        call_id = proposed["data"]["tool_call"]["call_id"]
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "已审查 diff"})
        store = AgentSessionStore(self.root)
        try:
            store.claim_tool_call(call_id)
        finally:
            store.close()
        applied = ProjectFiles(self.root).apply_replace(call_id, arguments)
        self._stop()
        del self.server
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  chat_llm=lambda system, messages: "恢复后继续",
                                  trusted_local=True)
        store = AgentSessionStore(self.root)
        try:
            recovered = store.get_tool_call(call_id)
            self.assertEqual("completed", recovered["status"])
            self.assertEqual(applied, recovered["result"])
            self.assertEqual("value = 2\n", source.read_text(encoding="utf-8"))
        finally:
            store.close()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def test_agent_file_disclosure_requires_approval_and_is_bounded(self):
        self._start(chat_llm=lambda system, messages: "等待文件读取审批",
                    chat_kwargs={"trusted_local": True})
        (self.root / "notes.md").write_text("private research note", encoding="utf-8")
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "文件披露", "harness": "chat"})
        session_id = created["data"]["session_id"]
        _, turn = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "file-disclose-turn-001",
            "text": "读取笔记"})
        turn_id = turn["data"]["turn"]["turn_id"]
        endpoint = f"/api/v1/sessions/{session_id}/tool-calls"
        proposed_calls = []
        for tool_name, arguments, key in (
                ("workspace.file_list", {}, "file-disclose-list-001"),
                ("workspace.file_read", {"path": "notes.md"}, "file-disclose-read-001")):
            code, proposed = self._post(endpoint, {
                "schema_version": "1.0", "turn_id": turn_id,
                "tool_name": tool_name, "arguments": arguments,
                "idempotency_key": key})
            self.assertEqual(201, code)
            call_id = proposed["data"]["tool_call"]["call_id"]
            self.assertEqual("disclose", proposed["data"]["tool_call"]["risk"])
            proposed_calls.append((tool_name, call_id))
        for tool_name, call_id in proposed_calls:
            code, _ = self._post(f"/api/v1/tool-calls/{call_id}/execute",
                                 {"schema_version": "1.0"})
            self.assertEqual(409, code)
            self._post(f"/api/v1/tool-calls/{call_id}/decision", {
                "schema_version": "1.0", "decision": "approve", "reason": "允许披露"})
            code, executed = self._post(f"/api/v1/tool-calls/{call_id}/execute",
                                        {"schema_version": "1.0"})
            self.assertEqual(200, code)
            self.assertEqual("completed", executed["data"]["status"])
            if tool_name == "workspace.file_list":
                self.assertIn("notes.md", [row["path"] for row in
                                           executed["data"]["result"]["files"]])
            else:
                self.assertEqual("private research note",
                                 executed["data"]["result"]["content"])

    @staticmethod
    def _create_draft_design(run_dir, design_id="D-agent-freeze"):
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            original = store.list("design")[0]
            store.create_design(ExperimentDesign(
                design_id=design_id,
                hypothesis_id=original["hypothesis_id"],
                interventions=tuple(original["interventions"]),
                control=original["control"], baseline=original["baseline"],
                metric=original["metric"], scorer_id=original["scorer_id"],
                metric_direction=original["metric_direction"],
                analysis_unit=original["analysis_unit"],
                splits=tuple(original["splits"]), seeds=tuple(original["seeds"]),
                min_meaningful_effect=original["min_meaningful_effect"],
            ))
            return design_id
        finally:
            store.close()

    @staticmethod
    def _design_proposal(research):
        design = research["designs"][0]
        return {
            "study_id": research["manifest"]["study_id"],
            "expected_version": research["state_version"],
            "hypothesis_id": design["hypothesis_id"],
            "expected_hypothesis_version": design["hypothesis_version"],
            "interventions": list(design["interventions"]),
            "control": design["control"], "baseline": design["baseline"],
            "metric": design["metric"], "scorer_id": design["scorer_id"],
            "metric_direction": design["metric_direction"],
            "analysis_unit": design["analysis_unit"],
            "splits": list(design["splits"]),
            "repeat_values": list(design["seeds"]),
            "min_meaningful_effect": design["min_meaningful_effect"],
        }

    @staticmethod
    def _stored_messages(root):
        store = AgentSessionStore(root)
        try:
            session = store.default_session(create=False)
            return store.chat_messages(session["session_id"]) if session else []
        finally:
            store.close()

    def _start(self, chat_llm=None, chat_kwargs=None, research_names=()):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name) / "project")
        self.research_dirs = [Path(self.tmp.name) / name for name in research_names]
        kwargs = {"chat_llm": chat_llm, "research_runs": self.research_dirs,
                  **(chat_kwargs or {})}
        self.server = Workstation(("127.0.0.1", 0), self.root, **kwargs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as r:
            self.token = json.loads(r.read())["token"]

    def _stop(self):
        leftover = self.server.wait_for_jobs(timeout=60)
        self.assertEqual(0, leftover, "后台作业线程没收尾，临时目录会被继续写入")
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def tearDown(self):
        if hasattr(self, "server"):
            self._stop()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def _get(self, path):
        req = urllib.request.Request(self.base + path,
                                     headers={"X-Popper-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"X-Popper-Token": self.token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # -- 未配置：诚实空态，拒绝发送 -------------------------------------------

    def test_unconfigured_reports_honestly_and_rejects_send(self):
        self._start()
        code, body = self._get("/api/chat")
        self.assertEqual(200, code)
        self.assertFalse(body["configured"])
        self.assertIsNone(body["model"])
        self.assertEqual([], body["messages"])
        self.assertTrue(body["event_integrity"]["ok"])
        self.assertIn("unconfigured_notice", body["labels"])
        code, body = self._post("/api/chat", {"text": "你好"})
        self.assertEqual(409, code)
        # 错误文案即后端单一下发的未配置提示。
        self.assertIn("未配置", body["error"])
        # 磁盘上没有伪造任何对话记录。
        self.assertFalse((self.root / ".popper" / "chat.json").is_file())

    # -- 已配置（注入 fake 模型）：真实落盘 + 真实上下文 -----------------------

    def test_send_roundtrip_stores_user_and_assistant(self):
        seen = {}

        def fake_llm(system, messages):
            seen["system"] = system
            seen["messages"] = messages
            return "这是模型的真实回复。"

        self._start(chat_llm=fake_llm)
        # 先生成一条真实控制器结果，验证具体指标与 run_id 会进入模型上下文。
        exp = Experiment(self.root)
        try:
            exp.evaluate(exp.state()["spec"]["baseline"], "dev", trusted_local=True)
        finally:
            exp.close()
        code, body = self._get("/api/chat")
        self.assertEqual(200, code)
        self.assertTrue(body["configured"])
        code, body = self._post("/api/chat", {"text": "当前指标是什么？"})
        self.assertEqual(200, code)
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(["user", "assistant"], roles)
        self.assertEqual("当前指标是什么？", body["messages"][0]["text"])
        self.assertEqual("这是模型的真实回复。", body["messages"][1]["text"])
        self.assertNotIn("error", body["messages"][1])
        # system prompt 来自真实实验状态：目标与指标名如实注入。
        spec = json.loads((self.root / "experiment.json").read_text(encoding="utf-8-sig"))
        self.assertIn(spec["objective"], seen["system"])
        self.assertIn(spec["metric"]["name"], seen["system"])
        # 完整控制器结果和证据定位符也进入上下文，模型可回答具体数值而非只见 delta。
        exp = Experiment(self.root)
        try:
            expected = exp.results()
        finally:
            exp.close()
        self.assertTrue(expected)
        for result in expected:
            self.assertIn(result["run_id"], seen["system"])
            self.assertIn(str(result["mean"]), seen["system"])
        self.assertIn("run_id 是证据定位符", seen["system"])
        # 历史真实落盘在当前工作区根。
        self.assertEqual(2, len(self._stored_messages(self.root)))

    def test_chat_refuses_tampered_dev_result_before_model_call(self):
        calls = []

        def fake_llm(system, messages):
            calls.append(messages)
            return "不应调用模型"

        self._start(chat_llm=fake_llm)
        exp = Experiment(self.root)
        try:
            spec = exp.state()["spec"]
            result = exp.evaluate(spec["baseline"], "dev", trusted_local=True)
            forged = {**result, "mean": result["mean"] + 123.0}
            with exp.db:
                exp.db.execute("UPDATE runs SET result = ? WHERE id = ?",
                               (json.dumps(forged), result["run_id"]))
        finally:
            exp.close()
        code, body = self._post("/api/chat", {"text": "现在的开发指标是多少？"})
        self.assertEqual(409, code)
        self.assertIn("投影结果与事件不一致", body["error"])
        self.assertEqual([], calls)
        _, chat = self._get("/api/chat")
        self.assertEqual([], chat["turns"], "证据失效时不能留下悬挂中的 Turn")

    def test_readonly_experiment_replays_without_write_authority(self):
        self._start(chat_llm=lambda system, messages: "已核验")
        db_path = self.root / ".popper" / "state.db"
        before = db_path.read_bytes()
        exp = Experiment(self.root, readonly=True)
        try:
            self.assertEqual("verified", exp.replay()["status"])
            with self.assertRaises(sqlite3.OperationalError):
                exp.db.execute("UPDATE state SET data = '{}' WHERE id = 1")
        finally:
            exp.close()
        self.assertEqual(before, db_path.read_bytes())

    def test_chat_rejects_tampered_turn_history_before_new_model_call(self):
        calls = []

        def fake_llm(system, messages):
            calls.append(messages)
            return "真实回答"

        self._start(chat_llm=fake_llm)
        code, _ = self._post("/api/chat", {"text": "第一问"})
        self.assertEqual(200, code)
        with sqlite3.connect(self.root / ".popper" / "sessions.sqlite") as db:
            db.execute("UPDATE turns SET assistant_message='伪造回答' "
                       "WHERE status='completed'")
        code, body = self._post("/api/chat", {"text": "第二问"})
        self.assertEqual(409, code)
        self.assertIn("内容快照", body["error"])
        self.assertEqual(1, len(calls), "篡改后的历史不能进入第二次模型调用")
        _, chat = self._get("/api/chat")
        self.assertEqual(1, len(chat["turns"]), "拒绝请求不能留下新的运行中 Turn")

    def test_tool_event_audit_endpoint_pages_transitions_without_result_body(self):
        self._start()
        store = AgentSessionStore(self.root)
        try:
            session = store.create_session("工具审计")
            turn, _ = store.start_turn(
                session["session_id"], "读取", "audit-tool-turn-0001",
                actor="test-user", trace_id="trc_toolaudithttp")
            store.complete_turn(turn["turn_id"], "建议读取")
            call, _ = store.propose_tool_call(
                session["session_id"], turn["turn_id"], "research.status",
                {"study_id": "std_test"}, "read", "audit-tool-call-0001",
                actor="agent", trace_id="trc_toolaudithttp")
            store.claim_tool_call(call["call_id"])
            store.finish_tool_call(call["call_id"], result={"private": "not-in-audit"})
        finally:
            store.close()
        path = f"/api/v1/sessions/{session['session_id']}/tool-events"
        code, first = self._get(path + "?after=0&limit=1")
        self.assertEqual(200, code)
        self.assertEqual("tool.approved", first["data"]["events"][0]["kind"])
        self.assertTrue(first["data"]["has_more"])
        self.assertTrue(first["data"]["integrity"]["ok"])
        code, rest = self._get(path + f"?after={first['data']['cursor']}&limit=10")
        self.assertEqual(200, code)
        self.assertEqual(["tool.running", "tool.completed"],
                         [event["kind"] for event in rest["data"]["events"]])
        self.assertNotIn("not-in-audit", json.dumps(rest))
        code, invalid = self._get(path + "?limit=0")
        self.assertEqual(400, code)
        self.assertIn("limit", invalid["error"]["message"])

    def test_v1_sessions_support_multiple_conversations_and_idempotent_turns(self):
        calls = []

        def fake_llm(system, messages):
            calls.append(messages)
            return "持久化回复"

        self._start(chat_llm=fake_llm)
        code, empty = self._get("/api/v1/sessions")
        self.assertEqual(200, code)
        self.assertEqual([], empty["data"]["sessions"])
        _, initial_chat = self._get("/api/chat")
        self.assertEqual("chat", initial_chat["harnesses"][0]["name"])
        self.assertEqual("1.0", initial_chat["harnesses"][0]["protocol_version"])
        code, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "证据分析", "harness": "chat"})
        self.assertEqual(201, code)
        session_id = created["data"]["session_id"]
        self.assertTrue(session_id.startswith("ses_"))
        self.assertEqual("chat", created["data"]["harness"])
        self.assertEqual("1.0", created["data"]["harness_version"])

        request = {"schema_version": "1.0", "idempotency_key": "turn-http-0001",
                   "text": "分析当前证据"}
        endpoint = f"/api/v1/sessions/{session_id}/turns"
        code, first = self._post(endpoint, request)
        self.assertEqual(202, code)
        self.assertFalse(first["data"]["replayed"])
        self.assertIn(first["data"]["turn"]["status"], {"running", "completed"})
        self.assertTrue(first["data"]["turn"]["turn_id"].startswith("trn_"))
        self.assertEqual("local_user", first["data"]["turn"]["actor"])
        self.assertEqual(first["meta"]["trace_id"], first["data"]["turn"]["trace_id"])
        self.server.wait_for_jobs(timeout=3)
        code, replay = self._post(endpoint, request)
        self.assertEqual(202, code)
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual("completed", replay["data"]["turn"]["status"])
        self.assertEqual(first["data"]["turn"]["turn_id"],
                         replay["data"]["turn"]["turn_id"])
        self.assertEqual(1, len(calls), "网络重试不得再次调用模型")

        changed = {**request, "text": "篡改后的请求"}
        code, conflict = self._post(endpoint, changed)
        self.assertEqual(409, code)
        self.assertEqual("idempotency_conflict", conflict["error"]["code"])
        missing = "/api/v1/sessions/ses_aaaaaaaaaaaaaaaaaaaaaaaaaa/turns"
        code, absent = self._post(missing, {**request, "idempotency_key": "missing-0001"})
        self.assertEqual(404, code)
        self.assertEqual("not_found", absent["error"]["code"])

        code, sessions = self._get("/api/v1/sessions")
        self.assertEqual(200, code)
        self.assertEqual([session_id], [s["session_id"]
                                        for s in sessions["data"]["sessions"]])

        code, second = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "第二会话", "harness": "chat"})
        self.assertEqual(201, code)
        second_id = second["data"]["session_id"]
        code, selected = self._post(
            f"/api/v1/sessions/{second_id}/select", {"schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual(second_id, selected["data"]["session_id"])
        self.assertEqual([], selected["data"]["messages"])
        _, compatible = self._get("/api/chat")
        self.assertEqual(second_id, compatible["session_id"])
        self.assertEqual(2, len(compatible["sessions"]))

    def test_legacy_chat_is_imported_once_into_default_session(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name) / "project")
        legacy = {"schema_version": "1.0", "messages": [
            {"id": "m_old_user", "role": "user", "text": "旧问题",
             "ts": "2026-01-01T00:00:00+00:00"},
            {"id": "m_old_assistant", "role": "assistant", "text": "旧回答",
             "ts": "2026-01-01T00:00:01+00:00"},
        ]}
        (self.root / ".popper").mkdir(exist_ok=True)
        (self.root / ".popper" / "chat.json").write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  chat_llm=lambda system, messages: "新回答")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as response:
            self.token = json.loads(response.read())["token"]
        _, body = self._get("/api/chat")
        self.assertEqual(["旧问题", "旧回答"], [m["text"] for m in body["messages"]])
        # Reopening the store/import path cannot duplicate the migrated turn.
        _, again = self._get("/api/chat")
        self.assertEqual(2, len(again["messages"]))

    def test_v1_tool_call_requires_approval_and_executes_versioned_workspace_command(self):
        self._start(chat_llm=lambda system, messages: "建议创建一个 Quest")
        _, workspace = self._get("/api/workspace")
        fid = workspace["folders"][0]["fid"]
        version = workspace["meta"]["state_version"]
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "工具审批", "harness": "chat"})
        session_id = created["data"]["session_id"]
        _, turn = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "tool-turn-http-0001",
            "text": "创建验证任务"})
        turn_id = turn["data"]["turn"]["turn_id"]
        proposal = {
            "schema_version": "1.0", "turn_id": turn_id,
            "tool_name": "workspace.task_create",
            "arguments": {"fid": fid, "title": "验证工具审批",
                          "expected_version": version},
            "idempotency_key": "tool-proposal-http-0001",
        }
        endpoint = f"/api/v1/sessions/{session_id}/tool-calls"
        code, proposed = self._post(endpoint, proposal)
        self.assertEqual(201, code)
        call = proposed["data"]["tool_call"]
        self.assertEqual("pending_approval", call["status"])
        self.assertEqual("write", call["risk"])
        call_id = call["call_id"]

        code, blocked = self._post(
            f"/api/v1/tool-calls/{call_id}/execute", {"schema_version": "1.0"})
        self.assertEqual(409, code)
        self.assertEqual("invalid_transition", blocked["error"]["code"])
        _, unchanged = self._get("/api/workspace")
        self.assertFalse(any(task["title"] == "验证工具审批"
                             for task in unchanged["folders"][0]["tasks"]))

        code, approved = self._post(
            f"/api/v1/tool-calls/{call_id}/decision",
            {"schema_version": "1.0", "decision": "approve", "reason": "用户确认"})
        self.assertEqual(200, code)
        self.assertEqual("approved", approved["data"]["status"])
        code, executed = self._post(
            f"/api/v1/tool-calls/{call_id}/execute", {"schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("completed", executed["data"]["status"], executed["data"])
        self.assertEqual(version + 1, executed["data"]["result"]["state_version"])
        _, changed = self._get("/api/workspace")
        self.assertEqual(1, sum(task["title"] == "验证工具审批"
                                for task in changed["folders"][0]["tasks"]))

        _, chat = self._get("/api/chat")
        projected = next(item for item in chat["tool_calls"]
                         if item["call_id"] == call_id)
        self.assertEqual("completed", projected["status"])

    def test_native_model_tool_call_is_atomically_projected_for_approval(self):
        requested = {}

        def fake_llm(system, messages):
            self.assertIn(requested["fid"], system)
            self.assertIn(str(requested["version"]), system)
            return {"text": "建议创建验证任务。", "tool_calls": [{
                "name": "workspace_task_create",
                "arguments": {"fid": requested["fid"], "title": "模型建议任务",
                              "expected_version": requested["version"]},
            }]}

        self._start(chat_llm=fake_llm)
        _, workspace = self._get("/api/workspace")
        requested.update(fid=workspace["folders"][0]["fid"],
                         version=workspace["meta"]["state_version"])
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "原生工具", "harness": "chat"})
        session_id = created["data"]["session_id"]
        code, response = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "native-tool-turn-0001",
            "text": "请创建一个验证任务"})
        self.assertEqual(202, code)
        self.assertIn(response["data"]["turn"]["status"], {"running", "completed"})
        self.server.wait_for_jobs(timeout=3)
        _, chat = self._get("/api/chat")
        self.assertEqual(1, len(chat["tool_calls"]))
        self.assertEqual("pending_approval", chat["tool_calls"][0]["status"])
        self.assertEqual("workspace.task_create", chat["tool_calls"][0]["tool_name"])

    def test_native_model_file_patch_stays_pending_until_user_approves(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "提议修改源码。", "tool_calls": [{
                    "name": "workspace_file_replace", "arguments": context["arguments"]}]}
            return "源码修改工具已返回。"

        self._start(chat_llm=fake_llm, chat_kwargs={"trusted_local": True})
        source = self.root / "analysis.py"
        source.write_text("value = 1\n", encoding="utf-8")
        context["arguments"] = {
            "path": "analysis.py",
            "expected_sha256": ProjectFiles(self.root).read("analysis.py")["sha256"],
            "old_text": "value = 1", "new_text": "value = 2"}
        _, chat = self._post("/api/chat", {"text": "把源码值改为 2"})
        self.assertEqual(1, len(chat["tool_calls"]))
        call = chat["tool_calls"][0]
        self.assertEqual("workspace.file_replace", call["tool_name"])
        self.assertEqual("pending_approval", call["status"])
        self.assertIn("+value = 2", call["preview"]["file_edit"]["diff"])
        self.assertEqual("value = 1\n", source.read_text(encoding="utf-8"))
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "已审查 diff"})
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute",
                                    {"schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("completed", executed["data"]["status"])
        self.assertEqual("value = 2\n", source.read_text(encoding="utf-8"))

    def test_next_model_turn_receives_approved_tool_result(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "建议创建任务。", "tool_calls": [{
                    "name": "workspace_task_create",
                    "arguments": {"fid": context["fid"], "title": "工具反馈任务",
                                  "expected_version": context["version"]},
                }]}
            context["messages"] = messages
            return "已看到工具结果。"

        self._start(chat_llm=fake_llm)
        _, workspace = self._get("/api/workspace")
        context.update(fid=workspace["folders"][0]["fid"],
                       version=workspace["meta"]["state_version"])
        code, chat = self._post("/api/chat", {"text": "创建验证任务"})
        self.assertEqual(200, code)
        call_id = chat["tool_calls"][0]["call_id"]
        self.assertEqual("pending_approval", chat["tool_calls"][0]["status"])
        code, _ = self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "用户确认"})
        self.assertEqual(200, code)
        code, executed = self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("completed", executed["data"]["status"])
        self.assertEqual(0, self.server.wait_for_jobs(timeout=5))
        _, continued = self._get("/api/chat")
        continuation = continued["turns"][-1]
        self.assertEqual("agent_continuation", continuation["actor"])
        self.assertEqual("", continuation["user_message"])
        self.assertEqual("completed", continuation["status"])
        self.assertEqual("已看到工具结果。", continuation["assistant_message"])
        code, replay = self._post(
            f"/api/v1/turns/{chat['turns'][0]['turn_id']}/continue",
            {"schema_version": "1.0"})
        self.assertEqual(202, code)
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual(continuation["turn_id"], replay["data"]["turn"]["turn_id"])
        code, _ = self._post("/api/chat", {"text": "刚才工具执行结果是什么？"})
        self.assertEqual(200, code)
        messages = context["messages"]
        self.assertEqual(["user", "assistant", "tool"],
                         [item["role"] for item in messages[:3]])
        self.assertEqual("user", messages[-1]["role"])
        self.assertEqual(call_id, messages[1]["tool_calls"][0]["id"])
        self.assertEqual(call_id, messages[2]["tool_call_id"])
        outcome = json.loads(messages[2]["content"])
        self.assertEqual("completed", outcome["status"])
        self.assertEqual(context["version"] + 1, outcome["result"]["state_version"])

    def test_multiple_tool_decisions_wait_for_all_then_continue_without_user(self):
        context = {"calls": 0, "messages": None}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "请求两项研究读取。", "tool_calls": [
                    {"name": "research_status",
                     "arguments": {"study_id": context["study_id"]}},
                    {"name": "research_status",
                     "arguments": {"study_id": context["study_id"]}},
                ]}
            context["messages"] = messages
            return "两项请求均被拒绝，未读取研究状态。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        context["study_id"] = json.loads(
            (run_dir / "research.json").read_text(encoding="utf-8"))["study_id"]
        _, chat = self._post("/api/chat", {"text": "查询两项状态"})
        calls = chat["tool_calls"]
        self.assertEqual(2, len(calls))
        source_id = chat["turns"][0]["turn_id"]
        code, blocked = self._post(f"/api/v1/turns/{source_id}/continue", {
            "schema_version": "1.0"})
        self.assertEqual(400, code)
        self.assertIn("尚未全部", blocked["error"]["message"])
        code, _ = self._post(f"/api/v1/tool-calls/{calls[0]['call_id']}/decision", {
            "schema_version": "1.0", "decision": "reject", "reason": "拒绝第一项"})
        self.assertEqual(200, code)
        _, partial = self._get("/api/chat")
        self.assertEqual(1, len(partial["turns"]))
        self.assertEqual(1, context["calls"])
        code, _ = self._post(f"/api/v1/tool-calls/{calls[1]['call_id']}/decision", {
            "schema_version": "1.0", "decision": "reject", "reason": "拒绝第二项"})
        self.assertEqual(200, code)
        self.assertEqual(0, self.server.wait_for_jobs(timeout=5))
        _, completed = self._get("/api/chat")
        self.assertEqual(2, len(completed["turns"]))
        self.assertEqual("agent_continuation", completed["turns"][1]["actor"])
        self.assertEqual(["user", "assistant", "tool", "tool"],
                         [message["role"] for message in context["messages"]])
        self.assertTrue(all(json.loads(message["content"])["status"] == "rejected"
                            for message in context["messages"][2:]))

    def test_failed_continuation_retry_preserves_native_tool_history(self):
        context = {"calls": 0, "messages": None}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "提议读取。", "tool_calls": [{
                    "name": "research_status",
                    "arguments": {"study_id": context["study_id"]},
                }]}
            if context["calls"] == 2:
                raise RuntimeError("模拟模型故障")
            context["messages"] = messages
            return "已根据获批结果恢复回答。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        context["study_id"] = json.loads(
            (run_dir / "research.json").read_text(encoding="utf-8"))["study_id"]
        _, chat = self._post("/api/chat", {"text": "读取研究"})
        call_id = chat["tool_calls"][0]["call_id"]
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "允许读取"})
        self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(0, self.server.wait_for_jobs(timeout=5))
        _, failed = self._get("/api/chat")
        continuation = failed["turns"][-1]
        self.assertEqual("agent_continuation", continuation["actor"])
        self.assertEqual("failed", continuation["status"])
        code, retried = self._post(
            f"/api/v1/turns/{continuation['turn_id']}/retry", {
                "schema_version": "1.0", "idempotency_key": "retry-continuation-1"})
        self.assertEqual(202, code)
        self.assertEqual("agent_continuation", retried["data"]["turn"]["actor"])
        self.assertEqual(0, self.server.wait_for_jobs(timeout=5))
        self.assertEqual(["user", "assistant", "tool"],
                         [message["role"] for message in context["messages"]])

    def test_restart_reconciles_committed_workspace_tool_receipt(self):
        self._start(chat_llm=lambda system, messages: "准备执行工具")
        _, workspace = self._get("/api/workspace")
        fid = workspace["folders"][0]["fid"]
        version = workspace["meta"]["state_version"]
        _, chat = self._post("/api/chat", {"text": "创建一个任务"})
        session_id = chat["session_id"]
        turn_id = chat["turns"][0]["turn_id"]
        code, proposed = self._post(f"/api/v1/sessions/{session_id}/tool-calls", {
            "schema_version": "1.0", "turn_id": turn_id,
            "tool_name": "workspace.task_create",
            "arguments": {"fid": fid, "title": "崩溃窗口任务",
                          "expected_version": version},
            "idempotency_key": "tool-crash-window-0001",
        })
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        code, _ = self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "确认"})
        self.assertEqual(200, code)
        store = AgentSessionStore(self.root)
        try:
            store.claim_tool_call(call_id)
        finally:
            store.close()
        # Simulate a crash after Workspace committed but before ToolCall finish.
        committed = Workspace(self.root).apply_command(
            {"op": "task_create", "fid": fid, "title": "崩溃窗口任务"},
            idempotency_key="tool-" + call_id, expected_version=version)
        self._stop()
        self.server = Workstation(("127.0.0.1", 0), self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as response:
            self.token = json.loads(response.read())["token"]
        _, recovered = self._get("/api/chat")
        call = next(item for item in recovered["tool_calls"] if item["call_id"] == call_id)
        self.assertEqual("completed", call["status"])
        self.assertEqual(committed, call["result"])
        _, workspace = self._get("/api/workspace")
        self.assertEqual(1, sum(task["title"] == "崩溃窗口任务"
                                for folder in workspace["folders"]
                                for task in folder["tasks"]))

    def test_research_status_tool_requires_disclosure_approval_and_is_read_only(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                self.assertIn(context["study_id"], system)
                return {"text": "请求读取研究状态。", "tool_calls": [{
                    "name": "research_status",
                    "arguments": {"study_id": context["study_id"]},
                }]}
            context["messages"] = messages
            return "已读取获批状态。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        context["study_id"] = json.loads(
            (run_dir / "research.json").read_text(encoding="utf-8"))["study_id"]
        db = run_dir / "research.sqlite"
        before = db.read_bytes()
        code, chat = self._post("/api/chat", {"text": "这项研究目前状态如何？"})
        self.assertEqual(200, code)
        self.assertTrue(chat["tool_calls"], chat)
        call = chat["tool_calls"][0]
        self.assertEqual("research.status", call["tool_name"])
        self.assertEqual("disclose", call["risk"])
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(call["preview"]["ok"])
        self.assertEqual(context["study_id"],
                         call["preview"]["research_status"]["study_id"])
        self.assertIsNone(call["result"])
        self.assertEqual(before, db.read_bytes())
        code, blocked = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(409, code)
        self.assertEqual("invalid_transition", blocked["error"]["code"])
        code, _ = self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "同意披露研究摘要"})
        self.assertEqual(200, code)
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        result = executed["data"]["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(context["study_id"], result["study_id"])
        self.assertEqual("active", result["study"]["status"])
        self.assertEqual(4, result["entity_counts"]["hypotheses"])
        self.assertEqual(before, db.read_bytes(), "研究状态工具必须零写入")
        code, _ = self._post("/api/chat", {"text": "请根据获批状态回答。"})
        self.assertEqual(200, code)
        messages = context["messages"]
        self.assertEqual(["user", "assistant", "tool"],
                         [item["role"] for item in messages[:3]])
        self.assertEqual("user", messages[-1]["role"])
        feedback = json.loads(messages[2]["content"])
        self.assertEqual("completed", feedback["status"])
        self.assertEqual(context["study_id"], feedback["result"]["study_id"])

    def test_agent_status_and_design_refuse_forged_research_snapshot(self):
        self._start(chat_llm=lambda system, messages: "准备核对",
                    research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        study_id = research["manifest"]["study_id"]
        design_id = research["designs"][0]["design_id"]
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            row = store._unsafe_conn.execute(
                "SELECT entity_id, payload FROM snapshots WHERE kind = 'hypothesis' "
                "ORDER BY updated_seq LIMIT 1").fetchone()
            forged = json.loads(row["payload"])
            forged["mechanism"] = "外部伪造的机制"
            with store._unsafe_conn:
                store._unsafe_conn.execute(
                    "UPDATE snapshots SET payload = ? WHERE kind = 'hypothesis' "
                    "AND entity_id = ?",
                    (json.dumps(forged, ensure_ascii=False), row["entity_id"]))
        finally:
            store.close()
        with self.assertRaisesRegex(ProtocolError, "完整性校验失败"):
            self.server.research_status_for_agent(study_id)
        with self.assertRaisesRegex(ProtocolError, "完整性校验失败"):
            self.server.research_design_for_agent(study_id, design_id)
        self._post("/api/chat", {"text": "读取当前研究状态"})
        _, chat = self._get("/api/chat")
        code, proposed = self._post(
            f"/api/v1/sessions/{chat['session_id']}/tool-calls", {
                "schema_version": "1.0", "turn_id": chat["turns"][-1]["turn_id"],
                "tool_name": "research.status", "arguments": {"study_id": study_id},
                "idempotency_key": "forged-status-read"})
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        _, current = self._get("/api/chat")
        preview = next(call["preview"] for call in current["tool_calls"]
                       if call["call_id"] == call_id)
        self.assertFalse(preview["ok"])
        self.assertNotIn("外部伪造的机制", json.dumps(preview, ensure_ascii=False))
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "测试误批准"})
        _, denied = self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual("failed", denied["data"]["status"])
        self.assertIsNone(denied["data"]["result"])

    def test_research_create_hypothesis_requires_approval_and_commits_once(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "提出可证伪假设。", "tool_calls": [{
                    "name": "research_create_hypothesis",
                    "arguments": context["proposal"],
                }]}
            context["messages"] = messages
            return "已登记未测试假设。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        context["proposal"] = {
            "study_id": research["manifest"]["study_id"],
            "expected_version": research["state_version"],
            "mechanism": "数值误差随步长减半而按二阶比例下降",
            "applicability": "固定光滑常微分方程与稳定步长区间",
            "predictions": ["步长减半时误差约缩小四倍"],
            "falsification": ["三次独立分辨率测试未出现二阶收敛"],
            "alternatives": ["截断误差之外的舍入误差主导"],
        }
        code, chat = self._post("/api/chat", {"text": "提出一个待检验假设"})
        self.assertEqual(200, code)
        call = chat["tool_calls"][0]
        self.assertEqual("research.create_hypothesis", call["tool_name"])
        self.assertEqual("write", call["risk"])
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(call["preview"]["ok"])
        self.assertTrue(call["preview"]["version_matches"])
        self.assertEqual(context["proposal"]["mechanism"],
                         call["preview"]["proposal"]["mechanism"])
        self.assertEqual(64, len(call["preview"]["proposal_sha256"]))
        _, unchanged = self._get("/api/research")
        self.assertEqual(len(research["hypotheses"]),
                         len(unchanged["runs"][0]["hypotheses"]))
        code, blocked = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(409, code)
        self.assertEqual("invalid_transition", blocked["error"]["code"])
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "可证伪且范围明确"})
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("completed", executed["data"]["status"])
        result = executed["data"]["result"]
        self.assertEqual("create_hypothesis", result["operation"])
        self.assertEqual("hypothesis", result["entity"]["kind"])
        self.assertEqual("untested", result["status"])
        self.assertEqual(research["state_version"] + 1, result["state_version"])
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            entity = store.get("hypothesis", result["entity"]["id"])
            self.assertEqual(context["proposal"]["mechanism"], entity["mechanism"])
            self.assertEqual(1, entity["version"])
            self.assertEqual(result, store.command_receipt(
                "tool-" + call["call_id"])["result"])
            self.assertEqual(result, store.execute_command(
                context["proposal"]["study_id"], "create_hypothesis",
                {key: context["proposal"][key] for key in (
                    "mechanism", "applicability", "predictions", "falsification",
                    "alternatives")},
                idempotency_key="tool-" + call["call_id"],
                expected_version=context["proposal"]["expected_version"],
                actor="agent", trace_id=call["trace_id"]))
            self.assertTrue(store.verify()["ok"])
        finally:
            store.close()
        _, updated = self._get("/api/research")
        self.assertEqual(len(research["hypotheses"]) + 1,
                         len(updated["runs"][0]["hypotheses"]))
        self.assertEqual(len(research["runs"]), len(updated["runs"][0]["runs"]))
        self.assertEqual(len(research["observations"]),
                         len(updated["runs"][0]["observations"]))
        self.assertEqual(0, self.server.wait_for_jobs(timeout=5))
        feedback = json.loads(context["messages"][2]["content"])
        self.assertEqual(result["entity"]["id"], feedback["result"]["entity"]["id"])

    def test_research_create_design_registers_draft_without_run(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "提出实验设计草稿。", "tool_calls": [{
                    "name": "research_create_design",
                    "arguments": context["proposal"],
                }]}
            context["messages"] = messages
            return "设计草稿已登记，但尚不能运行。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        context["proposal"] = self._design_proposal(research)
        code, chat = self._post("/api/chat", {"text": "为现有假设提出设计草稿"})
        self.assertEqual(200, code)
        call = chat["tool_calls"][0]
        self.assertEqual("research.create_design", call["tool_name"])
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(call["preview"]["ok"])
        self.assertTrue(call["preview"]["version_matches"])
        self.assertTrue(call["preview"]["hypothesis_version_matches"])
        self.assertFalse(call["preview"]["execution_ready"])
        _, unchanged = self._get("/api/research")
        self.assertEqual(len(research["designs"]),
                         len(unchanged["runs"][0]["designs"]))
        code, blocked = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(409, code)
        self.assertEqual("invalid_transition", blocked["error"]["code"])
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "核对设计字段"})
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("completed", executed["data"]["status"])
        result = executed["data"]["result"]
        self.assertEqual("create_design", result["operation"])
        self.assertEqual("draft", result["status"])
        self.assertFalse(result["execution_ready"])
        self.assertEqual(research["state_version"] + 1, result["state_version"])
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            entity = store.get("design", result["entity"]["id"])
            self.assertEqual(context["proposal"]["hypothesis_id"],
                             entity["hypothesis_id"])
            self.assertEqual("draft", entity["status"])
            self.assertEqual(result, store.command_receipt(
                "tool-" + call["call_id"])["result"])
            self.assertTrue(store.verify()["ok"])
        finally:
            store.close()
        readiness = self.server.research_design_for_agent(
            context["proposal"]["study_id"], result["entity"]["id"])[
                "execution_readiness"]
        self.assertFalse(readiness["controller_routing_ready"])
        self.assertIn("controller_design_id_unmapped", readiness["reasons"])
        self.assertIsNone(readiness["registered_candidate_match"])
        _, updated = self._get("/api/research")
        self.assertEqual(len(research["designs"]) + 1,
                         len(updated["runs"][0]["designs"]))
        self.assertEqual(len(research["runs"]), len(updated["runs"][0]["runs"]))
        self.assertEqual(len(research["observations"]),
                         len(updated["runs"][0]["observations"]))
        self.assertEqual(0, self.server.wait_for_jobs(timeout=5))
        feedback = json.loads(context["messages"][2]["content"])
        self.assertFalse(feedback["result"]["execution_ready"])

    def test_design_readiness_checks_core_candidate_registration(self):
        self._start(chat_llm=lambda system, messages: "ready", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, response = self._get("/api/research")
        state = response["runs"][0]
        manifest_path = run_dir / "research.json"
        original = manifest_path.read_bytes()
        try:
            manifest = json.loads(original)
            design = next(item for item in state["designs"]
                          if item["hypothesis_id"] != manifest["control_hypothesis_id"])
            original_readiness = controller_routing_readiness(run_dir, state, design)
            self.assertEqual(design["hypothesis_id"],
                             original_readiness["registered_candidate_match"])
            manifest["configs"][design["hypothesis_id"]] = {"unregistered": True}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            readiness = controller_routing_readiness(run_dir, state, design)
            self.assertIn("core_candidate_unregistered", readiness["reasons"])
            self.assertIn("manifest_identity_mismatch", readiness["reasons"])
            self.assertIsNone(readiness["registered_candidate_match"])
            self.assertFalse(readiness["controller_routing_ready"])
        finally:
            manifest_path.write_bytes(original)

    def test_agent_execute_design_requires_approval_and_returns_job(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "请求执行冻结设计", "tool_calls": [{
                    "name": "research_execute_design", "arguments": context["args"]}]}
            return "已收到 Job，等待真实执行结果。"

        self._start(chat_llm=fake_llm, chat_kwargs={"trusted_local": True},
                    research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            study_id = controller.manifest["study_id"]
            candidate_id = controller.manifest["candidate_hypothesis_ids"][0]
            design_id = controller._current_design_id(candidate_id)
        finally:
            controller.close()
        detail = self.server.research_design_for_agent(study_id, design_id)
        context["args"] = {"study_id": study_id, "design_id": design_id,
                           "expected_version": detail["state_version"],
                           "design_sha256": detail["design_sha256"]}
        code, chat = self._post("/api/chat", {"text": "执行这个冻结设计"})
        self.assertEqual(200, code)
        call = chat["tool_calls"][0]
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(call["preview"]["ok"])
        self.assertTrue(call["preview"]["design_sha256_matches"])
        self.assertEqual([], self.server.design_jobs_payload()["jobs"])
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "核对设计与预算"})
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        result = executed["data"]["result"]
        self.assertEqual(design_id, result["job"]["design_id"])
        self.assertEqual(0, self.server.wait_for_jobs(timeout=30))
        self.assertEqual("succeeded", self.server.design_job_payload(
            result["job"]["job_id"])["job"]["status"])

    def test_agent_study_action_uses_approved_preview_and_real_job(self):
        self._start(chat_llm=lambda system, messages: "等待研究作业",
                    chat_kwargs={"trusted_local": True}, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        study_id = json.loads((run_dir / "research.json").read_text(encoding="utf-8"))["study_id"]
        self._post("/api/chat", {"text": "执行预注册研究"})
        _, chat = self._get("/api/chat")
        endpoint = f"/api/v1/sessions/{chat['session_id']}/tool-calls"
        preview = self.server._agent_tool_public({"tool_name": "research.study_preview",
            "arguments": {"study_id": study_id, "action": "run", "max_steps": 1}})
        self.assertTrue(preview["preview"]["ok"])
        approval = preview["preview"]["study_action"]["approval_sha256"]
        arguments = {"study_id": study_id, "action": "run", "max_steps": 1,
                     "approval_sha256": approval}
        _, proposed = self._post(endpoint, {"schema_version": "1.0",
            "turn_id": chat["turns"][0]["turn_id"], "tool_name": "research.study_action",
            "arguments": arguments, "idempotency_key": "agent-study-action-001"})
        call_id = proposed["data"]["tool_call"]["call_id"]
        _, current = self._get("/api/chat")
        public = next(row for row in current["tool_calls"] if row["call_id"] == call_id)
        self.assertTrue(public["preview"]["approval_matches"])
        self.assertEqual([], self.server.study_jobs_payload(study_id)["jobs"])
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "核对范围和预算"})
        code, executed = self._post(f"/api/v1/tool-calls/{call_id}/execute",
                                    {"schema_version": "1.0"})
        self.assertEqual(200, code)
        job = executed["data"]["result"]["job"]
        self.assertEqual("tool-" + call_id, job["idempotency_key"])
        self.assertEqual(0, self.server.wait_for_jobs(timeout=60))
        jobs = self.server.study_jobs_payload(study_id)["jobs"]
        diagnostics = {"study_job": jobs[0]}
        if jobs[0]["status"] != "succeeded":
            # tearDown removes the synthetic project. Keep the real failed
            # receipt and process logs in the assertion, not merely "failed".
            artifacts = {}
            for path in (run_dir / "jobs").glob("JOB-*/*"):
                if path.name not in {"job.json", "receipt.json", "stderr.log",
                                     "supervisor.log"} or not path.is_file():
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")[-4096:]
                except OSError as error:
                    content = f"unreadable: {type(error).__name__}: {error}"
                artifacts[str(path.relative_to(run_dir))] = content
            diagnostics["worker_artifacts"] = artifacts
        self.assertEqual("succeeded", jobs[0]["status"], diagnostics)
        self.assertTrue(jobs[0]["result"]["observation_ids"])

    def test_agent_bound_draft_uses_all_verified_claims_and_approval(self):
        self._start(chat_llm=lambda system, messages: "等待稿件审查",
                    chat_kwargs={"trusted_local": True}, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        controller = ResearchController(run_dir)
        try:
            self.assertEqual("ready_for_confirmation",
                             controller.run(trusted_local=True, max_steps=2)["phase"])
            controller.confirm(trusted_local=True)
        finally:
            controller.close()
        state = read_research_state(run_dir)
        study_id = state["manifest"]["study_id"]
        base = {"study_id": study_id,
                "expected_state_version": state["state_version"],
                "title": "研究结果草稿", "author_note": "需要人工核对外部文献。"}
        preview = self.server.agent_bound_draft_preview(**base)
        self.assertEqual({claim["entity_id"] for claim in state["claims"]},
                         set(preview["claim_ids"]))
        self.assertFalse(preview["author_note_verified"])
        with self.assertRaisesRegex(ProtocolError, "重新预览"):
            self.server.agent_create_bound_draft(
                **base, approval_sha256="0" * 64)
        with self.assertRaisesRegex(ProtocolError, "状态已变化"):
            self.server.agent_bound_draft_preview(
                **{**base, "expected_state_version": state["state_version"] - 1})
        self._post("/api/chat", {"text": "生成证据绑定草稿"})
        _, chat = self._get("/api/chat")
        endpoint = f"/api/v1/sessions/{chat['session_id']}/tool-calls"
        arguments = {**base, "approval_sha256": preview["approval_sha256"]}
        code, proposed = self._post(endpoint, {"schema_version": "1.0",
            "turn_id": chat["turns"][0]["turn_id"], "tool_name": "research.draft_create",
            "arguments": arguments, "idempotency_key": "agent-bound-draft-001"})
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        _, current = self._get("/api/chat")
        public = next(row for row in current["tool_calls"] if row["call_id"] == call_id)
        self.assertTrue(public["preview"]["ok"])
        self.assertEqual([], self.server.bound_drafts_payload(study_id)["drafts"])
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "核对全部 Claim"})
        code, executed = self._post(f"/api/v1/tool-calls/{call_id}/execute",
                                    {"schema_version": "1.0"})
        self.assertEqual(200, code)
        result = executed["data"]["result"]
        self.assertEqual("draft_needs_review", result["status"])
        self.assertEqual(len(preview["claim_ids"]), result["claim_count"])
        self.assertFalse(result["publication_attested"])
        self.assertEqual(1, len(self.server.bound_drafts_payload(study_id)["drafts"]))
        self.assertTrue(self.server.agent_create_bound_draft(
            **arguments)["replayed"])

    def test_research_create_design_rejects_stale_hypothesis_version(self):
        self._start(chat_llm=lambda system, messages: "准备设计", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        self._post("/api/chat", {"text": "准备设计"})
        _, chat = self._get("/api/chat")
        endpoint = f"/api/v1/sessions/{chat['session_id']}/tool-calls"
        proposal = self._design_proposal(research)
        code, _ = self._post(endpoint, {
            "schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
            "tool_name": "research.create_design",
            "arguments": {**proposal, "interventions": []},
            "idempotency_key": "design-empty-intervention"})
        self.assertEqual(400, code)
        code, proposed = self._post(endpoint, {
            "schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
            "tool_name": "research.create_design",
            "arguments": {**proposal, "expected_hypothesis_version":
                          proposal["expected_hypothesis_version"] + 1},
            "idempotency_key": "design-stale-hypothesis"})
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        _, current = self._get("/api/chat")
        preview = next(call["preview"] for call in current["tool_calls"]
                       if call["call_id"] == call_id)
        self.assertTrue(preview["ok"])
        self.assertFalse(preview["hypothesis_version_matches"])
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "测试门禁"})
        _, failed = self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual("failed", failed["data"]["status"])
        self.assertIn("hypothesis 版本冲突", failed["data"]["error"]["message"])
        _, unchanged = self._get("/api/research")
        self.assertEqual(len(research["designs"]),
                         len(unchanged["runs"][0]["designs"]))

    def test_restart_reconciles_committed_design_without_duplicate(self):
        self._start(chat_llm=lambda system, messages: "准备设计", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        arguments = self._design_proposal(research)
        _, chat = self._post("/api/chat", {"text": "登记设计"})
        code, proposed = self._post(
            f"/api/v1/sessions/{chat['session_id']}/tool-calls", {
                "schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
                "tool_name": "research.create_design",
                "arguments": arguments, "idempotency_key": "design-crash-window"})
        self.assertEqual(201, code)
        call = proposed["data"]["tool_call"]
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "已审查草稿"})
        session_store = AgentSessionStore(self.root)
        try:
            session_store.claim_tool_call(call["call_id"])
        finally:
            session_store.close()
        payload = {key: arguments[key] for key in (
            "hypothesis_id", "expected_hypothesis_version", "interventions",
            "control", "baseline", "metric", "scorer_id", "metric_direction",
            "analysis_unit", "splits", "repeat_values", "min_meaningful_effect")}
        research_store = ResearchStore(run_dir / "research.sqlite")
        try:
            committed = research_store.execute_command(
                arguments["study_id"], "create_design", payload,
                idempotency_key="tool-" + call["call_id"],
                expected_version=arguments["expected_version"],
                actor="agent", trace_id=call["trace_id"])
            cursor = research_store.event_cursor()
        finally:
            research_store.close()
        self._stop()
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  research_runs=(run_dir,))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as response:
            self.token = json.loads(response.read())["token"]
        _, recovered = self._get("/api/chat")
        outcome = next(item for item in recovered["tool_calls"]
                       if item["call_id"] == call["call_id"])
        self.assertEqual("completed", outcome["status"])
        self.assertEqual(committed, outcome["result"])
        research_store = ResearchStore(run_dir / "research.sqlite")
        try:
            self.assertEqual(cursor, research_store.event_cursor())
            self.assertEqual("draft", research_store.get(
                "design", committed["entity"]["id"])["status"])
            self.assertTrue(research_store.verify()["ok"])
        finally:
            research_store.close()

    def test_research_create_hypothesis_rejects_bad_content_and_stale_version(self):
        self._start(chat_llm=lambda system, messages: "准备提案", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        self._post("/api/chat", {"text": "准备"})
        _, chat = self._get("/api/chat")
        endpoint = f"/api/v1/sessions/{chat['session_id']}/tool-calls"
        arguments = {
            "study_id": research["manifest"]["study_id"],
            "expected_version": research["state_version"] + 1,
            "mechanism": "候选机制", "applicability": "公开基准",
            "predictions": ["可测量预测"], "falsification": ["可测量反例"],
            "alternatives": ["替代机制"],
        }
        base = {"schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
                "tool_name": "research.create_hypothesis"}
        code, _ = self._post(endpoint, {
            **base, "arguments": {**arguments, "falsification": []},
            "idempotency_key": "hypothesis-invalid"})
        self.assertEqual(400, code)
        code, proposed = self._post(endpoint, {
            **base, "arguments": arguments,
            "idempotency_key": "hypothesis-stale"})
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        _, chat = self._get("/api/chat")
        preview = next(call["preview"] for call in chat["tool_calls"]
                       if call["call_id"] == call_id)
        self.assertFalse(preview["version_matches"])
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "竞态测试"})
        _, failed = self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual("failed", failed["data"]["status"])
        self.assertIn("版本冲突", failed["data"]["error"]["message"])
        _, unchanged = self._get("/api/research")
        self.assertEqual(len(research["hypotheses"]),
                         len(unchanged["runs"][0]["hypotheses"]))

    def test_restart_reconciles_committed_hypothesis_without_duplicate(self):
        self._start(chat_llm=lambda system, messages: "准备提案", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        study_id, version = research["manifest"]["study_id"], research["state_version"]
        payload = {
            "mechanism": "处理变量改变响应水平", "applicability": "合成公开数据",
            "predictions": ["处理组均值较高"],
            "falsification": ["处理组均值未高于对照组"],
            "alternatives": ["样本分配偏差"],
        }
        _, chat = self._post("/api/chat", {"text": "登记假设"})
        code, proposed = self._post(
            f"/api/v1/sessions/{chat['session_id']}/tool-calls", {
                "schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
                "tool_name": "research.create_hypothesis",
                "arguments": {"study_id": study_id, "expected_version": version,
                              **payload},
                "idempotency_key": "hypothesis-crash-window"})
        self.assertEqual(201, code)
        call = proposed["data"]["tool_call"]
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "已审查提案"})
        session_store = AgentSessionStore(self.root)
        try:
            session_store.claim_tool_call(call["call_id"])
        finally:
            session_store.close()
        research_store = ResearchStore(run_dir / "research.sqlite")
        try:
            committed = research_store.execute_command(
                study_id, "create_hypothesis", payload,
                idempotency_key="tool-" + call["call_id"],
                expected_version=version, actor="agent", trace_id=call["trace_id"])
            cursor = research_store.event_cursor()
        finally:
            research_store.close()
        self._stop()
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  research_runs=(run_dir,))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as response:
            self.token = json.loads(response.read())["token"]
        _, recovered = self._get("/api/chat")
        outcome = next(item for item in recovered["tool_calls"]
                       if item["call_id"] == call["call_id"])
        self.assertEqual("completed", outcome["status"])
        self.assertEqual(committed, outcome["result"])
        research_store = ResearchStore(run_dir / "research.sqlite")
        try:
            self.assertEqual(cursor, research_store.event_cursor())
            self.assertEqual("untested", research_store.get(
                "hypothesis", committed["entity"]["id"])["status"])
            self.assertTrue(research_store.verify()["ok"])
        finally:
            research_store.close()

    def test_research_status_tool_rejects_paths_and_unregistered_studies(self):
        self._start(chat_llm=lambda system, messages: "准备读取")
        _, chat = self._post("/api/chat", {"text": "检查研究"})
        endpoint = f"/api/v1/sessions/{chat['session_id']}/tool-calls"
        base = {"schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
                "tool_name": "research.status"}
        code, denied = self._post(endpoint, {
            **base, "arguments": {"study_id": "../../private/research.sqlite"},
            "idempotency_key": "research-status-path-denied"})
        self.assertEqual(400, code)
        self.assertEqual("invalid_request", denied["error"]["code"])
        code, proposed = self._post(endpoint, {
            **base, "arguments": {"study_id": new_id(EntityKind.STUDY)},
            "idempotency_key": "research-status-unknown"})
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        code, _ = self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "仅测试登记边界"})
        self.assertEqual(200, code)
        code, failed = self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("failed", failed["data"]["status"])
        self.assertIn("未登记 study", failed["data"]["error"]["message"])

    def test_research_inspect_design_requires_approval_and_returns_real_design(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "先检查设计。", "tool_calls": [{
                    "name": "research_inspect_design",
                    "arguments": {"study_id": context["study_id"],
                                  "design_id": context["design_id"],
                                  "expected_version": context["version"]},
                }]}
            context["messages"] = messages
            return "已查看获批设计。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        design_id = self._create_draft_design(run_dir)
        _, state = self._get("/api/research")
        context.update(study_id=state["runs"][0]["manifest"]["study_id"],
                       design_id=design_id,
                       version=state["runs"][0]["state_version"])
        db = run_dir / "research.sqlite"
        before = db.read_bytes()
        code, chat = self._post("/api/chat", {"text": "先看设计内容，再决定是否冻结"})
        self.assertEqual(200, code)
        call = chat["tool_calls"][0]
        self.assertEqual("research.inspect_design", call["tool_name"])
        self.assertEqual("disclose", call["risk"])
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(call["preview"]["ok"])
        self.assertTrue(call["preview"]["version_matches"])
        self.assertEqual(design_id, call["preview"]["design"]["design_id"])
        self.assertEqual(call["preview"]["design"]["hypothesis_id"],
                         call["preview"]["hypothesis"]["hypothesis_id"])
        self.assertTrue(call["preview"]["hypothesis_version_matches"])
        self.assertFalse(call["preview"]["execution_readiness"]["controller_routing_ready"])
        self.assertIn("design_not_frozen",
                      call["preview"]["execution_readiness"]["reasons"])
        self.assertEqual(before, db.read_bytes())
        code, blocked = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(409, code)
        self.assertEqual("invalid_transition", blocked["error"]["code"])
        code, _ = self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "允许查看设计"})
        self.assertEqual(200, code)
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        result = executed["data"]["result"]
        self.assertEqual(design_id, result["design"]["design_id"])
        self.assertEqual("draft", result["design"]["status"])
        self.assertEqual(result["design"]["hypothesis_id"],
                         result["hypothesis"]["hypothesis_id"])
        self.assertFalse(result["execution_readiness"]["controller_routing_ready"])
        self.assertEqual(state["runs"][0]["state_version"], result["state_version"])
        self.assertEqual(64, len(result["design_sha256"]))
        self.assertEqual(before, db.read_bytes(), "设计查看必须零写入")
        self._post("/api/chat", {"text": "现在可以判断设计吗？"})
        feedback = json.loads(context["messages"][2]["content"])
        self.assertEqual(design_id, feedback["result"]["design_id"])

    def test_research_inspect_design_rejects_foreign_or_invalid_ids(self):
        self._start(chat_llm=lambda system, messages: "准备查看", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        study_id = state["runs"][0]["manifest"]["study_id"]
        version = state["runs"][0]["state_version"]
        self._post("/api/chat", {"text": "检查设计"})
        _, chat = self._get("/api/chat")
        endpoint = f"/api/v1/sessions/{chat['session_id']}/tool-calls"
        base = {"schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
                "tool_name": "research.inspect_design"}
        code, _ = self._post(endpoint, {
            **base, "arguments": {"study_id": study_id, "design_id": "../../secret",
                                  "expected_version": version},
            "idempotency_key": "inspect-invalid"})
        self.assertEqual(400, code)
        code, proposed = self._post(endpoint, {
            **base, "arguments": {"study_id": study_id,
                                  "design_id": new_id(EntityKind.DESIGN),
                                  "expected_version": version},
            "idempotency_key": "inspect-unknown"})
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        _, current = self._get("/api/chat")
        preview = next(item["preview"] for item in current["tool_calls"]
                       if item["call_id"] == call_id)
        self.assertFalse(preview["ok"])
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "仅测试边界"})
        code, failed = self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("failed", failed["data"]["status"])
        self.assertIn("不存在", failed["data"]["error"]["message"])

    def test_research_inspect_design_rejects_stale_version_before_disclosure(self):
        self._start(chat_llm=lambda system, messages: "准备查看", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        self._post("/api/chat", {"text": "检查设计"})
        _, chat = self._get("/api/chat")
        code, proposed = self._post(
            f"/api/v1/sessions/{chat['session_id']}/tool-calls", {
                "schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
                "tool_name": "research.inspect_design",
                "arguments": {"study_id": research["manifest"]["study_id"],
                              "design_id": research["designs"][0]["design_id"],
                              "expected_version": research["state_version"] + 1},
                "idempotency_key": "inspect-stale"})
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        _, current = self._get("/api/chat")
        preview = next(item["preview"] for item in current["tool_calls"]
                       if item["call_id"] == call_id)
        self.assertTrue(preview["ok"])
        self.assertFalse(preview["version_matches"])
        self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "测试竞态"})
        _, failed = self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual("failed", failed["data"]["status"])
        self.assertIsNone(failed["data"]["result"])
        self.assertIn("版本已变化", failed["data"]["error"]["message"])

    def test_research_inspect_observation_rechecks_active_study_evidence(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "先核对开发评估。", "tool_calls": [{
                    "name": "research_inspect_observation",
                    "arguments": {"study_id": context["study_id"],
                                  "observation_id": context["observation_id"],
                                  "expected_version": context["version"]},
                }]}
            return "已收到获批评估结果。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        project = Path(self.tmp.name) / "ode"
        initialize_demo(project, "ode-convergence")
        run_dir = self.research_dirs[0]
        ResearchController.initialize(project, run_dir)
        controller = ResearchController(run_dir)
        try:
            controller.run(trusted_local=True, max_steps=1)
        finally:
            controller.close()
        _, state = self._get("/api/research")
        research = state["runs"][0]
        observation = research["observations"][0]
        context.update(study_id=research["manifest"]["study_id"],
                       observation_id=observation["entity_id"],
                       version=research["state_version"])
        summary = self.server.research_status_for_agent(context["study_id"])
        self.assertIn(context["observation_id"], [
            row["observation_id"] for row in summary["observations"]])
        code, chat = self._post("/api/chat", {"text": "先核对开发评估证据"})
        self.assertEqual(200, code)
        call = chat["tool_calls"][0]
        self.assertEqual("research.inspect_observation", call["tool_name"])
        self.assertEqual("disclose", call["risk"])
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(call["preview"]["ok"])
        self.assertTrue(call["preview"]["version_matches"])
        self.assertEqual(context["observation_id"],
                         call["preview"]["observation"]["entity_id"])
        self.assertNotIn("artifact_id", call["preview"]["observation"])
        self.assertNotIn(str(project), json.dumps(call["preview"]))
        code, blocked = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(409, code)
        self.assertEqual("invalid_transition", blocked["error"]["code"])
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "允许披露单项评估"})
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        result = executed["data"]["result"]
        self.assertEqual(context["observation_id"], result["observation_id"])
        self.assertEqual(observation["value"], result["observation"]["value"])
        self.assertEqual("local_research_store_and_scoring_artifact",
                         result["verification_level"])
        self.assertNotIn("artifact_id", result["observation"])
        self._post("/api/chat", {"text": "核对另一项开发结果"})
        _, current_chat = self._get("/api/chat")
        code, proposed = self._post(
            f"/api/v1/sessions/{current_chat['session_id']}/tool-calls", {
                "schema_version": "1.0", "turn_id": current_chat["turns"][-1]["turn_id"],
                "tool_name": "research.inspect_observation",
                "arguments": {"study_id": context["study_id"],
                              "observation_id": context["observation_id"],
                              "expected_version": context["version"] + 1},
                "idempotency_key": "inspect-observation-stale"})
        self.assertEqual(201, code)
        stale_id = proposed["data"]["tool_call"]["call_id"]
        self._post(f"/api/v1/tool-calls/{stale_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "测试版本竞态"})
        _, rejected = self._post(f"/api/v1/tool-calls/{stale_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual("failed", rejected["data"]["status"])
        self.assertIsNone(rejected["data"]["result"])
        self.assertIn("版本已变化", rejected["data"]["error"]["message"])
        with self.assertRaises(LookupError):
            self.server.research_observation_for_agent(
                context["study_id"], new_id(EntityKind.OBSERVATION))
        artifact = Path(observation["artifact_id"])
        artifact.write_bytes(artifact.read_bytes() + b" ")
        with self.assertRaises(ProtocolError):
            self.server.research_observation_for_agent(
                context["study_id"], context["observation_id"])

    def test_research_inspect_observation_never_discloses_confirmation_values(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "请求查看确认观察。", "tool_calls": [{
                    "name": "research_inspect_observation",
                    "arguments": {"study_id": context["study_id"],
                                  "observation_id": context["observation_id"],
                                  "expected_version": context["version"]},
                }]}
            return "不能通过开发工具读取确认结果。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        project = Path(self.tmp.name) / "ode"
        initialize_demo(project, "ode-convergence")
        run_dir = self.research_dirs[0]
        ResearchController.initialize(project, run_dir)
        controller = ResearchController(run_dir)
        try:
            self.assertEqual("ready_for_confirmation",
                             controller.run(trusted_local=True, max_steps=2)["phase"])
            controller.confirm(trusted_local=True)
        finally:
            controller.close()
        _, state = self._get("/api/research")
        research = state["runs"][0]
        confirmation = next(item for item in research["observations"]
                            if item["scope"] == "confirmation")
        context.update(study_id=research["manifest"]["study_id"],
                       observation_id=confirmation["entity_id"],
                       version=research["state_version"])
        summary = self.server.research_status_for_agent(context["study_id"])
        self.assertNotIn(context["observation_id"], [
            item["observation_id"] for item in summary["observations"]])
        self.assertTrue(summary["observations"], "开发评估仍应可发现")
        original_project = self.server.project
        try:
            self.server.project = project
            prompt = self.server._chat_system_prompt()
        finally:
            self.server.project = original_project
        self.assertIn('"split":"dev"', prompt)
        self.assertNotIn('"split":"test"', prompt)
        code, chat = self._post("/api/chat", {"text": "检查确认观察"})
        self.assertEqual(200, code)
        call = chat["tool_calls"][0]
        self.assertEqual("research.inspect_observation", call["tool_name"])
        self.assertFalse(call["preview"]["ok"])
        self.assertIn("仅允许读取开发评估", call["preview"]["reason"])
        self.assertNotIn(str(confirmation["value"]),
                         json.dumps(call["preview"], ensure_ascii=False))
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "测试误批准"})
        _, denied = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual("failed", denied["data"]["status"])
        self.assertIsNone(denied["data"]["result"])
        self.assertIn("仅允许读取开发评估", denied["data"]["error"]["message"])

    def test_research_inspect_claim_requires_approval_and_live_evidence(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "先读取已验证结论。", "tool_calls": [{
                    "name": "research_inspect_claim",
                    "arguments": {"study_id": context["study_id"],
                                  "claim_id": context["claim_id"],
                                  "expected_version": context["version"]},
                }]}
            return "已收到批准披露的范围限定结论。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        project = Path(self.tmp.name) / "ode"
        initialize_demo(project, "ode-convergence")
        run_dir = self.research_dirs[0]
        ResearchController.initialize(project, run_dir)
        controller = ResearchController(run_dir)
        try:
            self.assertEqual("ready_for_confirmation",
                             controller.run(trusted_local=True, max_steps=2)["phase"])
            self.assertEqual(1, len(controller.confirm(trusted_local=True)["claims"]))
        finally:
            controller.close()
        _, state = self._get("/api/research")
        research = state["runs"][0]
        context.update(study_id=research["manifest"]["study_id"],
                       claim_id=research["claims"][0]["entity_id"],
                       version=research["state_version"])
        code, chat = self._post("/api/chat", {"text": "只在核验证据后说明结论"})
        self.assertEqual(200, code)
        call = chat["tool_calls"][0]
        self.assertEqual("research.inspect_claim", call["tool_name"])
        self.assertEqual("disclose", call["risk"])
        self.assertEqual("pending_approval", call["status"])
        self.assertEqual(context["claim_id"], call["preview"]["binding"]["claim_id"])
        self.assertTrue(call["preview"]["version_matches"])
        code, blocked = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(409, code)
        self.assertEqual("invalid_transition", blocked["error"]["code"])
        self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "允许披露单项证据"})
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        result = executed["data"]["result"]
        self.assertEqual(context["claim_id"], result["binding"]["claim_id"])
        self.assertEqual(2, len(result["binding"]["sources"]))
        self.assertEqual("local_research_store_and_source_artifacts",
                         result["verification_level"])
        self.assertEqual(context["version"], result["state_version"])
        self._post("/api/chat", {"text": "检查下一项证据"})
        _, current_chat = self._get("/api/chat")
        turn_id = current_chat["turns"][-1]["turn_id"]
        code, proposed = self._post(
            f"/api/v1/sessions/{current_chat['session_id']}/tool-calls", {
                "schema_version": "1.0", "turn_id": turn_id,
                "tool_name": "research.inspect_claim",
                "arguments": {"study_id": context["study_id"],
                              "claim_id": context["claim_id"],
                              "expected_version": context["version"] + 1},
                "idempotency_key": "inspect-claim-stale"})
        self.assertEqual(201, code)
        stale_id = proposed["data"]["tool_call"]["call_id"]
        self._post(f"/api/v1/tool-calls/{stale_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "测试版本竞态"})
        _, rejected = self._post(f"/api/v1/tool-calls/{stale_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual("failed", rejected["data"]["status"])
        self.assertIsNone(rejected["data"]["result"])
        self.assertIn("版本已变化", rejected["data"]["error"]["message"])
        with self.assertRaises(LookupError):
            self.server.research_claim_for_agent(
                context["study_id"], new_id(EntityKind.CLAIM))
        input_path = project / "train.json"
        input_path.write_bytes(input_path.read_bytes() + b" ")
        with self.assertRaises(ProtocolError):
            self.server.research_claim_for_agent(
                context["study_id"], context["claim_id"])

    def test_research_inspect_design_flags_hypothesis_revision_mismatch(self):
        self._start(chat_llm=lambda system, messages: "准备查看", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        design_id = self._create_draft_design(run_dir)
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            design = store.get("design", design_id)
            hypothesis = store.get("hypothesis", design["hypothesis_id"])
            store.revise_hypothesis(Hypothesis(
                hypothesis_id=hypothesis["hypothesis_id"],
                study_id=hypothesis["study_id"],
                version=hypothesis["version"] + 1,
                parent_version=hypothesis["version"],
                mechanism=hypothesis["mechanism"] + "（修订）",
                applicability=hypothesis["applicability"],
                predictions=tuple(hypothesis["predictions"]),
                falsification=tuple(hypothesis["falsification"]),
                alternatives=tuple(hypothesis["alternatives"]),
            ))
        finally:
            store.close()
        result = self.server.research_design_for_agent(
            hypothesis["study_id"], design_id)
        self.assertFalse(result["hypothesis_version_matches"])
        self.assertEqual(hypothesis["version"] + 1,
                         result["hypothesis"]["version"])
        self.assertEqual(hypothesis["version"],
                         result["design"]["hypothesis_version"])
        self.assertIn("hypothesis_version_mismatch",
                      result["execution_readiness"]["reasons"])

    def test_registered_frozen_design_has_controller_route_but_not_execution_proof(self):
        self._start(chat_llm=lambda system, messages: "检查设计", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        design = research["designs"][0]
        result = self.server.research_design_for_agent(
            research["manifest"]["study_id"], design["design_id"])
        readiness = result["execution_readiness"]
        self.assertTrue(readiness["controller_routing_ready"], readiness)
        self.assertFalse(readiness["execution_ready"])
        self.assertIn("source_inputs", readiness["not_checked"])

    def test_research_freeze_design_requires_approval_and_uses_kernel(self):
        context = {"calls": 0}

        def fake_llm(system, messages):
            context["calls"] += 1
            if context["calls"] == 1:
                return {"text": "建议冻结预注册设计。", "tool_calls": [{
                    "name": "research_freeze_design",
                    "arguments": {"study_id": context["study_id"],
                                  "design_id": context["design_id"],
                                  "expected_version": context["version"]},
                }]}
            context["messages"] = messages
            return "设计已冻结。"

        self._start(chat_llm=fake_llm, research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        draft_id = self._create_draft_design(run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        design = next(item for item in research["designs"]
                      if item["design_id"] == draft_id)
        context.update(study_id=research["manifest"]["study_id"],
                       design_id=design["design_id"],
                       version=research["state_version"])
        code, chat = self._post("/api/chat", {"text": "请提出冻结设计的请求"})
        self.assertEqual(200, code)
        call = chat["tool_calls"][0]
        self.assertEqual("research.freeze_design", call["tool_name"])
        self.assertEqual("write", call["risk"])
        self.assertEqual("pending_approval", call["status"])
        self.assertTrue(call["preview"]["ok"])
        self.assertTrue(call["preview"]["version_matches"])
        self.assertEqual(context["design_id"], call["preview"]["design"]["design_id"])
        code, blocked = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(409, code)
        self.assertEqual("invalid_transition", blocked["error"]["code"])
        code, approved = self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "核对过设计"})
        self.assertEqual(200, code)
        self.assertEqual("approved", approved["data"]["status"])
        code, executed = self._post(f"/api/v1/tool-calls/{call['call_id']}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("completed", executed["data"]["status"], executed["data"])
        self.assertEqual("frozen", executed["data"]["result"]["status"])
        self.assertEqual(context["version"] + 1,
                         executed["data"]["result"]["state_version"])
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            receipt = store.command_receipt("tool-" + call["call_id"])
            self.assertEqual("agent", receipt["actor"])
            self.assertEqual(context["study_id"], receipt["study_id"])
            self.assertTrue(store.verify()["ok"])
        finally:
            store.close()
        _, updated = self._get("/api/research")
        frozen = next(item for item in updated["runs"][0]["designs"]
                      if item["design_id"] == context["design_id"])
        self.assertEqual("frozen", frozen["status"])
        code, _ = self._post("/api/chat", {"text": "获批操作的结果是什么？"})
        self.assertEqual(200, code)
        feedback = json.loads(context["messages"][2]["content"])
        self.assertEqual("completed", feedback["status"])
        self.assertEqual("frozen", feedback["result"]["status"])

    def test_restart_reconciles_committed_research_tool_receipt(self):
        self._start(chat_llm=lambda system, messages: "提出设计操作", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        design_id = self._create_draft_design(run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        study_id = research["manifest"]["study_id"]
        version = research["state_version"]
        _, chat = self._post("/api/chat", {"text": "准备冻结设计"})
        endpoint = f"/api/v1/sessions/{chat['session_id']}/tool-calls"
        code, proposed = self._post(endpoint, {
            "schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
            "tool_name": "research.freeze_design",
            "arguments": {"study_id": study_id, "design_id": design_id,
                          "expected_version": version},
            "idempotency_key": "research-freeze-crash-window",
        })
        self.assertEqual(201, code)
        call = proposed["data"]["tool_call"]
        code, _ = self._post(f"/api/v1/tool-calls/{call['call_id']}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "已审查设计"})
        self.assertEqual(200, code)
        session_store = AgentSessionStore(self.root)
        try:
            session_store.claim_tool_call(call["call_id"])
        finally:
            session_store.close()
        research_store = ResearchStore(run_dir / "research.sqlite")
        try:
            committed = research_store.execute_command(
                study_id, "freeze_design", {"design_id": design_id},
                idempotency_key="tool-" + call["call_id"],
                expected_version=version, actor="agent", trace_id=call["trace_id"])
            cursor = research_store.event_cursor()
        finally:
            research_store.close()
        self._stop()
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  research_runs=(run_dir,))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as response:
            self.token = json.loads(response.read())["token"]
        _, recovered = self._get("/api/chat")
        outcome = next(item for item in recovered["tool_calls"]
                       if item["call_id"] == call["call_id"])
        self.assertEqual("completed", outcome["status"])
        self.assertEqual(committed, outcome["result"])
        research_store = ResearchStore(run_dir / "research.sqlite")
        try:
            self.assertEqual(cursor, research_store.event_cursor(),
                             "恢复只能重放收据，不能重复写事件")
            self.assertEqual("frozen", research_store.get("design", design_id)["status"])
        finally:
            research_store.close()

    def test_research_freeze_design_rejects_stale_study_version(self):
        self._start(chat_llm=lambda system, messages: "建议冻结", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        design_id = self._create_draft_design(run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        _, chat = self._post("/api/chat", {"text": "准备冻结"})
        code, proposed = self._post(
            f"/api/v1/sessions/{chat['session_id']}/tool-calls", {
                "schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
                "tool_name": "research.freeze_design",
                "arguments": {"study_id": research["manifest"]["study_id"],
                              "design_id": design_id,
                              "expected_version": research["state_version"]},
                "idempotency_key": "research-freeze-stale-version",
            })
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        self._create_draft_design(run_dir, "D-agent-other-draft")
        _, current = self._get("/api/chat")
        preview = next(item["preview"] for item in current["tool_calls"]
                       if item["call_id"] == call_id)
        self.assertFalse(preview["version_matches"])
        code, _ = self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "测试版本门禁"})
        self.assertEqual(200, code)
        code, result = self._post(f"/api/v1/tool-calls/{call_id}/execute", {
            "schema_version": "1.0"})
        self.assertEqual(200, code)
        self.assertEqual("failed", result["data"]["status"])
        self.assertEqual("ResearchVersionConflict", result["data"]["error"]["type"])
        store = ResearchStore(run_dir / "research.sqlite")
        try:
            self.assertEqual("draft", store.get("design", design_id)["status"])
        finally:
            store.close()

    def test_restart_does_not_replay_research_tool_without_receipt(self):
        self._start(chat_llm=lambda system, messages: "提出设计操作", research_names=("research",))
        run_dir = self.research_dirs[0]
        ResearchController.initialize(self.root, run_dir)
        design_id = self._create_draft_design(run_dir)
        _, state = self._get("/api/research")
        research = state["runs"][0]
        _, chat = self._post("/api/chat", {"text": "准备冻结设计"})
        code, proposed = self._post(
            f"/api/v1/sessions/{chat['session_id']}/tool-calls", {
                "schema_version": "1.0", "turn_id": chat["turns"][0]["turn_id"],
                "tool_name": "research.freeze_design",
                "arguments": {"study_id": research["manifest"]["study_id"],
                              "design_id": design_id,
                              "expected_version": research["state_version"]},
                "idempotency_key": "research-freeze-no-receipt",
            })
        self.assertEqual(201, code)
        call_id = proposed["data"]["tool_call"]["call_id"]
        code, _ = self._post(f"/api/v1/tool-calls/{call_id}/decision", {
            "schema_version": "1.0", "decision": "approve", "reason": "测试无收据恢复"})
        self.assertEqual(200, code)
        session_store = AgentSessionStore(self.root)
        try:
            session_store.claim_tool_call(call_id)
        finally:
            session_store.close()
        research_store = ResearchStore(run_dir / "research.sqlite")
        try:
            cursor = research_store.event_cursor()
        finally:
            research_store.close()
        self._stop()
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  research_runs=(run_dir,))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as response:
            self.token = json.loads(response.read())["token"]
        _, recovered = self._get("/api/chat")
        call = next(item for item in recovered["tool_calls"]
                    if item["call_id"] == call_id)
        self.assertEqual("interrupted", call["status"])
        self.assertEqual("OutcomeUnknown", call["error"]["type"])
        research_store = ResearchStore(run_dir / "research.sqlite")
        try:
            self.assertEqual(cursor, research_store.event_cursor())
            self.assertEqual("draft", research_store.get("design", design_id)["status"])
            self.assertIsNone(research_store.command_receipt("tool-" + call_id))
        finally:
            research_store.close()

    def test_v1_turn_returns_immediately_and_cancel_wins_late_model_race(self):
        entered, release = threading.Event(), threading.Event()

        def slow_llm(system, messages):
            entered.set()
            release.wait(3)
            return "不应覆盖取消状态"

        self._start(chat_llm=slow_llm)
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "取消测试", "harness": "chat"})
        session_id = created["data"]["session_id"]
        started = time.monotonic()
        code, response = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "cancel-http-turn-0001",
            "text": "执行一个慢请求"})
        self.assertEqual(202, code)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual("running", response["data"]["turn"]["status"])
        self.assertIn(response["data"]["job"]["status"], {"queued", "running"})
        turn_id = response["data"]["turn"]["turn_id"]
        self.assertTrue(entered.wait(1))
        code, cancelled = self._post(f"/api/v1/turns/{turn_id}/cancel", {
            "schema_version": "1.0", "reason": "用户点击停止"})
        self.assertEqual(200, code)
        self.assertEqual("interrupted", cancelled["data"]["turn"]["status"])
        self.assertEqual("Cancelled", cancelled["data"]["turn"]["error"]["type"])
        release.set()
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        _, chat = self._get("/api/chat")
        assistant = chat["messages"][-1]
        self.assertEqual("Cancelled", assistant["error"]["type"])
        self.assertEqual("cancelled", chat["jobs"][0]["status"])
        code, page = self._get(
            f"/api/v1/sessions/{session_id}/events?after=0&limit=1")
        self.assertEqual(200, code)
        self.assertEqual("turn.started", page["data"]["events"][0]["kind"])
        self.assertTrue(page["data"]["integrity"]["ok"])
        self.assertTrue(page["data"]["has_more"])
        cursor = page["data"]["cursor"]
        code, rest = self._get(
            f"/api/v1/sessions/{session_id}/events?after={cursor}&limit=10")
        self.assertEqual(200, code)
        self.assertEqual(["turn.cancelled"],
                         [event["kind"] for event in rest["data"]["events"]])

    def test_v1_native_stream_persists_partial_draft_before_completion(self):
        emitted, release = threading.Event(), threading.Event()

        class StreamingFake:
            supports_cancellation = True

            def __call__(self, system, messages, *, cancel_event, on_delta):
                on_delta("部分结果")
                emitted.set()
                release.wait(3)
                on_delta("已完成")
                return "部分结果已完成"

        self._start(chat_llm=StreamingFake())
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "流式测试", "harness": "chat"})
        session_id = created["data"]["session_id"]
        code, response = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "stream-http-turn-0001",
            "text": "流式回答"})
        self.assertEqual(202, code)
        self.assertTrue(emitted.wait(1))
        _, live = self._get("/api/chat")
        running = next(turn for turn in live["turns"] if turn["status"] == "running")
        self.assertEqual("部分结果", running["assistant_draft"])
        release.set()
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        _, done = self._get("/api/chat")
        self.assertEqual("completed", done["turns"][0]["status"])
        self.assertEqual("部分结果已完成", done["messages"][-1]["text"])

    def test_agent_scheduler_enforces_slots_and_can_cancel_queued_job(self):
        lock, two_running, release = threading.Lock(), threading.Event(), threading.Event()
        calls = []

        def slow_llm(system, messages):
            with lock:
                calls.append(messages[-1]["content"])
                if len(calls) == 2:
                    two_running.set()
            release.wait(3)
            return "完成"

        self._start(chat_llm=slow_llm)
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "调度测试", "harness": "chat"})
        session_id = created["data"]["session_id"]
        turns = []
        for index in range(3):
            code, response = self._post(f"/api/v1/sessions/{session_id}/turns", {
                "schema_version": "1.0", "idempotency_key": f"scheduler-http-{index:04d}",
                "text": f"任务 {index}"})
            self.assertEqual(202, code)
            turns.append(response["data"]["turn"]["turn_id"])
        self.assertTrue(two_running.wait(1))
        _, live = self._get("/api/chat")
        self.assertEqual(2, sum(job["status"] == "running" for job in live["jobs"]))
        queued = next(job for job in live["jobs"] if job["status"] == "queued")
        self.assertEqual(turns[2], queued["turn_id"])
        code, _ = self._post(f"/api/v1/turns/{turns[2]}/cancel", {
            "schema_version": "1.0", "reason": "取消排队任务"})
        self.assertEqual(200, code)
        release.set()
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        self.assertEqual(2, len(calls), "排队时取消的任务不得调用模型")
        _, final = self._get("/api/chat")
        self.assertEqual(["succeeded", "succeeded", "cancelled"],
                         [job["status"] for job in final["jobs"]])

    def test_restart_resumes_durable_queued_job_once(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name) / "project")
        store = AgentSessionStore(self.root)
        try:
            session = store.create_session("恢复队列", "chat", "test-model")
            store.set_default_session(session["session_id"])
            turn, _ = store.start_turn(
                session["session_id"], "重启后继续", "restart-queued-turn-0001",
                actor="local_user", trace_id="trc_restartqueue", enqueue=True)
            job = store.agent_job_for_turn(turn["turn_id"])
        finally:
            store.close()
        calls = []
        self.server = Workstation(
            ("127.0.0.1", 0), self.root,
            chat_llm=lambda system, messages: calls.append(messages) or "恢复完成")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as response:
            self.token = json.loads(response.read())["token"]
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        reopened = AgentSessionStore(self.root)
        try:
            self.assertEqual("completed", reopened.get_turn(turn["turn_id"])["status"])
            self.assertEqual("succeeded", reopened.get_agent_job(job["job_id"])["status"])
        finally:
            reopened.close()
        self.assertEqual(1, len(calls))

    def test_agent_job_persists_provider_token_usage(self):
        class MeteredText(str):
            usage = {"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29}

        self._start(chat_llm=lambda system, messages: MeteredText("计量回复"))
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "用量测试", "harness": "chat"})
        session_id = created["data"]["session_id"]
        code, _ = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "usage-http-turn-0001",
            "text": "记录用量"})
        self.assertEqual(202, code)
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        _, chat = self._get("/api/chat")
        job = chat["jobs"][0]
        self.assertEqual("succeeded", job["status"])
        self.assertEqual((21, 8, 29), (job["prompt_tokens"],
                         job["completion_tokens"], job["total_tokens"]))
        self.assertEqual({"jobs": 1, "prompt_tokens": 21,
                          "completion_tokens": 8, "total_tokens": 29,
                          "reserved_tokens": 0, "token_budget": None,
                          "remaining_tokens": None, "cost_micros": 0,
                          "reserved_cost_micros": 0, "cost_budget_micros": None,
                          "remaining_cost_micros": None, "currency": None},
                         chat["usage"])

    def test_failed_turn_retry_creates_child_and_does_not_duplicate_on_replay(self):
        calls = []

        def flaky_llm(system, messages):
            calls.append(messages)
            if len(calls) == 1:
                raise RuntimeError("临时失败")
            return "重试成功"

        self._start(chat_llm=flaky_llm)
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "重试测试", "harness": "chat"})
        session_id = created["data"]["session_id"]
        _, started = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "retry-source-http-0001",
            "text": "需要重试"})
        source_id = started["data"]["turn"]["turn_id"]
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        request = {"schema_version": "1.0",
                   "idempotency_key": "retry-child-http-0001"}
        code, retried = self._post(f"/api/v1/turns/{source_id}/retry", request)
        self.assertEqual(202, code)
        child_id = retried["data"]["turn"]["turn_id"]
        self.assertEqual(source_id, retried["data"]["turn"]["parent_turn_id"])
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        code, replay = self._post(f"/api/v1/turns/{source_id}/retry", request)
        self.assertEqual(202, code)
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual(child_id, replay["data"]["turn"]["turn_id"])
        self.assertEqual(2, len(calls))
        _, chat = self._get("/api/chat")
        self.assertEqual(["failed", "completed"],
                         [turn["status"] for turn in chat["turns"]])

    def test_running_turn_can_atomically_steer_to_new_instruction(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def directional_llm(system, messages):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                entered.set()
                release.wait(3)
                return "旧方向迟到结果"
            return "新方向结果"

        self._start(chat_llm=directional_llm)
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "转向测试", "harness": "chat"})
        session_id = created["data"]["session_id"]
        _, started = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "steer-source-http-0001",
            "text": "旧方向"})
        source_id = started["data"]["turn"]["turn_id"]
        self.assertTrue(entered.wait(1))
        request = {"schema_version": "1.0",
                   "idempotency_key": "steer-child-http-0001", "text": "新方向"}
        code, steered = self._post(f"/api/v1/turns/{source_id}/steer", request)
        self.assertEqual(202, code)
        child_id = steered["data"]["turn"]["turn_id"]
        self.assertEqual(source_id, steered["data"]["turn"]["parent_turn_id"])
        release.set()
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        code, replay = self._post(f"/api/v1/turns/{source_id}/steer", request)
        self.assertEqual(202, code)
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual(child_id, replay["data"]["turn"]["turn_id"])
        self.assertEqual(["旧方向", "新方向"], calls)
        _, chat = self._get("/api/chat")
        self.assertEqual(["interrupted", "completed"],
                         [turn["status"] for turn in chat["turns"]])
        self.assertEqual("Steered", chat["turns"][0]["error"]["type"])
        self.assertEqual(["cancelled", "succeeded"],
                         [job["status"] for job in chat["jobs"]])

    def test_session_budget_rejects_before_turn_or_model_call(self):
        calls = []
        self._start(chat_llm=lambda system, messages: calls.append(messages) or "回复")
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "预算测试", "harness": "chat"})
        session_id = created["data"]["session_id"]
        code, budget = self._post(f"/api/v1/sessions/{session_id}/budget", {
            "schema_version": "1.0", "token_budget": 1})
        self.assertEqual(200, code)
        self.assertEqual(1, budget["data"]["usage"]["remaining_tokens"])
        code, rejected = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "budget-http-turn-0001",
            "text": "不应调用模型"})
        self.assertEqual(409, code)
        self.assertEqual("budget_exhausted", rejected["error"]["code"])
        self.assertEqual([], calls)
        _, chat = self._get("/api/chat")
        self.assertEqual([], chat["turns"])
        self.assertEqual([], chat["jobs"])

    def test_versioned_pricing_api_prices_job_from_snapshot(self):
        class MeteredText(str):
            usage = {"prompt_tokens": 100, "completion_tokens": 50,
                     "total_tokens": 150}

        self._start(chat_llm=lambda system, messages: MeteredText("计费回复"))
        self.server.chat_model = "deepseek-flash"
        code, catalog = self._post("/api/v1/pricing-catalogs", {
            "schema_version": "1.0", "name": "测试合同", "currency": "CNY",
            "prices": [{"model": "deepseek-flash",
                        "prompt_micros_per_million": 2_000_000,
                        "completion_micros_per_million": 4_000_000}]})
        self.assertEqual(201, code)
        catalog_id = catalog["data"]["catalog_id"]
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "价格测试", "harness": "chat"})
        session_id = created["data"]["session_id"]
        code, _ = self._post(f"/api/v1/sessions/{session_id}/pricing", {
            "schema_version": "1.0", "catalog_id": catalog_id})
        self.assertEqual(200, code)
        code, _ = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "priced-http-turn-0001",
            "text": "计算费用"})
        self.assertEqual(202, code)
        self.assertEqual(0, self.server.wait_for_jobs(timeout=3))
        _, chat = self._get("/api/chat")
        self.assertEqual("CNY", chat["usage"]["currency"])
        self.assertEqual(400, chat["usage"]["cost_micros"])
        self.assertEqual(catalog_id, chat["jobs"][0]["price_catalog_id"])

    def test_cost_budget_rejects_before_model_call(self):
        calls = []
        self._start(chat_llm=lambda system, messages: calls.append(messages) or "回复")
        self.server.chat_model = "deepseek-flash"
        _, catalog = self._post("/api/v1/pricing-catalogs", {
            "schema_version": "1.0", "name": "严格费用", "currency": "CNY",
            "prices": [{"model": "deepseek-flash",
                        "prompt_micros_per_million": 2_000_000,
                        "completion_micros_per_million": 4_000_000}]})
        _, created = self._post("/api/v1/sessions", {
            "schema_version": "1.0", "title": "费用门禁", "harness": "chat"})
        session_id = created["data"]["session_id"]
        self._post(f"/api/v1/sessions/{session_id}/pricing", {
            "schema_version": "1.0", "catalog_id": catalog["data"]["catalog_id"]})
        code, budget = self._post(f"/api/v1/sessions/{session_id}/cost-budget", {
            "schema_version": "1.0", "cost_budget_micros": 1})
        self.assertEqual(200, code)
        self.assertEqual(1, budget["data"]["usage"]["remaining_cost_micros"])
        code, rejected = self._post(f"/api/v1/sessions/{session_id}/turns", {
            "schema_version": "1.0", "idempotency_key": "cost-budget-http-0001",
            "text": "不得调用"})
        self.assertEqual(409, code)
        self.assertEqual("budget_exhausted", rejected["error"]["code"])
        self.assertEqual([], calls)
        _, chat = self._get("/api/chat")
        self.assertEqual([], chat["turns"])

    def test_model_failure_records_error_entry_honestly(self):
        def failing_llm(system, messages):
            raise RuntimeError("连接超时")

        self._start(chat_llm=failing_llm)
        code, body = self._post("/api/chat", {"text": "会失败的消息"})
        self.assertEqual(200, code)
        self.assertEqual(2, len(body["messages"]))
        self.assertEqual("user", body["messages"][0]["role"])
        self.assertEqual("会失败的消息", body["messages"][0]["text"])
        failed = body["messages"][1]
        self.assertEqual("assistant", failed["role"])
        self.assertIn("error", failed)
        self.assertEqual("RuntimeError", failed["error"]["type"])
        self.assertIn("连接超时", failed["error"]["message"])
        # error 条目不回传给下一次对话的模型历史。
        self.assertEqual(2, len(self._stored_messages(self.root)))

    def test_history_survives_restart(self):
        self._start(chat_llm=lambda system, messages: "回复甲")
        self._post("/api/chat", {"text": "第一条"})
        self._post("/api/chat", {"text": "第二条"})
        self._stop()
        # 模拟重启：同一项目目录再起一个 Workstation（注入新 fake）。
        self.server = Workstation(("127.0.0.1", 0), self.root,
                                  chat_llm=lambda system, messages: "回复乙")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as r:
            self.token = json.loads(r.read())["token"]
        code, body = self._get("/api/chat")
        self.assertEqual(200, code)
        self.assertEqual(["user", "assistant", "user", "assistant"],
                         [m["role"] for m in body["messages"]])
        self.assertEqual("第一条", body["messages"][0]["text"])
        self.assertEqual("回复甲", body["messages"][1]["text"])
        # 重启后继续对话：新回复接在既有历史之后（真实落盘，不重开对话）。
        code, body = self._post("/api/chat", {"text": "第三条"})
        self.assertEqual(200, code)
        self.assertEqual(6, len(body["messages"]))
        self.assertEqual("第三条", body["messages"][4]["text"])
        self.assertEqual("回复乙", body["messages"][5]["text"])

    def test_roots_isolate_chat_history(self):
        extra = None  # 在 _start 里随构造登记（额外根只能显式登记，无 HTTP register op）
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.tmp.name) / "project")
        extra = Path(self.tmp.name) / "ws-b"
        extra.mkdir(parents=True, exist_ok=True)
        self.server = Workstation(("127.0.0.1", 0), self.root, workspace_roots=(extra,),
                                  chat_llm=lambda system, messages: "根内回复")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urllib.request.urlopen(self.base + "/api/session", timeout=3) as r:
            self.token = json.loads(r.read())["token"]
        self._post("/api/chat", {"text": "主根的消息"})
        # 切到额外根：chat 为空（该根没有对话记录），再发消息落在该根。
        code, ws = self._get("/api/workspace")
        self.assertEqual(200, code)
        other_wid = next(r["wid"] for r in ws["roots"]
                         if r["wid"] != ws["current_root"])
        code, _ = self._post("/api/workspace", {"op": "workspace_switch",
                                                "wid": other_wid})
        self.assertEqual(200, code)
        code, body = self._get("/api/chat")
        self.assertEqual(200, code)
        self.assertEqual([], body["messages"])
        code, body = self._post("/api/chat", {"text": "另一根的消息"})
        self.assertEqual(200, code)
        self.assertEqual("另一根的消息", body["messages"][0]["text"])
        # 两根记录互不影响。
        self.assertEqual(2, len(self._stored_messages(self.root)))
        self.assertEqual(2, len(self._stored_messages(extra)))

    # -- 请求校验 --------------------------------------------------------------

    def test_rejects_empty_and_malformed(self):
        self._start(chat_llm=lambda system, messages: "x")
        code, body = self._post("/api/chat", {"text": "   "})
        self.assertEqual(409, code)
        code, body = self._post("/api/chat", {"text": ""})
        self.assertEqual(409, code)
        code, body = self._post("/api/chat", {"text": "hi", "extra": 1})
        self.assertEqual(409, code)
        code, body = self._post("/api/chat", {})
        self.assertEqual(409, code)
        # 校验失败不落盘。
        self.assertFalse((self.root / ".popper" / "chat.json").is_file())


class ChatClientGuardTests(unittest.TestCase):
    """make_chat_client 安全约束：地址与 key 的 fail-fast 校验。"""

    def test_rejects_non_https_remote(self):
        with self.assertRaises(ProtocolError):
            make_chat_client("http://api.example.com/v1", "m")

    def test_allows_local_http_and_requires_key(self):
        import os
        old = os.environ.pop("POPPER_API_KEY", None)
        try:
            with self.assertRaises(ProtocolError):
                make_chat_client("http://127.0.0.1:8000/v1", "m")
            os.environ["POPPER_API_KEY"] = "sk-test"
            client = make_chat_client("http://127.0.0.1:8000/v1", " m ")
            self.assertTrue(callable(client))
        finally:
            if old is None:
                os.environ.pop("POPPER_API_KEY", None)
            else:
                os.environ["POPPER_API_KEY"] = old

    def test_rejects_url_with_credentials_or_query(self):
        import os
        os.environ.setdefault("POPPER_API_KEY", "sk-test")
        try:
            with self.assertRaises(ProtocolError):
                make_chat_client("https://user:pass@api.example.com/v1", "m")
            with self.assertRaises(ProtocolError):
                make_chat_client("https://api.example.com/v1?key=1", "m")
            with self.assertRaises(ProtocolError):
                make_chat_client("https://api.example.com/v1", " ")
        finally:
            os.environ.pop("POPPER_API_KEY", None)

    def test_native_openai_tool_call_is_parsed_and_tools_are_declared(self):
        import os
        old = os.environ.get("POPPER_API_KEY")
        os.environ["POPPER_API_KEY"] = "sk-test"
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                first = {"choices": [{"delta": {
                    "content": "准备创建任务",
                    "tool_calls": [{"index": 0, "type": "function", "function": {
                        "name": "workspace_task_create", "arguments": ""}}],
                }}]}
                second = {"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "function": {"arguments": json.dumps({
                        "expected_version": 1, "fid": "f1", "title": "任务"})}}]}}]}
                yield ("data: " + json.dumps(first) + "\n\n").encode()
                yield ("data: " + json.dumps(second) + "\n\n").encode()
                yield ("data: " + json.dumps({"choices": [], "usage": {
                    "prompt_tokens": 10, "completion_tokens": 4,
                    "total_tokens": 14}}) + "\n\n").encode()
                yield b"data: [DONE]\n\n"

            def readline(self, size=-1):
                import io
                if not hasattr(self, '_stream'):
                    self._stream = io.BytesIO(b''.join(iter(self)))
                return self._stream.readline(size)

            def close(self):
                pass

        class Opener:
            def open(self, request, timeout):
                seen["body"] = json.loads(request.data)
                return Response()

        try:
            with patch("popper.chat.urllib.request.build_opener", return_value=Opener()):
                client = make_chat_client("http://127.0.0.1:8000/v1", "deepseek-flash")
                result = client("system", [{"role": "user", "content": "创建任务"}])
            self.assertEqual("workspace_task_create", result["tool_calls"][0]["name"])
            self.assertEqual("f1", result["tool_calls"][0]["arguments"]["fid"])
            declared = [item["function"]["name"] for item in seen["body"]["tools"]]
            self.assertEqual(["context_tool_result", "context_source", "research_study_preview", "research_study_action",
                              "research_study_job", "research_draft_preview",
                              "research_draft_create", "research_create_design", "research_create_hypothesis",
                              "research_execute_design",
                              "research_freeze_design", "research_status",
                              "research_inspect_design", "research_inspect_claim",
                              "research_inspect_observation",
                              "workspace_task_create",
                              "workspace_task_toggle"], declared)
            with patch("popper.chat.urllib.request.build_opener", return_value=Opener()):
                trusted_client = make_chat_client(
                    "http://127.0.0.1:8000/v1", "deepseek-flash",
                    enable_file_tools=True)
                trusted_client("system", [{"role": "user", "content": "读取项目代码"}])
            trusted_declared = [item["function"]["name"] for item in seen["body"]["tools"]]
            self.assertEqual(declared + ["workspace_file_list", "workspace_file_read",
                                          "workspace_file_replace", "research_revision_preview",
                                          "research_revision_commit", "research_revision_run",
                                          "research_revision_job"], trusted_declared)
            self.assertEqual("auto", seen["body"]["tool_choice"])
            self.assertTrue(seen["body"]["stream"])
            self.assertEqual({"include_usage": True}, seen["body"]["stream_options"])
            self.assertEqual(14, result.usage["total_tokens"])
            self.assertEqual("openai_compatible", client.descriptor.name)
            self.assertEqual({"cooperative_cancel", "native_tools", "provider_usage",
                              "streaming_text", "text"},
                             set(client.descriptor.capabilities))
        finally:
            if old is None:
                os.environ.pop("POPPER_API_KEY", None)
            else:
                os.environ["POPPER_API_KEY"] = old

    def test_streaming_client_emits_text_deltas_and_honours_pre_cancel(self):
        import os
        old = os.environ.get("POPPER_API_KEY")
        os.environ["POPPER_API_KEY"] = "sk-test"

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self):
                for piece in ("研究", "完成"):
                    yield ("data: " + json.dumps({"choices": [{
                        "delta": {"content": piece}}]}) + "\n\n").encode()
                yield b"data: [DONE]\n\n"
            def readline(self, size=-1):
                import io
                if not hasattr(self, '_stream'):
                    self._stream = io.BytesIO(b''.join(iter(self)))
                return self._stream.readline(size)
            def close(self): pass

        class Opener:
            def open(self, request, timeout): return Response()

        try:
            with patch("popper.chat.urllib.request.build_opener", return_value=Opener()):
                client = make_chat_client("http://127.0.0.1:8000/v1", "model")
                deltas = []
                self.assertEqual("研究完成", client(
                    "system", [], on_delta=deltas.append))
                self.assertEqual(["研究", "完成"], deltas)
                cancelled = threading.Event()
                cancelled.set()
                with self.assertRaisesRegex(ProtocolError, "取消"):
                    client("system", [], cancel_event=cancelled)
        finally:
            if old is None:
                os.environ.pop("POPPER_API_KEY", None)
            else:
                os.environ["POPPER_API_KEY"] = old

    def test_streaming_client_cancel_closes_inflight_http_response(self):
        import os
        old = os.environ.get("POPPER_API_KEY")
        os.environ["POPPER_API_KEY"] = "sk-test"
        opened, closed = threading.Event(), threading.Event()

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def readline(self, size=-1):
                opened.set()
                closed.wait(3)
                raise OSError("response closed")
            def close(self): closed.set()

        class Opener:
            def open(self, request, timeout): return Response()

        try:
            with patch("popper.chat.urllib.request.build_opener", return_value=Opener()):
                client = make_chat_client("http://127.0.0.1:8000/v1", "model")
                cancel, errors = threading.Event(), []

                def invoke():
                    try:
                        client("system", [], cancel_event=cancel)
                    except Exception as error:
                        errors.append(error)

                thread = threading.Thread(target=invoke)
                thread.start()
                self.assertTrue(opened.wait(1))
                cancel.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertTrue(closed.is_set())
                self.assertEqual("ChatCancelled", type(errors[0]).__name__)
        finally:
            if old is None:
                os.environ.pop("POPPER_API_KEY", None)
            else:
                os.environ["POPPER_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()
