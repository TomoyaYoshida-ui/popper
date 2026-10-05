"""Campaign reservations exercise the real Workstation dispatch boundary."""
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path

from popper.api_contracts import new_trace_id
from popper.chat import ChatText, ChatToolResult
from popper.core import digest, initialize
from popper.research.agent_evaluation_budget import AgentEvaluationBudget
from popper.server import Workstation
from popper.sessions import AgentSessionStore
from popper.workspace_files import ProjectFiles
from popper import task_context


class CampaignDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(example / name, self.project / name)
        runpy.run_path(str(example / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        self.calls, self.replies = [], []

        def fake(system, messages):
            self.calls.append((system, messages))
            result = self.replies.pop(0) if self.replies else ChatText("停止", {"total_tokens": 7})
            if isinstance(result, Exception):
                raise result
            return result

        self.path = self.root / "campaign.sqlite"
        self.ws = Workstation(("127.0.0.1", 0), self.project, chat_llm=fake,
                              evaluation_budget_path=self.path)
        self.session = self.ws.create_agent_session("campaign")
        self.counter = 0

    def tearDown(self):
        self.ws.wait_for_jobs(timeout=20)
        self.ws.server_close()
        self.temp.cleanup()

    def send(self):
        self.counter += 1
        return self.ws.send_session_turn(self.session["session_id"], "合成研究请求",
                                        f"campaign-message-{self.counter:04d}", new_trace_id())

    def status(self):
        ledger = AgentEvaluationBudget(self.path)
        try:
            return ledger.status()
        finally:
            ledger.close()

    def test_actual_dispatch_uses_persisted_reservation_snapshot(self):
        self.replies.append(ChatText("可核对的回复", {"total_tokens": 123}))
        result = self.send()
        self.assertEqual("completed", result["turn"]["status"])
        store = AgentSessionStore(self.project)
        ledger = AgentEvaluationBudget(self.path)
        try:
            snapshot = task_context.load_input(store, result["turn"]["turn_id"])
            row = ledger.connection.execute("SELECT * FROM requests").fetchone()
            self.assertEqual(digest(snapshot), row["sha256"])
            self.assertEqual(self.ws._reserve_context(snapshot), row["reserved"])
            self.assertEqual(snapshot["system"], self.calls[0][0])
            self.assertEqual(snapshot["messages"], self.calls[0][1])
            self.assertEqual(123, row["used"])
        finally:
            ledger.close()
            store.close()

    def test_unknown_usage_and_transport_failure_stop_further_dispatch(self):
        for reply in (ChatText("用量缺失"), RuntimeError("connection lost after dispatch")):
            with self.subTest(reply=type(reply).__name__):
                # Each ambiguous attempt belongs to an independent campaign.
                self.path = self.root / f"campaign-{self.counter}.sqlite"
                self.ws.evaluation_budget_path = self.path
                self.replies.append(reply)
                before = len(self.calls)
                self.assertEqual("failed", self.send()["turn"]["status"])
                self.assertEqual(1, self.status()["unresolved_requests"])
                self.assertEqual("failed", self.send()["turn"]["status"])
                self.assertEqual(before + 1, len(self.calls))

    def test_shared_ceiling_refuses_before_provider_io(self):
        ledger = AgentEvaluationBudget(self.path)
        ledger.reserve("other-arm", {}, 400000)
        ledger.settle("other-arm", 399999)
        ledger.close()
        result = self.send()
        self.assertEqual("failed", result["turn"]["status"])
        self.assertEqual([], self.calls)
        self.assertEqual(1, self.status()["request_count"])

    def test_invalid_tool_result_still_records_actual_provider_usage(self):
        self.replies.append(ChatToolResult({"text": "bad", "tool_calls": [
            {"name": "unregistered_tool", "arguments": {}}]}, {"total_tokens": 31}))
        result = self.send()
        self.assertEqual("completed", result["turn"]["status"])
        store = AgentSessionStore(self.project)
        try:
            calls = [call for call in store.tool_calls(self.session["session_id"])
                     if call["turn_id"] == result["turn"]["turn_id"]]
            self.assertEqual(1, len(calls))
            self.assertEqual("invalid", calls[0]["status"])
            self.assertFalse(calls[0]["error"]["executed"])
            self.assertIsNone(calls[0]["result"])
            with self.assertRaises(Exception):
                self.ws.decide_agent_tool(calls[0]["call_id"], "approve", "must not execute")
            with self.assertRaises(Exception):
                self.ws.execute_agent_tool(calls[0]["call_id"])
        finally:
            store.close()
        self.assertEqual(31, self.status()["used_tokens"])
        self.assertEqual(0, self.status()["unresolved_requests"])

    def test_crash_reservation_blocks_restart_without_redispatch(self):
        ledger = AgentEvaluationBudget(self.path)
        ledger.reserve("crashed-request", {}, 100)
        ledger.close()
        self.assertEqual("failed", self.send()["turn"]["status"])
        self.assertEqual([], self.calls)

    def test_model_file_tools_deny_reference_and_confirmation_inputs(self):
        files = ProjectFiles(self.project)
        visible = self.ws._agent_file_list(files)
        paths = {row["path"] for row in visible["files"]}
        self.assertIn("model.py", paths)
        for path in ("train.json", "dev.json", "test.json"):
            self.assertNotIn(path, paths)
            with self.assertRaises(Exception):
                self.ws._require_agent_file_disclosure(files, path)


if __name__ == "__main__":
    unittest.main()
