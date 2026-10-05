import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, initialize
from popper.api_contracts import new_trace_id
from popper.research.controller import ResearchController
from popper.research.readout import read_research_state
from popper.research.revisions import RevisionStore
from popper.server import Workstation, validate_agent_tool_arguments
from popper.sessions import AgentSessionStore, ToolCallConflict
from popper.workspace_files import ProjectFiles


class AgentRevisionToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project, self.run = self.root / "project", self.root / "research"
        example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(example / name, self.project / name)
        runpy.run_path(str(example / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        ResearchController.initialize(self.project, self.run)
        state = read_research_state(self.run)
        h = next(row for row in state["hypotheses"] if row["status"] == "untested" and not row["hypothesis_id"].startswith("H-control-"))
        d = next(row for row in state["designs"] if row["hypothesis_id"] == h["hypothesis_id"])
        source = ProjectFiles(self.project).read("model.py")
        self.args = {"study_id": state["manifest"]["study_id"], "expected_state_version": state["state_version"],
                     "hypothesis_id": h["hypothesis_id"], "design_id": d["design_id"],
                     "path": "model.py", "expected_sha256": source["sha256"],
                     "replacement": source["content"].replace("def main", "# Agent implementation\ndef main")}
        self.responses = []
        self.inputs = []
        def fake(system, messages):
            self.inputs.append(messages)
            return self.responses.pop(0) if self.responses else "核对真实工具结果后停止。"
        self.ws = Workstation(("127.0.0.1", 0), self.project, trusted_local=True,
                              research_runs=[self.run], chat_llm=fake)
        self.session = self.ws.create_agent_session("Revision test")

    def tearDown(self):
        self.ws.wait_for_jobs(timeout=20)
        self.ws.server_close()
        self.temp.cleanup()

    def propose(self, tool, args):
        self.ws.wait_for_jobs(timeout=20)
        self.responses.append({"text": "实验提案", "tool_calls": [{"name": tool.replace(".", "_"), "arguments": args}]})
        result = self.ws.send_session_turn(self.session["session_id"], "提出下一步", f"revision-turn-{len(self.inputs):04d}", new_trace_id())
        store = AgentSessionStore(self.project)
        try:
            return next(call for call in store.tool_calls(self.session["session_id"])
                        if call["turn_id"] == result["turn"]["turn_id"])
        finally:
            store.close()

    def approve_execute(self, call):
        self.ws.decide_agent_tool(call["call_id"], "approve", "用户核对差异和作用域")
        return self.ws.execute_agent_tool(call["call_id"])

    def test_native_proposal_two_approvals_author_and_snapshot(self):
        preview_call = self.propose("research.revision_preview", self.args)
        with self.assertRaises(ToolCallConflict):
            self.ws.execute_agent_tool(preview_call["call_id"])
        preview = self.approve_execute(preview_call)
        self.assertEqual("completed", preview["status"])
        create_args = {**self.args, "approval_sha256": preview["result"]["proposal_sha256"]}
        create_call = self.propose("research.revision_create", create_args)
        public = self.ws._agent_tool_public(create_call)
        self.assertTrue(public["preview"]["ok"])
        self.assertIn("Agent implementation", public["preview"]["revision_preview"]["diff"])
        created = self.approve_execute(create_call)
        self.assertEqual("completed", created["status"], created.get("error"))
        revision_id = created["result"]["revision_id"]
        revision = RevisionStore(self.run / "revisions").verify(revision_id)
        self.assertEqual("agent", revision["identity"]["actor"])
        self.assertNotIn("Agent implementation", ProjectFiles(self.project).read("model.py")["content"])
        self.assertEqual([], read_research_state(self.run)["observations"])
        args = {"study_id": self.args["study_id"], "revision_id": revision_id,
                "manifest_sha256": created["result"]["manifest_sha256"],
                "expected_state_version": read_research_state(self.run)["state_version"]}
        execution = self.propose("research.revision_execute", args)
        with self.assertRaises(ToolCallConflict):
            self.ws.execute_agent_tool(execution["call_id"])
        with patch.object(self.ws, "_start_revision_job"):
            queued = self.approve_execute(execution)
        self.assertEqual("completed", queued["status"], queued.get("error"))
        self.assertEqual("enqueued_not_completed", queued["result"]["status"])
        self.assertEqual("queued", queued["result"]["job"]["status"])
        self.assertEqual([], read_research_state(self.run)["observations"])
        # Terminal tool messages, not fabricated user messages, enter continuation.
        self.ws.wait_for_jobs(timeout=20)
        self.assertTrue(any(any(m["role"] == "tool" for m in msgs) for msgs in self.inputs))

    def test_rejected_digest_preserves_redacted_reply_without_tools(self):
        original = ProjectFiles(self.project).read("model.py")["content"]
        args = {**self.args, "approval_sha256": "not-a-digest",
                "replacement": '{"api_key":"secret-value"}'}
        self.responses.append({"text": "request creation", "tool_calls": [
            {"name": "research_revision_create", "arguments": args}]})
        result = self.ws.send_session_turn(self.session["session_id"], "review", "invalid-digest-audit", new_trace_id())
        turn = result["turn"]
        self.assertEqual("failed", turn["status"])
        self.assertIn("invalid Revision digest", turn["error"]["message"])
        evidence = turn["error"]["rejected_reply"]
        self.assertTrue(evidence["retained"])
        self.assertEqual("not-a-digest", evidence["reply"]["tool_calls"][0]["arguments"]["approval_sha256"])
        self.assertNotIn("secret-value", str(evidence))
        self.assertEqual("secret-value", __import__('json').loads(args["replacement"])["api_key"])
        with __import__('contextlib').closing(AgentSessionStore.readonly(self.project)) as store:
            self.assertEqual([], store.tool_calls(self.session["session_id"]))
        self.assertEqual(original, ProjectFiles(self.project).read("model.py")["content"])

    def test_oversized_rejected_reply_retains_digest_not_truncated_summary(self):
        self.responses.append({"text": "x" * 70000, "tool_calls": [
            {"name": "not_registered", "arguments": {}}]})
        result = self.ws.send_session_turn(self.session["session_id"], "review", "oversized-reply-audit", new_trace_id())
        evidence = result["turn"]["error"]["rejected_reply"]
        self.assertFalse(evidence["retained"])
        self.assertGreater(evidence["bytes"], 65536)
        self.assertNotIn("reply", evidence)
        self.assertEqual(64, len(evidence["sha256"]))

    def test_changed_digest_rejects_creation_and_preserves_proposal(self):
        call = self.propose("research.revision_create", {**self.args, "approval_sha256": "0" * 64})
        self.assertFalse(self.ws._agent_tool_public(call)["preview"]["ok"])
        completed = self.approve_execute(call)
        self.assertEqual("failed", completed["status"])
        self.assertEqual(self.args["replacement"], completed["arguments"]["replacement"])
        self.assertEqual([], list((self.run / "revisions").glob("REV-*")))

    def test_actual_revision_job_observation_and_receipt_reconciliation(self):
        preview = self.approve_execute(self.propose("research.revision_preview", self.args))
        call = self.propose("research.revision_create", {
            **self.args, "approval_sha256": preview["result"]["proposal_sha256"]})
        created = self.approve_execute(call)
        from popper.research.agent_revisions import reconcile
        self.assertEqual(created["result"], reconcile(self.ws, call, self.project))
        # A repeated tool invocation must return the persisted immutable receipt.
        with self.assertRaises(ToolCallConflict):
            self.ws.execute_agent_tool(call["call_id"])
        self.assertEqual(created["result"], reconcile(self.ws, call, self.project))
        args = {"study_id": self.args["study_id"], "revision_id": created["result"]["revision_id"],
                "manifest_sha256": created["result"]["manifest_sha256"],
                "expected_state_version": read_research_state(self.run)["state_version"]}
        execution = self.propose("research.revision_execute", args)
        queued = self.approve_execute(execution)
        self.ws.wait_for_jobs(timeout=30)
        job_id = queued["result"]["job"]["job_id"]
        queried = self.approve_execute(self.propose("research.revision_job", {
            "study_id": args["study_id"], "job_id": job_id}))
        self.assertEqual("completed", queried["status"], queried.get("error"))
        self.assertEqual("succeeded", queried["result"]["job"]["status"], queried["result"])
        self.assertTrue(queried["result"]["observations"])
        self.ws.wait_for_jobs(timeout=20)
        self.assertTrue(any(any(m["role"] == "tool" and job_id in str(m) for m in msgs)
                            for msgs in self.inputs))

    def test_validation_does_not_accept_boolean_version_or_extra_cost(self):
        for args in ({**self.args, "expected_state_version": True}, {**self.args, "cost": 0}):
            with self.assertRaises(ProtocolError):
                validate_agent_tool_arguments("research.revision_preview", args)

    def test_failed_parent_repair_separate_approval_and_actual_observation(self):
        from popper.core import file_hash
        original = ProjectFiles(self.project).read("model.py")["content"]
        broken = original.replace('def main():', 'def main():\n    print("synthetic changed statement")\n    raise RuntimeError("fixture failure")')
        self.args["replacement"] = broken
        preview = self.approve_execute(self.propose("research.revision_preview", self.args))
        created = self.approve_execute(self.propose("research.revision_create", {
            **self.args, "approval_sha256": preview["result"]["proposal_sha256"]}))
        parent_id = created["result"]["revision_id"]
        queued = self.approve_execute(self.propose("research.revision_execute", {
            "study_id": self.args["study_id"], "revision_id": parent_id,
            "manifest_sha256": created["result"]["manifest_sha256"],
            "expected_state_version": read_research_state(self.run)["state_version"]}))
        self.ws.wait_for_jobs(timeout=30)
        self.assertEqual("failed", self.ws.revision_job_payload(queued["result"]["job"]["job_id"])["job"]["status"])
        parent_job_id = queued["result"]["job"]["job_id"]
        failed_result = self.approve_execute(self.propose("research.revision_job", {
            "study_id": self.args["study_id"], "job_id": parent_job_id}))
        self.assertEqual("completed", failed_result["status"], failed_result.get("error"))
        self.assertEqual([], failed_result["result"]["observations"])
        self.assertTrue(failed_result["result"]["executions"])
        self.assertIn('fixture failure', str(failed_result["result"]["executions"]))
        repaired = broken.replace('    raise RuntimeError("fixture failure")\n', '')
        spec = {**self.args, "replacement": repaired, "parent_revision_id": parent_id,
                "expected_sha256": file_hash(self.run / "revisions" / parent_id / "code/model.py"),
                "expected_state_version": read_research_state(self.run)["state_version"]}
        checked = self.approve_execute(self.propose("research.revision_preview", spec))
        self.assertEqual("completed", checked["status"], checked.get("error"))
        fixed = self.approve_execute(self.propose("research.revision_create", {
            **spec, "approval_sha256": checked["result"]["proposal_sha256"]}))
        self.assertEqual("completed", fixed["status"], fixed.get("error"))
        child_id = fixed["result"]["revision_id"]
        self.assertEqual(parent_id, RevisionStore(self.run / "revisions").verify(child_id)["identity"]["parent_revision_id"])
        execution = self.propose("research.revision_execute", {
            "study_id": self.args["study_id"], "revision_id": child_id,
            "manifest_sha256": fixed["result"]["manifest_sha256"],
            "expected_state_version": read_research_state(self.run)["state_version"]})
        with self.assertRaises(ToolCallConflict):
            self.ws.execute_agent_tool(execution["call_id"])
        final = self.approve_execute(execution)
        self.assertEqual("completed", final["status"], final.get("error"))
        self.ws.wait_for_jobs(timeout=30)
        observed = self.approve_execute(self.propose("research.revision_job", {
            "study_id": self.args["study_id"], "job_id": final["result"]["job"]["job_id"]}))
        self.assertEqual("succeeded", observed["result"]["job"]["status"], observed)
        self.assertTrue(observed["result"]["observations"])
        # Querying the failed parent after its child succeeds must not leak the
        # child's Observation into the parent's result just because design matches.
        parent_again = self.approve_execute(self.propose("research.revision_job", {
            "study_id": self.args["study_id"], "job_id": parent_job_id}))
        self.assertEqual([], parent_again["result"]["observations"])
        self.assertTrue(parent_again["result"]["executions"])
        # A successful child is not a permissible repair parent.
        rejected = {**spec, "parent_revision_id": child_id,
                    "expected_state_version": read_research_state(self.run)["state_version"]}
        call = self.propose("research.revision_preview", rejected)
        self.assertFalse(self.ws._agent_tool_public(call)["preview"]["ok"])


if __name__ == "__main__":
    unittest.main()
