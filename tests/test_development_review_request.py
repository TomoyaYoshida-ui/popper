"""Synthetic dispatcher gates. Fake Workstation never sends any network request."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from evaluation import review_development_request as runner
from popper.core import ProtocolError, canonical, digest, file_hash, read_json, write_json
from popper.research.agent_evaluation_budget import AgentEvaluationBudget
from popper.research.execution_jobs import RevisionExecutionJobStore
from popper.sessions import AgentSessionStore


class DevelopmentReviewRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.campaign = self.root / "campaign"
        self.folder = self.campaign / "popper_loop-2"
        self.project = self.folder / "project"
        self.research = self.folder / "research"
        self.project.mkdir(parents=True)
        self.research.mkdir()
        with closing(AgentSessionStore(self.project)) as store:
            self.session_id = store.create_session("synthetic fixture", model="simulation")["session_id"]
        write_json(self.folder / "initial-request.json", {"session_id": self.session_id})
        self.ledger = self.root / "shared-budget.sqlite"
        with closing(AgentEvaluationBudget(self.ledger, max_requests=60, max_tokens=400000000)):
            pass
        self.manifest = {"budget_ledger": str(self.ledger), "model": "synthetic-model",
                         "base_url": "http://127.0.0.1:1/v1", "fixture": "simulation only"}
        self.run = {"run_id": "popper_loop-2", "arm": "popper_loop",
                    "project": str(self.project), "research": str(self.research)}
        write_json(self.campaign / "campaign.json", self.manifest)
        self.revision = self.campaign / "execution-revisions" / "0013.json"
        write_json(self.revision, {"fixture": "synthetic gate, not acceptance evidence"})
        self.log = self.root / "synthetic-regression.log"
        self.log.write_text("synthetic test-only full regression receipt\n", encoding="utf-8")
        write_json(self.log.with_suffix(".receipt.json"), {
            "exit_code": 0, "sources_stable": True, "log_sha256": file_hash(self.log),
            "revision": str(self.revision), "revision_sha256": file_hash(self.revision),
            "command": ["simulation-python", "-m", "pytest", "-q", "-rfEs", "--tb=short"]})
        self.protected = self.root / "synthetic-private.json"
        write_json(self.protected, {"fixture": "never disclosed"})
        self.arm_path = self.campaign / "execution-arms" / "0013.json"
        self.arm = {"campaign_sha256": file_hash(self.campaign / "campaign.json"),
                    "execution_snapshot_sha256": file_hash(self.revision),
                    "regression_log": str(self.log), "regression_sha256": file_hash(self.log),
                    "read_probe": {"passed": True, "host_read_verified": True, "network_block": False,
                                   "protected_count": 1, "protected_sha256": [file_hash(self.protected)]}}
        write_json(self.arm_path, self.arm)
        self.evidence = {"scope": "dev", "study_id": "S-simulation", "job_id": "JOBR-" + "a" * 24,
                         "state_version": 45, "evidence_sha256": "e" * 64,
                         "candidate": {"private_fixture_payload": "not put in prompt", "cases": ["x" * 9000]}}
        self.mode, self.constructor_count, self.sent, self.closed = "pending", 0, [], False
        self.wait_sequence, self.wait_calls = [], 0
        self.preflight_error = None
        owner = self

        class FakeWorkstation:
            def __init__(self, _address, project, **kwargs):
                owner.constructor_count += 1
                owner.assertTrue(owner.intent.exists(), "intent must precede even local service creation")
                owner.assertEqual(str(owner.ledger), kwargs["evaluation_budget_path"])
                owner.assertNotIn("max_tokens", kwargs)
                owner.assertNotIn("token_budget", kwargs)
                self.project, self.dispatched = Path(project), None

            def _validate_turn_input(self, prompt):
                owner.assertLessEqual(len(prompt), 8000)
                if owner.preflight_error:
                    raise owner.preflight_error

            def _chat_system_prompt(self, _project):
                return "synthetic system"

            def _assemble_session_context(self, store, sid, system, prompt):
                return {"system": system, "messages": [{"role": "user", "content": prompt}],
                        "tools": [{"name": "research_revision_job"}], "input_bytes": 4096}

            def _reserve_context(self, _snapshot):
                return 4096

            def launch_session_turn(self, sid, prompt, key, _trace):
                owner.assertTrue(owner.intent.exists())
                owner.assertTrue((owner.folder / "supplemental-development-review-preflight-v1.json").exists())
                owner.sent.append({"sid": sid, "prompt": prompt, "key": key})
                if owner.mode == "launch_error":
                    raise RuntimeError('api_key="fixture-secret". Uncertain transport')
                with closing(AgentSessionStore(self.project)) as store:
                    turn, replayed = store.start_turn(sid, prompt, key, actor="synthetic",
                                                      trace_id="trc_synthetic", enqueue=True,
                                                      token_reservation=4096)
                    job = store.agent_job_for_turn(turn["turn_id"])
                    store.claim_agent_job(job["job_id"], worker_id="synthetic")
                    with closing(AgentEvaluationBudget(owner.ledger)) as ledger:
                        ledger.reserve(turn["turn_id"], {"synthetic": True}, 4096)
                        if owner.mode not in {"timeout", "unknown"}:
                            ledger.settle(turn["turn_id"], 31)
                        elif owner.mode == "unknown":
                            ledger.settle(turn["turn_id"], None)
                    if owner.mode == "pending":
                        store.propose_tool_call(sid, turn["turn_id"], "research.revision_job",
                                                {"study_id": owner.evidence["study_id"], "job_id": owner.evidence["job_id"]},
                                                "disclose", "synthetic-read-proposal", actor="agent", trace_id="trc_synthetic")
                    if owner.mode != "timeout":
                        store.complete_turn(turn["turn_id"], "Synthetic source without fabricated interpretation.")
                        store.finish_agent_job(job["job_id"], usage={"total_tokens": 31} if owner.mode != "unknown" else None)
                    self.dispatched = {"turn": turn, "job": job, "replayed": replayed}
                    return self.dispatched

            def wait_for_jobs(self, timeout):
                owner.assertLessEqual(timeout, 1)
                owner.wait_calls += 1
                owner.assertFalse(owner.closed, "do not close a live continuation worker")
                if owner.wait_sequence:
                    return owner.wait_sequence.pop(0)
                return 1 if owner.mode == "timeout" else 0

            def server_close(self):
                owner.closed = True

        self.patches = [patch.object(runner, "Workstation", FakeWorkstation),
                        patch.object(runner, "load_campaign", side_effect=lambda *_args: (deepcopy(self.manifest), deepcopy(self.run))),
                        patch.object(runner, "protected_files", return_value=[self.protected]),
                        patch.object(runner, "build_development_evidence", side_effect=lambda *_args: deepcopy(self.evidence))]
        for mock in self.patches:
            mock.start()

    def tearDown(self):
        for mock in reversed(self.patches):
            mock.stop()
        self.temp.cleanup()

    @property
    def intent(self):
        return self.folder / "supplemental-development-review-request-v1.json"

    def dispatch(self, **kwargs):
        return runner.dispatch_development_review(self.campaign, "popper_loop-2", self.evidence["job_id"], 13, **kwargs)

    def test_one_root_request_stops_at_native_read_approval_without_execution(self):
        result = self.dispatch()
        self.assertEqual("needs_read_approval", result["status"])
        self.assertEqual(1, len(self.sent))
        self.assertEqual(runner.IDEMPOTENCY_KEY, self.sent[0]["key"])
        self.assertEqual(1, result["budget_after"]["status"]["request_count"])
        self.assertEqual(31, result["budget_after"]["status"]["used_tokens"])
        self.assertEqual("pending_approval", result["tool_calls"][0]["status"])
        self.assertIsNone(result["tool_calls"][0]["result"])
        self.assertTrue(self.closed)

    def test_prompt_preserves_contract_not_full_diagnostics_or_method_advice(self):
        self.dispatch()
        prompt = self.sent[0]["prompt"]
        self.assertNotIn("private_fixture_payload", prompt)
        self.assertNotIn("x" * 9000, prompt)
        self.assertIn("research_revision_job", prompt)
        self.assertIn("solve(rhs,y0,times,budget,config)", prompt)
        self.assertIn("stopping is valid", prompt)
        self.assertIn("development_review_v1", prompt)
        self.assertNotIn("Gauss", prompt)
        self.assertNotIn("Verlet", prompt)
        request = read_json(self.intent)
        self.assertEqual(file_hash(runner.__file__), request["helper_sha256"])
        self.assertEqual("intent_claimed_not_dispatched", request["phase"])

    def test_second_invocation_only_reads_receipts_without_service_or_provider(self):
        original = self.dispatch()
        before = {str(path): file_hash(path) for path in self.folder.glob("supplemental-*.json")}
        self.assertEqual("already_attempted_do_not_redispatch", self.dispatch()["status"])
        self.assertEqual(1, self.constructor_count)
        self.assertEqual(1, len(self.sent))
        self.assertEqual(before, {str(path): file_hash(path) for path in self.folder.glob("supplemental-*.json")})
        self.assertEqual(original, read_json(self.folder / "supplemental-development-review-result-v1.json"))

    def test_loop3_or_single_shot_cannot_restart(self):
        for run in ("popper_loop-3", "single_shot-1", "../popper_loop-2"):
            with self.subTest(run=run), self.assertRaisesRegex(ProtocolError, "loop 3 already stopped"):
                runner.dispatch_development_review(self.campaign, run, self.evidence["job_id"], 13)
        self.assertEqual(0, self.constructor_count)
        self.assertFalse(self.intent.exists())

    def test_wrong_arm_or_protected_probe_blocks_before_intent(self):
        for section, key, value in (("arm", "execution_snapshot_sha256", "0" * 64),
                                    ("probe", "passed", False),
                                    ("probe", "host_read_verified", False),
                                    ("probe", "protected_count", 0),
                                    ("probe", "protected_sha256", ["0" * 64])):
            with self.subTest(key=key):
                arm = deepcopy(self.arm)
                (arm if section == "arm" else arm["read_probe"])[key] = value
                write_json(self.arm_path, arm)
                with self.assertRaises(ProtocolError):
                    self.dispatch()
                self.assertFalse(self.intent.exists())
        self.assertEqual(0, self.constructor_count)

    def test_previous_execution_arm_not_usable_after_new_freeze(self):
        write_json(self.revision.with_name("0014.json"), {"fixture": "new execution revision"})
        with self.assertRaisesRegex(ProtocolError, "current frozen"):
            self.dispatch()
        self.assertFalse(self.intent.exists())

    def test_nonzero_unstable_or_foreign_ci_receipt_blocks(self):
        receipt_path = self.log.with_suffix(".receipt.json")
        original = read_json(receipt_path)
        for key, value in (("exit_code", 1), ("exit_code", False), ("sources_stable", False),
                           ("revision_sha256", "0" * 64), ("log_sha256", "0" * 64),
                           ("command", ["simulation-python", "-m", "pytest", "tests/test_one.py"]),
                           ("revision", str(self.root / "foreign.json"))):
            with self.subTest(key=key, value=value):
                write_json(receipt_path, {**original, key: value})
                with self.assertRaisesRegex(ProtocolError, "full CI regression receipt"):
                    self.dispatch()
                self.assertFalse(self.intent.exists())
        self.assertEqual(0, self.constructor_count)

    def test_unknown_ledger_blocks_without_creating_another_request(self):
        with closing(AgentEvaluationBudget(self.ledger)) as ledger:
            ledger.reserve("synthetic-unknown", {"fixture": True}, 100)
            ledger.settle("synthetic-unknown", None)
        with self.assertRaisesRegex(ProtocolError, "unknown usage"):
            self.dispatch()
        self.assertFalse(self.intent.exists())
        self.assertEqual([], self.sent)

    def test_missing_ledger_is_not_initialized(self):
        missing = self.root / "missing-ledger.sqlite"
        self.manifest["budget_ledger"] = str(missing)
        with self.assertRaisesRegex(ProtocolError, "missing"):
            self.dispatch()
        self.assertFalse(missing.exists())
        self.assertFalse(self.intent.exists())

    def test_exhausted_or_unauthorized_budget_blocks_not_increased(self):
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("UPDATE policy SET max_tokens=400000001 WHERE id=1")
        with self.assertRaisesRegex(ProtocolError, "authorization"):
            self.dispatch()
        self.assertFalse(self.intent.exists())
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("UPDATE policy SET max_tokens=10 WHERE id=1")
        with self.assertRaisesRegex(ProtocolError, "cannot reserve"):
            self.dispatch()
        self.assertTrue(self.intent.exists())
        self.assertEqual([], self.sent)
        self.assertEqual("preflight_failed_no_dispatch", read_json(self.folder / "supplemental-development-review-result-v1.json")["status"])

    def test_old_pending_approval_or_active_model_job_not_implicitly_resumed(self):
        with closing(AgentSessionStore(self.project)) as store:
            turn, _ = store.start_turn(self.session_id, "old source", "old-turn-001", actor="fixture", trace_id="trc_fixture")
            store.propose_tool_call(self.session_id, turn["turn_id"], "research.status", {"study_id": "S-fixture"},
                                    "disclose", "old-tool-001", actor="agent", trace_id="trc_fixture")
            store.complete_turn(turn["turn_id"], "Old genuine pending read.")
        with self.assertRaisesRegex(ProtocolError, "pending or active tools"):
            self.dispatch()
        self.assertEqual(0, self.constructor_count)
        self.assertFalse(self.intent.exists())

    def test_active_research_queue_blocks_before_workstation_recovery(self):
        queue = RevisionExecutionJobStore(self.research / "revision-execution-jobs.sqlite")
        try:
            queue.enqueue("synthetic-old", "REV-" + "a" * 24)
        finally:
            queue.close()
        with self.assertRaisesRegex(ProtocolError, "active persistent work"):
            self.dispatch()
        self.assertEqual(0, self.constructor_count)
        self.assertFalse(self.intent.exists())

    def test_timeout_does_not_cancel_or_redispatch_and_retains_uncertainty(self):
        self.mode = "timeout"
        with patch.object(runner.time, "monotonic", side_effect=[100, 102]):
            result = self.dispatch(wait_timeout=1)
        self.assertEqual("timed_out_uncertain_no_retry", result["status"])
        self.assertEqual("running", result["jobs"][0]["status"])
        self.assertEqual(1, result["budget_after"]["status"]["unresolved_requests"])
        self.assertEqual("already_attempted_do_not_redispatch", self.dispatch()["status"])
        self.assertEqual(1, len(self.sent))

    def test_transport_error_redacted_and_intent_blocks_second_try(self):
        self.mode = "launch_error"
        with self.assertRaisesRegex(RuntimeError, "Uncertain transport"):
            self.dispatch()
        result = read_json(self.folder / "supplemental-development-review-result-v1.json")
        self.assertEqual("dispatch_uncertain_no_retry", result["status"])
        self.assertNotIn("fixture-secret", canonical(result))
        self.assertEqual("already_attempted_do_not_redispatch", self.dispatch()["status"])
        self.assertEqual(1, len(self.sent))

    def test_preflight_failure_retains_intent_and_does_not_send(self):
        self.preflight_error = ProtocolError("complete input does not fit the context byte limit")
        with self.assertRaisesRegex(ProtocolError, "byte limit"):
            self.dispatch()
        result = read_json(self.folder / "supplemental-development-review-result-v1.json")
        self.assertEqual("preflight_failed_no_dispatch", result["status"])
        self.assertEqual([], self.sent)
        self.assertEqual("already_attempted_do_not_redispatch", self.dispatch()["status"])

    def test_unknown_usage_after_terminal_is_blocked_not_success(self):
        self.mode = "unknown"
        result = self.dispatch()
        self.assertEqual("usage_unknown_blocked", result["status"])
        self.assertEqual(1, result["budget_after"]["status"]["unresolved_requests"])
        self.assertEqual(1, len(self.sent))

    def test_plain_source_is_not_converted_to_invented_structured_review(self):
        self.mode = "text"
        result = self.dispatch()
        self.assertEqual("terminal_source_unreviewed", result["status"])
        self.assertEqual([], result["tool_calls"])
        self.assertEqual("Synthetic source without fabricated interpretation.", result["root_turn"]["assistant_message"])
        self.assertNotIn("decision", result)

    def test_terminal_database_gap_waits_for_native_continuation_thread(self):
        # The root job is already succeeded in SQLite, but its worker has not
        # finished deciding/scheduling a bounded invalid-tool continuation.
        self.wait_sequence = [1, 0]
        result = self.dispatch()
        self.assertEqual(2, self.wait_calls)
        self.assertEqual("needs_read_approval", result["status"])
        self.assertEqual(1, len(self.sent))
        self.assertTrue(self.closed)


if __name__ == "__main__":
    unittest.main()
