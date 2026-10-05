"""Approval/recovery guards using only synthetic stores and Workstation mocks."""
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from evaluation import approve_native_development_tool as runner
from popper.core import ProtocolError, canonical, read_json


class ProjectGuardTests(unittest.TestCase):
    def setUp(self):
        self.call = {"call_id": "tcl_test", "session_id": "session-a", "turn_id": "turn-a",
                     "status": "pending_approval", "proposed_by": "agent",
                     "tool_name": "research.status", "arguments": {"study_id": "S-test"}}
        self.calls = [self.call]
        self.jobs = {"session-a": [{"job_id": "old", "turn_id": "turn-a", "status": "succeeded"}],
                     "session-b": []}
        self.queued = []
        self.event_ok, self.turn_status = True, "completed"
        self.other_turn_running = False
        owner = self

        class Store:
            def close(self): pass
            def verify_turn_events(self): return {"ok": owner.event_ok}
            def verify_tool_events(self): return {"ok": owner.event_ok}
            def list_sessions(self): return [{"session_id": sid} for sid in owner.jobs]
            def agent_jobs(self, sid): return deepcopy(owner.jobs[sid])
            def turns(self, sid):
                return [{"turn_id": "turn-a" if sid == "session-a" else "turn-b", "session_id": sid,
                         "status": "running" if sid == "session-b" and owner.other_turn_running else "completed"}]
            def tool_calls(self, sid): return [deepcopy(c) for c in owner.calls if c["session_id"] == sid]
            def queued_agent_jobs(self): return deepcopy(owner.queued)
            def get_tool_call(self, _): return deepcopy(owner.call)
            def get_turn(self, _): return {"turn_id": "turn-a", "session_id": "session-a", "status": owner.turn_status}

        self.readonly = patch.object(runner.AgentSessionStore, "readonly", return_value=Store())
        self.readonly.start()
        self.addCleanup(self.readonly.stop)
        self.validator = patch.object(runner, "validate_agent_tool_arguments", side_effect=lambda tool, args: args)
        self.validator.start()
        self.addCleanup(self.validator.stop)

    def check(self):
        return runner._project_snapshot("synthetic", approval_call_id="tcl_test")

    def test_only_explicit_pending_call_allowed(self):
        self.assertEqual("tcl_test", self.check()["call"]["call_id"])
        for status in ("pending_approval", "approved", "running"):
            other = {**self.call, "call_id": "tcl_other", "session_id": "session-b", "status": status}
            self.calls.append(other)
            with self.subTest(status=status), self.assertRaisesRegex(ProtocolError, "another project tool"):
                self.check()
            self.calls.pop()

    def test_other_session_active_jobs_and_orphan_queued_blocked(self):
        for status in ("queued", "running"):
            self.jobs["session-b"] = [{"job_id": "other", "turn_id": "other", "status": status}]
            with self.subTest(status=status), self.assertRaisesRegex(ProtocolError, "active model"):
                self.check()
        self.jobs["session-b"] = []
        self.queued = [{"job_id": "orphan"}]
        with self.assertRaises(ProtocolError): self.check()
        self.queued = []
        self.other_turn_running = True
        with self.assertRaisesRegex(ProtocolError, "active model"):
            self.check()

    def test_invalid_author_tool_and_terminal_call_cannot_be_approved(self):
        for key, value in (("status", "invalid"), ("status", "completed"),
                           ("proposed_by", "operator"), ("tool_name", "research.study_action")):
            old = self.call[key]
            self.call[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ProtocolError): self.check()
            self.call[key] = old

    def test_same_turn_genuine_pending_sibling_permitted_not_approved(self):
        sibling = {**self.call, "call_id": "tcl_sibling"}
        invalid = {**self.call, "call_id": "tcl_invalid", "status": "invalid", "executed": False}
        self.calls.extend([sibling, invalid])
        self.assertEqual("tcl_test", self.check()["call"]["call_id"])
        self.assertEqual("pending_approval", sibling["status"])
        for key, value in (("status", "approved"), ("status", "running"), ("turn_id", "other-turn"),
                           ("session_id", "session-b"), ("proposed_by", "operator"), ("tool_name", "research.study_action")):
            old = sibling[key]
            sibling[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ProtocolError):
                self.check()
            sibling[key] = old
        with patch.object(runner, "validate_agent_tool_arguments", side_effect=ProtocolError("bad sibling arguments")):
            with self.assertRaisesRegex(ProtocolError, "bad sibling"):
                self.check()

    def test_event_integrity_and_incomplete_source_blocked(self):
        self.event_ok = False
        with self.assertRaisesRegex(ProtocolError, "integrity"): self.check()
        self.event_ok = True
        self.turn_status = "running"
        with self.assertRaisesRegex(ProtocolError, "source Turn"): self.check()


class ScopeGuardTests(unittest.TestCase):
    def setUp(self):
        self.state = {"ok": True, "study": {"study_id": "S-test"}, "state_version": 42}
        self.call = {"call_id": "tcl_test", "tool_name": "research.revision_preview",
                     "arguments": {"study_id": "S-test", "expected_state_version": 42}}
        self.patches = [patch.object(runner, "read_research_state", return_value=self.state),
                        patch.object(runner, "read_json", return_value={"study_id": "S-test"}),
                        patch.object(runner, "validate_agent_tool_arguments", side_effect=lambda tool, args: args)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_current_explicit_study_version_and_wrong_study_rejected(self):
        self.assertEqual(42, runner._check_tool_scope("project", "research", self.call)["state_version"])
        self.call["arguments"]["expected_state_version"] = 41
        with self.assertRaisesRegex(ProtocolError, "stale"):
            runner._check_tool_scope("project", "research", self.call)
        self.call["arguments"] = {"study_id": "S-other"}
        with self.assertRaisesRegex(ProtocolError, "Study"):
            runner._check_tool_scope("project", "research", self.call)

    def test_reference_uses_original_version_not_latest_replacement(self):
        for tool, source in (("research.revision_commit", {"arguments": {"expected_state_version": 41}}),
                             ("research.revision_run", {"result": {"state_version": 41}})):
            self.call["tool_name"] = tool
            self.call["arguments"] = {"study_id": "S-test"}
            with self.subTest(tool=tool), patch.object(runner.agent_revisions, "_reference_source", return_value=source):
                with self.assertRaisesRegex(ProtocolError, "stale"):
                    runner._check_tool_scope("project", "research", self.call)
                self.assertEqual(41, (source.get("arguments") or source.get("result"))["expected_state_version" if tool.endswith("commit") else "state_version"])

    def test_file_disclosure_still_uses_live_server_preview(self):
        self.call["tool_name"] = "workspace.file_read"
        self.assertIsNone(runner._check_tool_scope("project", "research", self.call))


class EligibilityTests(unittest.TestCase):
    def test_latest_full_arm_and_all_quiet_guards_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project, research = root / "popper_loop-1" / "project", root / "research"
            manifest = {"budget_ledger": "synthetic"}
            run = {"arm": "popper_loop", "project": str(project), "research": str(research)}
            snapshot = {"call": {"call_id": "tcl_test", "session_id": "session"}, "job_ids": [], "tool_calls": []}
            with patch.object(runner, "load_campaign", return_value=(manifest, run)), \
                    patch.object(runner, "_latest_revision", return_value=15), \
                    patch.object(runner, "_validate_arm", return_value=({"execution_snapshot_sha256": "frozen"}, {"exit_code": 0})) as gate, \
                    patch.object(runner, "_quiet_registries") as registry, \
                    patch.object(runner, "_project_snapshot", return_value=snapshot) as sessions, \
                    patch.object(runner, "_research_quiet") as research_guard, \
                    patch.object(runner, "_read_budget", return_value={"known": True}) as budget, \
                    patch.object(runner, "_check_tool_scope", return_value=None):
                result = runner.preview_native_development_approval(root, "popper_loop-1", "tcl_test")
                self.assertEqual(15, result["execution_revision"])
                gate.assert_called_once_with(root, manifest, 15)
                registry.assert_called_once_with(project, research)
                sessions.assert_called_once_with(project, approval_call_id="tcl_test")
                research_guard.assert_called_once_with(research)
                budget.assert_called_once_with("synthetic")
                gate.side_effect = ProtocolError("missing full current CI receipt")
                with self.assertRaisesRegex(ProtocolError, "current CI"):
                    runner.preview_native_development_approval(root, "popper_loop-1", "tcl_test", 14)
                budget.side_effect = ProtocolError("unknown usage")
                gate.side_effect = None
                with self.assertRaisesRegex(ProtocolError, "unknown usage"):
                    runner.preview_native_development_approval(root, "popper_loop-1", "tcl_test", 15)

    def test_stopped_loop3_never_approved(self):
        with patch.object(runner, "load_campaign") as load:
            with self.assertRaisesRegex(ProtocolError, "loop3 stopped"):
                runner.preview_native_development_approval("synthetic", "popper_loop-3", "tcl_test", 15)
            load.assert_not_called()

    def test_same_turn_sibling_study_is_checked_not_only_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            run = {"arm": "popper_loop", "project": str(root / "popper_loop-1" / "project"),
                   "research": str(root / "research")}
            target = {"call_id": "tcl_test", "session_id": "session", "status": "pending_approval"}
            sibling = {**target, "call_id": "tcl_sibling"}
            with patch.object(runner, "load_campaign", return_value=({"budget_ledger": "fixture"}, run)), \
                    patch.object(runner, "_validate_arm", return_value=({"execution_snapshot_sha256": "frozen"}, {})), \
                    patch.object(runner, "_quiet_registries"), patch.object(runner, "_research_quiet"), \
                    patch.object(runner, "_read_budget", return_value={}), \
                    patch.object(runner, "_project_snapshot", return_value={"call": target,
                        "tool_calls": [target, sibling], "job_ids": []}), \
                    patch.object(runner, "_check_tool_scope", side_effect=[{}, ProtocolError("sibling Study differs")]) as scope:
                with self.assertRaisesRegex(ProtocolError, "sibling Study"):
                    runner.preview_native_development_approval(root, "popper_loop-1", "tcl_test", 15)
                self.assertEqual(2, scope.call_count)

    def test_persistent_research_activity_read_only_no_database_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertFalse(runner._research_active(root))
            self.assertEqual([], list(root.iterdir()))
            for filename, table, column, value in (
                    ("revision-execution-jobs.sqlite", "revision_execution_jobs", "status", "running"),
                    ("design-execution-jobs.sqlite", "design_execution_jobs", "status", "queued"),
                    ("study-jobs.sqlite", "study_jobs", "snapshot", canonical({"status": "running"}))):
                with self.subTest(filename=filename):
                    with closing(sqlite3.connect(root / filename)) as conn, conn:
                        conn.execute(f"CREATE TABLE {table}({column} TEXT)")
                        conn.execute(f"INSERT INTO {table} VALUES(?)", (value,))
                    self.assertTrue(runner._research_active(root))
                    with closing(sqlite3.connect(root / filename)) as conn, conn:
                        conn.execute(f"UPDATE {table} SET {column}=?", (
                            canonical({"status": "succeeded"}) if column == "snapshot" else "succeeded",))
                    self.assertFalse(runner._research_active(root))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "popper_loop-1").mkdir()
        self.folder = self.root / "popper_loop-1" / "native-development-approvals"
        self.manifest = {"base_url": "http://127.0.0.1:1", "model": "simulation", "budget_ledger": "fixture"}
        self.run = {"project": "synthetic-project", "research": "synthetic-research"}
        self.preview = {"run_id": "popper_loop-1", "call_id": "tcl_test", "execution_revision": 15,
                        "preflight_sha256": "current", "job_ids_before": ["old"],
                        "tool_call": {"call_id": "tcl_test", "status": "pending_approval"}}
        self.budget = {"status": {"unresolved_requests": 0}}
        self.observed = {"tool_call": {"call_id": "tcl_test", "status": "completed"},
                         "continuation_jobs": [{"job_id": "child", "status": "succeeded"}],
                         "continuation_turns": [], "new_tool_calls": [], "persistent_active": False}
        self.decisions, self.executions, self.waits, self.closes = [], [], 0, 0
        self.valid_preview = True
        owner = self

        class WS:
            def __init__(self, *args, **kwargs):
                if not (owner.folder / "tcl_test.json").is_file():
                    raise AssertionError("intent must precede recovering constructor")
            def _agent_tool_public(self, call): return {**call, "preview": {"ok": owner.valid_preview}}
            def decide_agent_tool(self, *args): owner.decisions.append(args)
            def execute_agent_tool(self, *args):
                owner.executions.append(args)
                return {"status": "completed"}
            def wait_for_jobs(self, **kwargs):
                owner.waits += 1
                return False
            def server_close(self): owner.closes += 1

        self.ws_type = WS
        self.guards = [patch.object(runner, "preview_native_development_approval", return_value=self.preview),
                       patch.object(runner, "load_campaign", return_value=(self.manifest, self.run)),
                       patch.object(runner, "Workstation", WS),
                       patch.object(runner, "_observe", return_value=self.observed),
                       patch.object(runner, "_read_budget", return_value=self.budget)]
        self.mocks = [guard.start() for guard in self.guards]
        for guard in self.guards: self.addCleanup(guard.stop)

    def dispatch(self, **kwargs):
        return runner.approve_native_development_tool(self.root, "popper_loop-1", "tcl_test", 15, **kwargs)

    def test_intent_before_constructor_and_once_only(self):
        self.assertEqual("settled", self.dispatch()["status"])
        with patch.object(runner, "_reconcile", return_value={"status": "already_attempted_do_not_redispatch"}):
            self.assertEqual("already_attempted_do_not_redispatch", self.dispatch()["status"])
        self.assertEqual(1, len(self.decisions))
        self.assertEqual(1, len(self.executions))
        self.assertEqual("intent_claimed_not_approved", read_json(self.folder / "tcl_test.json")["phase"])

    def test_child_persisted_after_parent_thread_snapshot_waited_to_terminal(self):
        self.mocks[3].side_effect = [{**self.observed, "persistent_active": True,
                                    "continuation_jobs": [{"job_id": "child", "status": "queued"}]}, self.observed]
        self.assertEqual("settled", self.dispatch()["status"])
        self.assertEqual(2, self.waits)
        self.assertEqual("succeeded", read_json(self.folder / "tcl_test-settled.json")["continuation_jobs"][0]["status"])

    def test_live_thread_blocks_even_if_database_is_temporarily_terminal(self):
        with patch.object(self.ws_type, "wait_for_jobs", side_effect=[1, 0]) as wait:
            self.assertEqual("settled", self.dispatch()["status"])
            self.assertEqual(2, wait.call_count)

    def test_timeout_unknown_is_not_settled_and_never_retried(self):
        self.mocks[3].return_value = {**self.observed, "persistent_active": True}
        with patch.object(runner.time, "monotonic", side_effect=[0, 2]):
            result = self.dispatch(wait_timeout=1)
        self.assertEqual("timed_out_uncertain_no_retry", result["status"])
        self.assertEqual("uncertain_not_settled", result["phase"])
        with patch.object(runner, "_reconcile", return_value={"status": "already_attempted_do_not_redispatch"}):
            self.dispatch()
        self.assertEqual(1, len(self.executions))

    def test_terminal_unknown_usage_stays_blocked(self):
        self.budget["status"]["unresolved_requests"] = 1
        result = self.dispatch()
        self.assertEqual("usage_unknown_blocked", result["status"])
        self.assertEqual("uncertain_not_settled", result["phase"])

    def test_invalid_current_preview_retains_intent_without_approval(self):
        self.valid_preview = False
        with self.assertRaisesRegex(ProtocolError, "invalid current"):
            self.dispatch()
        result = read_json(self.folder / "tcl_test-settled.json")
        self.assertEqual("preflight_failed_no_approval", result["status"])
        self.assertEqual([], self.decisions)
        self.assertEqual([], self.executions)

    def test_context_change_before_constructor_blocks_recovery(self):
        self.mocks[0].side_effect = [self.preview, {**self.preview, "preflight_sha256": "changed"}]
        with patch.object(runner, "Workstation") as workstation:
            with self.assertRaisesRegex(ProtocolError, "after intent"):
                self.dispatch()
            workstation.assert_not_called()
        self.assertEqual("uncertain_not_settled", read_json(self.folder / "tcl_test-settled.json")["phase"])

    def test_context_change_after_constructor_still_cannot_approve(self):
        self.mocks[0].side_effect = [self.preview, self.preview, {**self.preview, "preflight_sha256": "changed"}]
        with self.assertRaisesRegex(ProtocolError, "before approval"):
            self.dispatch()
        self.assertEqual([], self.decisions)
        self.assertEqual(1, self.closes)

    def test_exhausted_or_unknown_preflight_never_constructs(self):
        self.mocks[0].side_effect = ProtocolError("unknown usage blocks approval")
        with patch.object(runner, "Workstation") as workstation:
            with self.assertRaisesRegex(ProtocolError, "unknown usage"):
                self.dispatch()
            workstation.assert_not_called()
        self.assertFalse(self.folder.exists())

    def test_each_new_native_write_requires_its_own_approval(self):
        self.observed["new_tool_calls"] = [{"status": "pending_approval", "call_id": "tcl_next"}]
        self.assertEqual("settled_needs_native_approval", self.dispatch()["status"])
        self.assertEqual(1, len(self.decisions))
        self.assertEqual("tcl_test", self.decisions[0][0])

    def test_original_pending_sibling_remains_needs_approval(self):
        self.observed["remaining_pending_tools"] = [{"status": "pending_approval", "call_id": "tcl_sibling"}]
        self.observed["new_tool_calls"] = []
        result = self.dispatch()
        self.assertEqual("settled_needs_native_approval", result["status"])
        self.assertEqual(1, len(self.decisions))
        self.assertEqual("tcl_test", self.decisions[0][0])

    def test_old_receipt_reconciles_read_only_without_workstation(self):
        self.folder.mkdir()
        actual = {"call_id": "tcl_test", "status": "running", "session_id": "session", "turn_id": "turn",
                  "tool_name": "research.status", "arguments": {"study_id": "S-test"}, "proposed_by": "agent"}
        (self.folder / "tcl_test.json").write_text(canonical({"phase": "reviewed_not_executed",
            "tool_call": {**actual, "status": "pending_approval"}}), encoding="utf-8")
        class Store:
            def close(self): pass
            def verify_turn_events(self): return {"ok": True}
            def verify_tool_events(self): return {"ok": True}
            def get_tool_call(self, _): return actual
        with patch.object(runner.AgentSessionStore, "readonly", return_value=Store()), \
                patch.object(runner, "Workstation") as workstation:
            result = self.dispatch()
            workstation.assert_not_called()
        self.assertEqual("already_attempted_do_not_redispatch", result["status"])
        self.assertEqual("running", result["actual_tool_call"]["status"])
        self.assertEqual([], self.decisions)
        actual["arguments"] = {"study_id": "S-other"}
        with patch.object(runner.AgentSessionStore, "readonly", return_value=Store()), \
                patch.object(runner, "Workstation") as workstation:
            with self.assertRaisesRegex(ProtocolError, "immutable tool identity"):
                self.dispatch()
            workstation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
