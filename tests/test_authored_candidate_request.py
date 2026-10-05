"""Synthetic source/dispatch checks; never invoke a provider or solver."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from evaluation import request_authored_candidate as runner
from popper.core import ProtocolError, canonical, digest, read_json


class SourceDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.ledger = self.project / "ledger.sqlite"
        self.evidence = {"scope": "dev", "study_id": "S-fixture", "job_id": "JOBR-fixture",
                         "state_version": 41, "candidate": {"trajectory_error": None},
                         "current_budget": {"kernel": {"available": 4.0}}}
        self.authored = {"observed": [
            {"pointer": "/development_diagnostics/scope", "value": "dev"},
            {"pointer": "/development_diagnostics/job_id", "value": "JOBR-fixture"},
            {"pointer": "/development_diagnostics/current_budget/kernel/available", "value": 4}],
            "decision": {"action": "propose_candidate", "candidate_index": 4,
                         "reason": "synthetic proposal, not a scientific method",
                         "predictions": ["synthetic prediction"], "falsification": ["synthetic check"],
                         "implementation": {"path": "model.py", "replacement":
                             "def solve(rhs,y0,times,budget,config):\n    return [y0 for _ in times]\n"}}}
        self.call = {"call_id": "tcl-fixture", "turn_id": "trn-parent", "session_id": "ses-fixture",
                     "tool_name": "research.revision_job", "status": "completed",
                     "arguments": {"study_id": "S-fixture", "job_id": "JOBR-fixture"},
                     "result": {"study_id": "S-fixture", "state_version": 41,
                                "job": {"job_id": "JOBR-fixture"},
                                "development_diagnostics": deepcopy(self.evidence)}}
        self.turn = {"turn_id": "trn-source", "parent_turn_id": "trn-parent", "status": "completed",
                     "session_id": "ses-fixture", "assistant_message": canonical(self.authored)}
        self.snapshot = {"messages": [
            {"role": "assistant", "tool_calls": [{"id": self.call["call_id"],
                "function": {"name": "research_revision_job", "arguments": canonical(self.call["arguments"])}}]},
            {"role": "tool", "tool_call_id": self.call["call_id"], "content": canonical({
                "call_id": self.call["call_id"], "status": "completed", "result": self.call["result"]})}]}
        owner = self

        class Store:
            def close(self): pass
            def verify_turn_events(self): return {"ok": True}
            def verify_tool_events(self): return {"ok": True}
            def get_turn(self, _id): return deepcopy(owner.turn)
            def turns(self, _id): return [deepcopy(owner.turn)]
            def tool_calls(self, _id): return [deepcopy(owner.call)]
            def get_tool_call(self, _id): return deepcopy(owner.call)
            def agent_job_for_turn(self, _id): return {"status": "succeeded", "total_tokens": 100}

        for target, value in (("AgentSessionStore.readonly", Store()), ("load_input", self.snapshot)):
            p = patch.object(runner.AgentSessionStore, "readonly", return_value=value) if target.startswith("Agent") else patch.object(runner, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        with sqlite3.connect(self.ledger) as conn:
            conn.execute("CREATE TABLE requests(id TEXT,state TEXT,used INTEGER,sha256 TEXT,snapshot TEXT)")
            conn.execute("INSERT INTO requests VALUES(?,?,?,?,?)", ("trn-source", "settled", 100,
                         digest(self.snapshot), canonical(self.snapshot)))

    def source(self):
        self.turn["assistant_message"] = canonical(self.authored)
        return runner._source_data(self.project, "ses-fixture", "trn-source", "JOBR-fixture", self.evidence, self.ledger)

    def test_source_preserved_not_promoted_or_rewritten(self):
        authored, record = self.source()
        self.assertEqual(self.authored, authored)
        self.assertNotIn("kind", authored)
        self.assertTrue(record["facts_verified"])
        self.assertFalse(record["structured_review_accepted"])
        self.assertFalse(record["execution_approved"])
        self.assertEqual(canonical(self.authored), record["source_text"])

    def test_fact_mismatch_boolean_and_string_are_not_repaired(self):
        for value in (True, "4", 4.000000000000001):
            with self.subTest(value=value):
                self.authored["observed"][-1]["value"] = value
                with self.assertRaisesRegex(ProtocolError, "facts do not match"):
                    self.source()

    def test_stale_receipt_cross_session_and_nonterminal_rejected(self):
        for key, value in (("session_id", "other"), ("turn_id", "other"), ("status", "pending_approval")):
            with self.subTest(key=key):
                old = self.call[key]
                self.call[key] = value
                with self.assertRaises(ProtocolError): self.source()
                self.call[key] = old
        self.call["result"]["state_version"] = 42
        with self.assertRaises(ProtocolError): self.source()

    def test_intact_assistant_tool_group_required(self):
        self.snapshot["messages"][0]["tool_calls"] = []
        with self.assertRaisesRegex(ProtocolError, "intact real"):
            self.source()

    def test_unknown_usage_blocks_source(self):
        with sqlite3.connect(self.ledger) as conn:
            conn.execute("UPDATE requests SET state='usage_unknown'")
        with self.assertRaisesRegex(ProtocolError, "usage/input"):
            self.source()

    def test_duplicate_or_private_pointer_rejected(self):
        self.authored["observed"].append(deepcopy(self.authored["observed"][0]))
        with self.assertRaisesRegex(ProtocolError, "unique"): self.source()
        self.authored["observed"] = [{"pointer": "/development_diagnostics/source_digests", "value": {}}]
        with self.assertRaisesRegex(ProtocolError, "outside public"): self.source()

    def test_source_parser_never_repairs_prefix_or_duplicate_json(self):
        for text in ('{"decision":', '{"a":1,"a":2}', '```json\n{"a":1}', '[]', '{"x":NaN}'):
            with self.subTest(text=text), self.assertRaises(ProtocolError):
                runner._complete_json(text)

    def test_candidate_has_explicit_index_predictions_and_complete_solver(self):
        self.assertEqual(4, runner._decision(self.authored)["candidate_index"])
        for key, value in (("action", "stop"), ("candidate_index", 5), ("predictions", []),
                           ("implementation", {"path": "model.py", "replacement": "def solve("})):
            authored = deepcopy(self.authored)
            authored["decision"][key] = value
            with self.subTest(key=key), self.assertRaises(ProtocolError): runner._decision(authored)


class EligibilityTests(unittest.TestCase):
    def test_lifecycle_untested_does_not_override_failed_execution_attempt(self):
        # Matches the actual refusal: all Hypotheses still carry lifecycle
        # "untested", but index4 already has a failed native execution Job.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "popper_loop-1"
            folder.mkdir()
            (folder / "initial-request.json").write_text(
                canonical({"session_id": "ses-test"}), encoding="utf-8")
            project = folder / "project"
            run = {"arm": "popper_loop", "project": str(project), "research": str(folder / "research")}
            manifest = {"budget_ledger": "synthetic"}
            evidence = {"scope": "dev", "study_id": "S-test", "job_id": "JOBR-test", "state_version": 41}
            evidence["evidence_sha256"] = digest(evidence)
            decision = {"candidate_index": 4}
            with patch.object(runner, "load_campaign", return_value=(manifest, run)), \
                    patch.object(runner, "_validate_arm", return_value=({}, {})), \
                    patch.object(runner, "_quiet_registries"), \
                    patch.object(runner, "_session_quiet"), \
                    patch.object(runner, "_research_quiet"), \
                    patch.object(runner, "_read_budget", return_value={}), \
                    patch.object(runner, "build_development_evidence", return_value=evidence), \
                    patch.object(runner, "_source_data", return_value=({}, {})), \
                    patch.object(runner, "_decision", return_value=decision), \
                    patch.object(runner, "_candidate_scope", return_value=(
                        [{"slot": i + 1} for i in range(6)], {0, 3, 4}, set(range(6)))), \
                    patch.object(runner, "Workstation") as workstation:
                with self.assertRaisesRegex(ProtocolError, "have no execution attempt"):
                    runner.preview_authored_candidate_request(root, "popper_loop-1", "trn-test", "JOBR-test", 14)
                workstation.assert_not_called()
            self.assertFalse((folder / "authored-candidate-native-request-v1.json").exists())
            self.assertEqual(4, decision["candidate_index"])

    def test_truncated_second_and_stopped_third_runs_are_never_reopened(self):
        for run_id in ("popper_loop-2", "popper_loop-3"):
            with self.subTest(run_id=run_id), patch.object(runner, "load_campaign") as load:
                with self.assertRaisesRegex(ProtocolError, "only loop1"):
                    runner.preview_authored_candidate_request(Path("unused"), run_id, "trn-test", "JOBR-test", 14)
                load.assert_not_called()


class DispatchTests(unittest.TestCase):
    def test_once_intent_and_complete_source_before_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "popper_loop-1"
            folder.mkdir()
            preview = {"preview_sha256": "unchanged", "session_id": "ses-test", "source_turn_id": "trn-test",
                       "identity": {}, "source_data": {"source_text": "source data", "source_sha256": "source",
                       "read_call_id": "read", "evidence_sha256": "evidence"}}
            manifest = {"base_url": "http://127.0.0.1:1", "model": "simulation", "budget_ledger": "synthetic"}
            run = {"project": str(folder), "research": str(folder)}
            sends = []

            class WS:
                def __init__(self, *args, **kwargs):
                    assert (folder / "authored-candidate-native-request-v1.json").is_file()
                def _validate_turn_input(self, prompt): pass
                def _assemble_session_context(self, *args):
                    return {"input_bytes": 100, "messages": [{"role": "assistant", "content": "source data"}]}
                def _chat_system_prompt(self, *args): return "synthetic"
                def _reserve_context(self, *args): return 100
                def launch_session_turn(self, *args):
                    sends.append(args)
                    return {"turn": {"turn_id": "new"}, "job": {"job_id": "new-job"}}
                def wait_for_jobs(self, **kwargs): return False
                def server_close(self): pass

            budget = {"status": {"used_tokens": 0, "unresolved_requests": 0}, "policy": {"max_tokens": 400000000}}
            class Store:
                def close(self): pass

            with patch.object(runner, "preview_authored_candidate_request", return_value=preview), \
                    patch.object(runner, "load_campaign", return_value=(manifest, run)), \
                    patch.object(runner, "_session_quiet", return_value={}), \
                    patch.object(runner, "_read_budget", return_value=budget), \
                    patch.object(runner, "_result", return_value={"jobs": [], "tool_calls": []}), \
                    patch.object(runner, "Workstation", WS), \
                    patch.object(runner.AgentSessionStore, "readonly", return_value=Store()):
                args = (root, "popper_loop-1", "trn-test", "JOBR-test", 14)
                self.assertEqual("terminal_source_unreviewed", runner.dispatch_authored_candidate_request(*args)["status"])
                self.assertEqual("already_attempted_do_not_redispatch", runner.dispatch_authored_candidate_request(*args)["status"])
                self.assertEqual(1, len(sends))
                with self.assertRaisesRegex(ProtocolError, "intent conflict"):
                    runner.dispatch_authored_candidate_request(root, "popper_loop-1", "another", "JOBR-test", 14)
            record = read_json(folder / "authored-candidate-native-result-v1.json")
            self.assertFalse(record["execution_approved"])
            self.assertFalse(record["structured_source_review_accepted"])


if __name__ == "__main__":
    unittest.main()
