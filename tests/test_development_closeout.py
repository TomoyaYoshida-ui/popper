"""Temporary local closeout tests: no paid calls, experiments or confirmation."""
from __future__ import annotations

import copy
import csv
import tempfile
import unittest
import xml.etree.ElementTree as ET
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from evaluation import closeout_development as closeout
from popper.core import ProtocolError, canonical, digest, file_hash, read_json, write_json
from popper.research.agent_evaluation_budget import AgentEvaluationBudget
from popper.research.execution_jobs import RevisionExecutionJobStore
from popper.sessions import AgentSessionStore
from popper.task_context import save_input
from popper.domains.oscillator_trajectory import PACK


class DevelopmentCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.campaign = self.root / "campaign"
        self.campaign.mkdir()
        self.study_root = self.root / "scientific"
        self.study_root.mkdir()
        write_json(self.study_root / "protocol.json", {"kind": "synthetic protocol, not scientific acceptance"})
        self.project = self.campaign / "popper_loop-2" / "project"
        self.research = self.campaign / "popper_loop-2" / "research"
        self.project.mkdir(parents=True)
        self.research.mkdir()
        self.ledger = self.root / "ledger.sqlite"
        with closing(AgentEvaluationBudget(self.ledger, max_requests=60, max_tokens=400000)) as budget:
            budget.reserve("historical-request", {"request_limits": {"max_completion_tokens": 4096}}, 20)
            budget.settle("historical-request", 10)
            budget.amend_policy("synthetic-authorization", {"max_requests": 60, "max_tokens": 400000},
                {"max_requests": 60, "max_tokens": 400000000}, {"actor": "user", "text": "synthetic authorized test amendment"})
        self.manifest = {"study_root": str(self.study_root), "protocol_sha256": file_hash(self.study_root / "protocol.json"),
            "budget_ledger": str(self.ledger), "token_limit": 400000, "model": "synthetic-model",
            "primary_criterion": {"relative_error_reduction": .2, "successful_loop_runs": 2},
            "runs": [{"run_id": "popper_loop-2", "arm": "popper_loop", "project": str(self.project), "research": str(self.research)}],
            "implementation_sha256": {str(Path(sys.modules[PACK.__class__.__module__].__file__).resolve()):
                file_hash(sys.modules[PACK.__class__.__module__].__file__)}}
        write_json(self.campaign / "campaign.json", self.manifest)
        self.study = {"exists": True, "study_id": "S-synthetic", "state_version": 45, "claims_count": 0,
            "confirmation_consumed": False, "confirmation_eligible": False, "confirmation_refusal": "threshold not met",
            "control_hypothesis_id": "H-control", "candidate_hypothesis_ids": ["H-candidate"],
            "budget": {"cap": 9, "spent": 5, "reserved": 0, "available": 4}}
        self.original_study_reader = closeout._study_snapshot
        self.patcher = patch.object(closeout, "_study_snapshot", side_effect=lambda *_: copy.deepcopy(self.study))
        self.patcher.start()
        self.presence_patcher = patch.object(closeout, "_execution_presence", return_value="executed")
        self.presence_patcher.start()

    def tearDown(self):
        self.presence_patcher.stop()
        self.patcher.stop()
        self.temp.cleanup()

    def revision(self, label, *, control=False):
        identity = {"hypothesis_id": "H-control" if control else "H-candidate", "design_id": "D-synthetic", "label": label}
        revision_id = "REV-" + digest(identity)[:24]
        directory = self.research / "revisions" / revision_id
        (directory / "code").mkdir(parents=True)
        source = directory / "code" / "model.py"
        source.write_text("def solve():\n    return 'synthetic only'\n", encoding="utf-8")
        write_json(directory / "revision.json", {"revision_id": revision_id, "status": "immutable",
            "identity": identity, "files": {"model.py": file_hash(source)}})
        return revision_id

    def job(self, label, status="succeeded", *, control=False):
        revision_id = self.revision(label, control=control)
        with closing(RevisionExecutionJobStore(self.research / "revision-execution-jobs.sqlite")) as queue:
            job = queue.enqueue(label, revision_id)
            if status in {"succeeded", "failed", "running"}:
                queue.claim(job["job_id"])
            if status == "succeeded":
                job = queue.finish(job["job_id"], run_id="RUN-" + label)
            elif status == "failed":
                job = queue.finish(job["job_id"], error='{"api_key":"synthetic-secret","message":"real retained failure"}')
            elif status == "cancelled":
                job = queue.cancel(job["job_id"], "synthetic cancellation")
            else:
                job = queue.get(job["job_id"])
        return job

    def evidence(self, project, research, job_id):
        with closing(RevisionExecutionJobStore.readonly(research / "revision-execution-jobs.sqlite")) as queue:
            job = queue.get(job_id)
        candidate = {"observation_id": "OBS-" + job_id if job["status"] == "succeeded" else None,
            "trajectory_error": 9.0 if job["status"] == "succeeded" else None,
            "execution_status": job["status"], "trusted_rhs_queries": 12,
            "execution_cases": 1, "trajectory_valid_cases": 1,
            "cases": [{"id": "synthetic-case", "system": "harmonic", "trajectory_error": 9.0,
                "energy_drift": .5, "failed": False, "query_count": 12}],
            "systems": {"harmonic": {"case_count": 1, "failed_cases": 0, "mean_trajectory_error": 9.0,
                "mean_sampled_energy_drift_successful": .5}}, "workers": []}
        body = {"study_id": "S-synthetic", "state_version": 45, "job_id": job_id,
            "revision_id": job["revision_id"], "run_id": job.get("run_id"), "source_digests": {"synthetic": "hash"},
            "scope": "dev", "candidate": candidate, "control": None,
            "comparison": {"absolute_improvement": -8.0, "relative_improvement": -8.0,
                "threshold": .2, "development_threshold_met": False}}
        return body | {"evidence_sha256": digest(body)}

    def snapshot(self):
        return {str(path.relative_to(self.root)): file_hash(path) for path in self.root.rglob("*")
            if path.is_file() and path.suffix not in {"-wal", "-shm"}}

    def saved_review(self, job, *, action="stop", structured=True):
        from popper.research.development_review import _source_turn, verify_development_review
        evidence = self.evidence(self.project, self.research, job["job_id"])
        decision = {"action": action, "reason": "Explicit synthetic model decision"}
        if action == "propose_candidate":
            decision.update(candidate_index=1, predictions=["Synthetic prediction"],
                falsification=["Synthetic failure condition"])
        authored = {"kind": "development_review_v1",
            "observed": [{"pointer": "/candidate/trajectory_error", "value": 9.0}],
            "mechanism_hypotheses": ["This mechanism is not verified by a metric"],
            "unsupported_assertions": [], "decision": decision}
        with closing(AgentSessionStore(self.project)) as store:
            session = store.create_session("review source", model="synthetic")
            turn, _ = store.start_turn(session["session_id"], "Review actual feedback", "review-root", actor="user", trace_id="trc_review")
            store.complete_turn(turn["turn_id"], canonical(authored) if structured else "Unstructured prose does not choose a candidate")
        source, _ = _source_turn(self.project, session["session_id"], turn["turn_id"])
        assessment = (verify_development_review(evidence, authored,
            registered_candidates=[{}, {}], untested_indices={1}) if structured else
            {"schema_valid": False, "facts_verified": False, "checks": [], "decision": None})
        preview = {"kind": "DevelopmentReview", "schema_version": "1.0", "scope": "dev",
            "project": str(self.project), "research": str(self.research), "study_id": evidence["study_id"],
            "state_version": evidence["state_version"], "job_id": evidence["job_id"],
            "revision_id": evidence["revision_id"], "run_id": evidence["run_id"],
            "evidence_sha256": evidence["evidence_sha256"], "source_digests": evidence["source_digests"],
            "source": source, "status": "structured_source_review" if structured else "unverified_source",
            "authored_review": authored if structured else None, "assessment": assessment}
        preview["approval_sha256"] = digest(preview)
        key_sha = digest("synthetic-review-receipt")
        record = {"schema_version": "1.0", "request_sha256": digest("synthetic request"),
            "idempotency_sha256": key_sha, "review": preview, "effect": "application audit only"}
        record["record_sha256"] = digest(record)
        path = self.project / ".popper" / "drev" / (key_sha[:24] + ".json")
        path.parent.mkdir(parents=True)
        write_json(path, record)
        return path, record

    def test_review_records_readonly_stop_has_fact_checks_not_final_candidate(self):
        job = self.job("reviewed")
        path, record = self.saved_review(job)
        before = self.snapshot()
        report, _ = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        run = report["runs"][0]
        review = run["development_reviews"]["records"][0]
        self.assertEqual(record["record_sha256"], review["record_sha256"])
        self.assertEqual(file_hash(path), review["file_sha256"])
        self.assertEqual(record["review"]["source"]["turn_id"], review["source_turn"]["turn_id"])
        self.assertTrue(review["historical_assessment"]["facts_verified"])
        self.assertTrue(review["current_assessment"]["facts_verified"])
        self.assertTrue(review["explicit_stop"])
        self.assertFalse(review["execution_approved"])
        self.assertEqual("unverified", review["historical_assessment"]["mechanism_hypotheses"][0]["status"])
        self.assertEqual("current_evidence", review["freshness"])
        self.assertIsNone(run["final_candidate"])
        self.assertEqual(before, self.snapshot())
        self.assertIn("模型明确停止", closeout._markdown(report))

    def test_review_old_version_is_explicitly_stale_not_current_decision(self):
        job = self.job("old-review")
        self.saved_review(job)
        self.study["state_version"] = 46
        def advanced(*args):
            evidence = self.evidence(*args)
            evidence["state_version"] = 46
            evidence["evidence_sha256"] = digest({key: value for key, value in evidence.items() if key != "evidence_sha256"})
            return evidence
        report, _ = closeout.build_closeout(self.campaign, evidence_builder=advanced)
        review = report["runs"][0]["development_reviews"]["records"][0]
        self.assertTrue(review["stale"])
        self.assertIn("state_version", review["stale_fields"])
        self.assertIsNone(review["current_assessment"])
        self.assertEqual("needs_new_review", review["freshness"])
        self.assertIn("需重新核对", closeout._markdown(report))

    def test_review_proposal_is_not_final_or_approved(self):
        job = self.job("proposal-review")
        self.saved_review(job, action="propose_candidate")
        with patch("popper.research.development_review._candidate_scope", return_value=([{}, {}], {0}, {1})):
            report, _ = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        review = report["runs"][0]["development_reviews"]["records"][0]
        self.assertEqual("propose_candidate", review["decision_action"])
        self.assertFalse(review["current_assessment"]["decision"]["eligible"])
        self.assertIn("candidate_development_budget_unavailable", review["current_assessment"]["decision"]["blocking_reasons"])
        self.assertFalse(review["explicit_stop"])
        self.assertIsNone(report["runs"][0]["final_candidate"])

    def test_review_unstructured_prose_does_not_create_decision(self):
        job = self.job("prose-review")
        self.saved_review(job, structured=False)
        report, _ = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        review = report["runs"][0]["development_reviews"]["records"][0]
        self.assertIsNone(review["decision_action"])
        self.assertFalse(review["explicit_stop"])
        self.assertIsNone(review["current_assessment"])

    def test_review_integrity_and_cross_project_ownership_rejected(self):
        job = self.job("review-tamper")
        path, record = self.saved_review(job)
        changed = copy.deepcopy(record)
        changed["review"]["state_version"] = 1
        write_json(path, changed)
        with self.assertRaisesRegex(ProtocolError, "integrity failed"):
            closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        changed = copy.deepcopy(record)
        changed["review"]["project"] = str(self.root / "other")
        changed["review"]["approval_sha256"] = digest({key: value for key, value in changed["review"].items() if key != "approval_sha256"})
        changed["record_sha256"] = digest({key: value for key, value in changed.items() if key != "record_sha256"})
        write_json(path, changed)
        with self.assertRaisesRegex(ProtocolError, "ownership changed"):
            closeout.build_closeout(self.campaign, evidence_builder=self.evidence)

    def test_live_amended_budget_not_legacy_manifest_limit(self):
        before = self.snapshot()
        report, manifest = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        self.assertEqual(400000000, report["budget"]["policy"]["max_tokens"])
        self.assertEqual(400000000 - 10, report["budget"]["remaining_tokens"])
        self.assertEqual(400000, report["campaign"]["original_manifest_token_limit"])
        self.assertEqual(59, report["budget"]["remaining_calls"])
        self.assertEqual(report["report_sha256"], manifest["report_sha256"])
        self.assertEqual(before, self.snapshot())

    def test_latest_native_execution_included_without_legacy_evidence_files(self):
        first, latest = self.job("old"), self.job("new-native")
        before = self.snapshot()
        report, manifest = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        trials = report["runs"][0]["candidate_trials"]
        self.assertEqual({first["job_id"], latest["job_id"]}, {row["job"]["job_id"] for row in trials})
        self.assertEqual(2, report["runs"][0]["candidate_execution_count"])
        self.assertEqual(2, len(manifest["entries"]))
        self.assertEqual(before, self.snapshot())
        self.assertEqual("development_evidence_report", report["report_kind"])
        self.assertIsNone(report["runs"][0]["final_candidate"])

    def test_failed_without_observation_preserved_not_scored(self):
        failed = self.job("failed", "failed")
        report, manifest = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        trial = report["runs"][0]["candidate_trials"][0]
        self.assertEqual(failed["job_id"], trial["job"]["job_id"])
        self.assertFalse(trial["has_observation"])
        self.assertIsNone(trial["evidence"]["candidate"]["trajectory_error"])
        self.assertEqual("failed", manifest["entries"][0]["status"])
        self.assertNotIn("synthetic-secret", str(report))

    def test_never_executed_cancelled_job_retained_without_calling_builder(self):
        self.job("cancelled", "cancelled")
        with patch.object(closeout, "_execution_presence", return_value="no_execution"):
            report, _ = closeout.build_closeout(self.campaign,
                evidence_builder=lambda *_: self.fail("Unexecuted job must not produce scored evidence"))
        trial = report["runs"][0]["candidate_trials"][0]
        self.assertEqual("no_execution", trial["evidence"]["reason"])
        self.assertIsNone(trial["evidence"]["candidate"]["trusted_rhs_queries"])
        self.assertIsNone(trial["evidence"]["comparison"]["development_threshold_met"])

    def test_active_jobs_mark_incomplete_do_not_score_or_recover(self):
        self.job("queued", "queued")
        with patch.object(closeout, "_execution_presence", return_value="not_terminal"):
            before = self.snapshot()
            report, _ = closeout.build_closeout(self.campaign,
                evidence_builder=lambda *_: self.fail("Must not execute or score active jobs"))
        self.assertFalse(report["runs"][0]["all_jobs_terminal"])
        self.assertEqual(before, self.snapshot())

    def test_source_tamper_propagates_instead_of_becoming_unscored_failure(self):
        self.job("artifact-tamper", "failed")
        with self.assertRaisesRegex(ProtocolError, "artifact changed"):
            closeout.build_closeout(self.campaign,
                evidence_builder=lambda *_: (_ for _ in ()).throw(ProtocolError("artifact changed")))

    def test_project_scope_and_returned_object_scope_rejected(self):
        self.job("candidate")
        def wrong(*args):
            result = self.evidence(*args)
            result["study_id"] = "S-other"
            return result
        with self.assertRaisesRegex(ProtocolError, "different object"):
            closeout.build_closeout(self.campaign, evidence_builder=wrong)
        changed = copy.deepcopy(self.manifest)
        changed["runs"][0]["project"] = str(self.root / "other-project")
        write_json(self.campaign / "campaign.json", changed)
        with self.assertRaisesRegex(ProtocolError, "ownership mismatch"):
            closeout.build_closeout(self.campaign, evidence_builder=self.evidence)

    def test_queue_projection_and_immutable_code_tamper_rejected(self):
        job = self.job("candidate")
        source = self.research / "revisions" / job["revision_id"] / "code" / "model.py"
        source.write_text("raise RuntimeError('tamper')\n", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "SHA-256"):
            closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        source.write_text("def solve():\n    return 'synthetic only'\n", encoding="utf-8")
        with closing(RevisionExecutionJobStore(self.research / "revision-execution-jobs.sqlite")) as queue:
            with queue._conn:
                queue._conn.execute("UPDATE revision_execution_jobs SET status='failed'")
        with self.assertRaisesRegex(ProtocolError, "integrity failed"):
            closeout.build_closeout(self.campaign, evidence_builder=self.evidence)

    def test_budget_input_tamper_and_missing_ledger_never_initialize(self):
        with closing(closeout._readonly_connection(self.ledger)) as db:
            with self.assertRaises(Exception):
                db.execute("UPDATE requests SET snapshot='{}'")
        with closing(AgentEvaluationBudget(self.ledger)) as budget:
            with budget.connection:
                budget.connection.execute("UPDATE requests SET snapshot='{}'")
        with self.assertRaisesRegex(ProtocolError, "snapshot digest changed"):
            closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        missing = self.root / "must-not-create.sqlite"
        with self.assertRaisesRegex(ProtocolError, "database is missing"):
            closeout._budget_snapshot(missing)
        self.assertFalse(missing.exists())

    def test_old_execution_versions_preserved_no_false_current_source_validation(self):
        versions = self.campaign / "execution-revisions"
        versions.mkdir()
        parent = file_hash(self.campaign / "campaign.json")
        for number in (1, 2):
            path = versions / f"{number:04d}.json"
            write_json(path, {"version": number, "campaign_sha256": file_hash(self.campaign / "campaign.json"),
                "parent_sha256": parent, "reason": "historical infrastructure revision",
                "implementation_sha256": {"historical-not-current.py": str(number)}})
            parent = file_hash(path)
        report, _ = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        self.assertEqual([1, 2], [item["version"] for item in report["execution_revisions"]])
        changed = read_json(versions / "0002.json")
        changed["parent_sha256"] = "bad"
        write_json(versions / "0002.json", changed)
        with self.assertRaisesRegex(ProtocolError, "chain changed"):
            closeout.build_closeout(self.campaign, evidence_builder=self.evidence)

    def test_late_state_result_rejected_not_reported_current(self):
        self.job("candidate")
        def stale(*args):
            value = self.evidence(*args)
            value["state_version"] = 44
            return value
        with self.assertRaisesRegex(ProtocolError, "Study changed"):
            closeout.build_closeout(self.campaign, evidence_builder=stale)

    def test_no_study_remains_no_study_not_operator_invented(self):
        self.study = {"exists": False, "study_id": None, "state_version": None, "claims_count": 0,
            "confirmation_consumed": False, "confirmation_eligible": False,
            "confirmation_refusal": "No Study: incomplete original proposal"}
        before = self.snapshot()
        report, manifest = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        self.assertIsNone(report["runs"][0]["study"]["study_id"])
        self.assertEqual([], report["runs"][0]["candidate_trials"])
        self.assertEqual([], manifest["entries"])
        self.assertEqual(before, self.snapshot())

    def test_report_exports_cases_and_refuses_existing_output(self):
        self.job("candidate")
        report, manifest = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        output = self.root / "new-report"
        receipt = closeout.write_closeout(report, manifest, output)
        self.assertEqual(report["report_sha256"], receipt["report_sha256"])
        with (output / "cases.csv").open(encoding="utf-8-sig", newline="") as stream:
            case = next(csv.DictReader(stream))
        self.assertEqual("synthetic-case", case["case_id"])
        self.assertEqual("12", case["trusted_rhs_queries"])
        before = self.snapshot()
        with self.assertRaisesRegex(ProtocolError, "overwrite"):
            closeout.write_closeout(report, manifest, output)
        self.assertEqual(before, self.snapshot())
        self.assertTrue((output / "files.json").exists())
        for name in ("trajectory-error.svg", "sampled-energy-drift.svg"):
            text = (output / name).read_text(encoding="utf-8")
            ET.fromstring(text)
            self.assertIn("log10", text)
            self.assertIn("固定 RK4", text)
            self.assertIn("dev", text)
            self.assertIn(report["created_at"], text)
            self.assertIn("冻结输出采样点", text)
        self.assertFalse(report["fair_complete_numeric_comparison"])

    def test_persisted_model_snapshots_and_ledger_same_input_required(self):
        with closing(AgentSessionStore(self.project)) as store:
            session = store.create_session("synthetic", model="synthetic")
            turn, _ = store.start_turn(session["session_id"], "synthetic only", "synthetic-once", actor="user", trace_id="trc_synthetic")
            value = {"messages": [{"role": "user", "content": "synthetic only"}], "request_limits": {"max_completion_tokens": 32}}
            with store._conn:
                save_input(store, turn["turn_id"], value)
        with closing(AgentEvaluationBudget(self.ledger)) as budget:
            budget.reserve(turn["turn_id"], value, 50)
            budget.settle(turn["turn_id"], 20)
        report, _ = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        model = report["runs"][0]["conversation"]
        self.assertEqual(digest(value), model["turns"][0]["input_snapshot_sha256"])
        self.assertTrue(model["turns"][0]["ledger_request_present"])
        with closing(AgentEvaluationBudget(self.ledger)) as budget:
            changed = {"messages": []}
            with budget.connection:
                from popper.core import canonical
                budget.connection.execute("UPDATE requests SET snapshot=?,sha256=? WHERE id=?",
                    (canonical(changed), digest(changed), turn["turn_id"]))
        with self.assertRaisesRegex(ProtocolError, "snapshots differ"):
            closeout.build_closeout(self.campaign, evidence_builder=self.evidence)

    def test_control_projection_uses_verified_side_not_as_new_candidate(self):
        candidate_job = self.job("candidate")
        control_id = self.revision("control", control=True)
        def with_control(*args):
            result = self.evidence(*args)
            result["control"] = {"observation_id": "OBS-control", "revision_id": control_id,
                "run_id": "RUN-control", "execution_status": "succeeded", "trajectory_error": 1.0,
                "cases": [], "systems": {}, "workers": []}
            return result
        report, manifest = closeout.build_closeout(self.campaign, evidence_builder=with_control)
        run = report["runs"][0]
        self.assertEqual(1, run["candidate_execution_count"])
        self.assertEqual(1, len(run["control_trials"]))
        self.assertEqual(candidate_job["job_id"], run["candidate_trials"][0]["job"]["job_id"])
        self.assertEqual({"candidate", "fixed_control"}, {item["role"] for item in manifest["entries"]})

    def test_zero_plot_has_explicit_zero_without_epsilon_measurement(self):
        self.job("candidate")
        report, _ = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        report["runs"][0]["candidate_trials"][0]["evidence"]["candidate"]["systems"]["harmonic"]["mean_trajectory_error"] = 0
        svg = closeout._system_plot(report, "mean_trajectory_error", "synthetic plot", "dimensionless")
        ET.fromstring(svg)
        self.assertIn('cx="320.00"', svg)
        self.assertIn("candidate: 0 (raw value)", svg)
        self.assertNotIn("1e-12 (raw value)", svg)

    def test_plot_missing_and_bad_values_are_not_scored(self):
        self.job("candidate")
        report, _ = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        candidate = report["runs"][0]["candidate_trials"][0]["evidence"]["candidate"]
        candidate["systems"]["harmonic"]["mean_trajectory_error"] = None
        svg = closeout._system_plot(report, "mean_trajectory_error", "synthetic plot", "dimensionless")
        self.assertIn("没有可绘制", svg)
        self.assertNotIn("candidate: 0 (raw value)", svg)
        candidate["systems"]["harmonic"]["mean_trajectory_error"] = float("nan")
        with self.assertRaisesRegex(ProtocolError, "finite nonnegative"):
            closeout._system_plot(report, "mean_trajectory_error", "synthetic plot", "dimensionless")

    def test_reporting_cannot_write_within_scientific_roots(self):
        report, manifest = closeout.build_closeout(self.campaign, evidence_builder=self.evidence)
        output = self.project / "must-not-create"
        with self.assertRaisesRegex(ProtocolError, "outside scientific"):
            closeout.write_closeout(report, manifest, output)
        self.assertFalse(output.exists())

    def test_real_prepared_six_projects_without_studies_remain_untouched(self):
        from tests import test_oscillator_campaign as preparation
        fixture = preparation.CampaignPreparationTests("test_six_independent_projects_same_material_no_studies")
        fixture.setUp()
        try:
            before = {str(path): file_hash(path) for path in fixture.campaign.rglob("*") if path.is_file()}
            with patch.object(closeout, "_study_snapshot", wraps=self.original_study_reader), \
                    patch.object(AgentSessionStore, "__init__", side_effect=AssertionError("Must not initialize a session DB")), \
                    patch.object(RevisionExecutionJobStore, "__init__", side_effect=AssertionError("Must not initialize a queue")), \
                    patch.object(closeout.RevisionStore, "__init__", side_effect=AssertionError("Must not create a revision directory")):
                report, manifest = closeout.build_closeout(fixture.campaign,
                    evidence_builder=lambda *_: self.fail("No Study or execution to score"))
            self.assertEqual(6, len(report["runs"]))
            self.assertTrue(all(run["study"]["study_id"] is None for run in report["runs"]))
            self.assertEqual([], manifest["entries"])
            self.assertEqual(before, {str(path): file_hash(path) for path in fixture.campaign.rglob("*") if path.is_file()})
        finally:
            fixture.tearDown()

    def retained_fixture(self, *, metering=True):
        from popper.research.workers.base import InputArtifact, JobSpec, WorkerReceipt
        from popper.research.evaluation_service import scoring_code_hash
        from tests.test_oscillator_broker import row
        job = self.job("retained")
        revision = closeout._immutable_revision(self.research / "revisions", job["revision_id"])
        rows = [{**row("synthetic-case"), "reference": [[.3, 0.0]] * 3}]
        write_json(self.project / "dev.json", rows)
        write_json(self.project / "public-dev.json", PACK.project_inputs(rows))
        write_json(self.project / "config.json", {"slot": 1})
        spec = JobSpec("RUN-retained:seed:0:execution-v1", revision["revision_id"], revision["manifest_sha256"],
            "D-synthetic", "model.py", inputs=(
                InputArtifact(str(self.project / "public-dev.json"), "instances.json", file_hash(self.project / "public-dev.json"), "development_features_without_labels"),
                InputArtifact(str(self.project / "config.json"), "config.json", file_hash(self.project / "config.json"), "frozen_intervention")),
            outputs=("outputs/trajectories-0.json",), timeout_seconds=5)
        worker = self.research / "jobs" / spec.job_id
        (worker / "workspace" / "outputs").mkdir(parents=True)
        payload = {"trajectories": [{"id": "synthetic-case", "states": [[.3, 0.0]] * 3}]}
        artifacts = {}
        if metering:
            rhs = {"kind": "host_metered_rhs_execution", "job_id": spec.job_id,
                "input_sha256": digest(PACK.project_inputs(rows)), "source_sha256": revision["files"],
                "config_sha256": digest({"slot": 1}), "payload": payload,
                "cases": [{"case_id": "synthetic-case", "budget": 256, "query_count": 4, "status": "succeeded"}]}
            for name, body in (("outputs/rhs-receipt.json", rhs), ("outputs/trajectories-0.json", payload)):
                path = worker / "workspace" / name
                write_json(path, body)
                artifacts[name] = file_hash(path)
        status = "succeeded" if metering else "implementation_failed"
        receipt = WorkerReceipt(spec.job_id, status, revision["revision_id"], revision["manifest_sha256"],
            artifacts, .2, "windows_low_integrity_metered_rhs").payload()
        write_json(worker / "receipt.json", receipt)
        write_json(worker / "job.json", {"job_id": spec.job_id, "status": status,
            "spec": spec.payload(), "receipt_sha256": file_hash(worker / "receipt.json")})
        ref = {"job_id": spec.job_id, "seed": 0, "status": status,
            "receipt_sha256": file_hash(worker / "receipt.json"), "trusted_rhs_available": metering}
        evidence = self.evidence(self.project, self.research, job["job_id"])
        side = evidence["candidate"]
        side.update(observation_id="OBS-retained" if metering else None, trajectory_error=0.0 if metering else None,
            trusted_rhs_queries=4 if metering else None, workers=[ref])
        sources = {"development_input": file_hash(self.project / "dev.json"), "scoring_service": scoring_code_hash(),
            "candidate:worker:0:manifest": file_hash(worker / "job.json")}
        evidence.update(source_digests=sources, run_id="RUN-retained")
        entry = {"campaign_run_id": "popper_loop-2", "arm": "popper_loop", "study_id": "S-synthetic", "state_version": 45,
            "project": str(self.project), "research": str(self.research), "job_id": job["job_id"],
            "run_id": "RUN-retained", "role": "candidate", "status": job["status"], "revision": revision,
            "source_digests": sources, "workers": [ref], "observation_id": side["observation_id"],
            "trajectory_error": side["trajectory_error"]}
        return entry, side, evidence

    def test_retained_reproduction_real_receipts_score_and_tamper(self):
        from evaluation import reproduce_development_closeout as reproduction
        entry, side, _ = self.retained_fixture()
        before = self.snapshot()
        result = reproduction._verified_entry(entry, side)
        self.assertEqual(0, result["score"])
        self.assertEqual(4, result["trusted_rhs_queries"])
        self.assertEqual(before, self.snapshot())
        path = self.research / "jobs" / entry["workers"][0]["job_id"] / "workspace/outputs/trajectories-0.json"
        write_json(path, {"trajectories": []})
        with self.assertRaisesRegex(ProtocolError, "artifact changed"):
            reproduction._verified_entry(entry, side)

    def test_retained_reproduction_missing_rhs_never_means_zero(self):
        from evaluation import reproduce_development_closeout as reproduction
        entry, side, _ = self.retained_fixture(metering=False)
        result = reproduction._verified_entry(entry, side)
        self.assertIsNone(result["score"])
        self.assertIsNone(result["trusted_rhs_queries"])
        self.assertFalse(result["trusted_rhs_complete"])
        self.assertEqual(0, result["trusted_rhs_queries_known_partial"])

    def test_bundle_reproduction_requires_all_candidates_and_frozen_files(self):
        from evaluation import reproduce_development_closeout as reproduction
        entry, side, evidence = self.retained_fixture()
        report, manifest = closeout.build_closeout(self.campaign, evidence_builder=lambda *_: evidence)
        output = self.root / "bundle"
        closeout.write_closeout(report, manifest, output)
        reproduced = reproduction.verify_retained(output)
        self.assertEqual(1, len(reproduced["entries"]))
        self.assertEqual(0, reproduced["entries"][0]["score"])
        self.assertTrue(reproduced["no_solver_reexecution"])
        with (output / "report.md").open("a", encoding="utf-8") as stream:
            stream.write("tamper")
        with self.assertRaisesRegex(ProtocolError, "file changed"):
            reproduction.load_bundle(output)

    def test_replay_existing_destination_and_unavailable_boundary_refuse(self):
        from evaluation import reproduce_development_closeout as reproduction
        existing = self.root / "already-executed"
        existing.mkdir()
        with patch.object(reproduction, "execute_metered", side_effect=AssertionError("No replay permitted")):
            with self.assertRaisesRegex(ProtocolError, "overwrite or resume"):
                reproduction.replay_isolated(self.root / "unused", existing)
            with patch.object(reproduction, "load_bundle", return_value=({}, {}, {"runs": []})), \
                    patch.object(reproduction, "require_windows_boundary", side_effect=ProtocolError("Windows boundary missing")):
                destination = self.root / "must-not-create"
                with self.assertRaisesRegex(ProtocolError, "boundary missing"):
                    reproduction.replay_isolated(self.root / "unused", destination)
                self.assertFalse(destination.exists())

    def legacy_failed_worker_fixture(self, *, actual_run=None):
        from popper.research.workers.base import JobSpec
        entry, side, _ = self.retained_fixture(metering=False)
        old_ref = entry["workers"][0]
        old_directory = self.research / "jobs" / old_ref["job_id"]
        old_manifest = read_json(old_directory / "job.json")
        actual_run = actual_run or "RUN-" + digest({"revision_id": entry["revision"]["revision_id"], "split": "dev"})[:24]
        payload = old_manifest["spec"] | {"idempotency_key": actual_run + ":seed:0:execution-v1"}
        spec = JobSpec.from_payload(payload)
        directory = self.research / "jobs" / spec.job_id
        directory.mkdir()
        receipt = read_json(old_directory / "receipt.json") | {"job_id": spec.job_id}
        write_json(directory / "receipt.json", receipt)
        write_json(directory / "job.json", {"job_id": spec.job_id, "status": receipt["status"],
            "spec": spec.payload(), "receipt_sha256": file_hash(directory / "receipt.json")})
        ref = old_ref | {"job_id": spec.job_id, "receipt_sha256": file_hash(directory / "receipt.json")}
        entry.update(status="failed", run_id=None, workers=[ref])
        entry["source_digests"]["candidate:worker:0:manifest"] = file_hash(directory / "job.json")
        return entry, side

    def test_legacy_failed_bundle_null_run_uses_exact_revision_bound_identity(self):
        from evaluation import reproduce_development_closeout as reproduction
        entry, side = self.legacy_failed_worker_fixture()
        before = self.snapshot()
        result = reproduction._verified_entry(entry, side)
        self.assertIsNone(result["score"])
        self.assertIsNone(result["trusted_rhs_queries"])
        self.assertIsNone(entry["run_id"], "Reader compatibility must not rewrite the old manifest")
        self.assertEqual(before, self.snapshot())

    def test_legacy_failed_bundle_wrong_worker_run_and_success_null_run_rejected(self):
        from evaluation import reproduce_development_closeout as reproduction
        entry, side = self.legacy_failed_worker_fixture(actual_run="RUN-other-object")
        with self.assertRaisesRegex(ProtocolError, "specification ownership"):
            reproduction._verified_entry(entry, side)
        entry["status"] = "succeeded"
        with self.assertRaisesRegex(ProtocolError, "specification ownership"):
            reproduction._verified_entry(entry, side)


class DevelopmentCloseoutPresentationTests(unittest.TestCase):
    """Pure retained-data exports: no session, research DB, solver or model."""

    def report(self, side, *, status="failed", control=None, root=None):
        root = root or Path(tempfile.gettempdir()) / "synthetic-closeout-presentation"
        trial = {"job": {"job_id": "JOBR-synthetic", "revision_id": "REV-synthetic", "status": status},
            "role": "candidate", "has_observation": bool(side.get("observation_id")),
            "evidence": {"candidate": copy.deepcopy(side), "control": copy.deepcopy(control),
                "comparison": {"development_threshold_met": False}}}
        report = {"created_at": "2026-10-05T00:00:00+00:00", "campaign": {"path": str(root / "retained" / "campaign.json")},
            "budget": {"request_count": 0, "used_tokens": 0, "policy": {"max_requests": 1, "max_tokens": 1},
                "remaining_calls": 1, "remaining_tokens": 1, "unresolved_requests": 0},
            "runs": [{"run_id": "synthetic", "arm": "synthetic", "project": str(root / "project"),
                "research": str(root / "research"), "candidate_execution_count": 1,
                "study": {"study_id": None, "claims_count": 0, "confirmation_consumed": False,
                    "confirmation_eligible": False}, "candidate_trials": [trial], "control_trials": []}],
            "limitations": list(closeout._LIMITATIONS)}
        report["report_sha256"] = digest(report)
        return report

    def side(self, cases, *, observation=None, trajectory=None, energy=None, failed_cases=0):
        return {"observation_id": observation, "trajectory_error": trajectory, "trusted_rhs_queries": 0,
            "cases": cases, "systems": {"harmonic": {"case_count": len(cases), "failed_cases": failed_cases,
                "mean_trajectory_error": trajectory, "mean_sampled_energy_drift_successful": energy}}}

    def export(self, side, *, status="failed", control=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.report(side, status=status, control=control, root=root)
            reproduction = {"report_sha256": report["report_sha256"], "entries": []}
            reproduction["manifest_sha256"] = digest(reproduction)
            output = root / "export"
            closeout.write_closeout(report, reproduction, output)
            with (output / "cases.csv").open(encoding="utf-8-sig", newline="") as stream:
                cases = list(csv.DictReader(stream))
            with (output / "systems.csv").open(encoding="utf-8-sig", newline="") as stream:
                systems = list(csv.DictReader(stream))
            return cases, systems, (output / "report.md").read_text(encoding="utf-8")

    def test_failed_without_observation_plot_keeps_control_and_marks_candidate_missing(self):
        side = self.side([{"id": "a", "system": "harmonic", "trajectory_error": None}])
        control = self.side([], observation="OBS-control", trajectory=.25, energy=.5)
        report = self.report(side, control=control)
        for field in ("mean_trajectory_error", "mean_sampled_energy_drift_successful"):
            with self.subTest(field=field):
                svg = closeout._system_plot(report, field, "synthetic", "dimensionless")
                tree = ET.fromstring(svg)
                ns = {"svg": "http://www.w3.org/2000/svg"}
                self.assertIn("无 Observation/无成绩", svg)
                self.assertIn("候选无成绩（按候选/系统）：1", svg)
                self.assertIn("候选与对照均无成绩而整行省略：0", svg)
                self.assertIn("fixed RK4 control:", svg)
                self.assertNotIn("candidate: 0 (raw value)", svg)
                measured_candidates = [node for node in tree.findall("svg:circle", ns)
                    if node.get("class") == "candidate" and node.find("svg:title", ns) is not None]
                self.assertEqual([], measured_candidates)

    def test_plot_distinguishes_candidate_missing_from_whole_row_missing(self):
        side = self.side([{"id": "a", "system": "harmonic", "trajectory_error": None}])
        svg = closeout._system_plot(self.report(side), "mean_trajectory_error", "synthetic", "dimensionless")
        self.assertIn("候选无成绩（按候选/系统）：1", svg)
        self.assertIn("候选与对照均无成绩而整行省略：1", svg)
        self.assertIn("没有可绘制", svg)
        side["systems"] = {}
        svg = closeout._system_plot(self.report(side), "mean_trajectory_error", "synthetic", "dimensionless")
        self.assertIn("候选无成绩（按候选/系统）：0", svg)
        self.assertIn("候选与对照均无成绩而整行省略：0", svg)
        self.assertIn("无系统记录候选：1", svg)

    def test_observation_with_missing_metric_is_not_labelled_no_observation(self):
        side = self.side([], observation="OBS-candidate", trajectory=.25)
        control = self.side([], observation="OBS-control", trajectory=.25, energy=.5)
        svg = closeout._system_plot(self.report(side, status="succeeded", control=control),
            "mean_sampled_energy_drift_successful", "synthetic", "dimensionless")
        self.assertIn("无成绩", svg)
        self.assertNotIn("无 Observation/无成绩", svg)
        self.assertIn("候选无成绩（按候选/系统）：1", svg)

    def test_failed_job_csv_separates_unscored_and_host_case_execution(self):
        side = self.side([{"id": str(index), "system": "harmonic", "trajectory_error": None,
            "energy_drift": None, "failed": None, "execution_status": "failed" if index == 0 else "not_completed"}
            for index in range(4)])
        cases, systems, markdown = self.export(side)
        system = systems[0]
        self.assertEqual("failed", system["job_execution_status"])
        self.assertEqual("0", system["scored_count"])
        self.assertEqual("0", system["failed_cases"])
        self.assertEqual("scored_cases_only", system["failed_cases_basis"])
        self.assertEqual(("0", "1", "3"), tuple(system[key] for key in
            ("host_execution_completed_count", "host_execution_failed_count", "host_execution_not_completed_count")))
        self.assertEqual("", system["mean_trajectory_error"])
        self.assertTrue(all(case["failed"] == "" and case["trajectory_error"] == "" for case in cases))
        self.assertEqual("failed", cases[0]["host_case_execution_status"])
        self.assertEqual("not_completed", cases[1]["host_case_execution_status"])
        self.assertTrue(all(case["job_execution_status"] == "failed" for case in cases))
        self.assertIn("failed_cases counts scored trajectory failures only", markdown)

    def test_scored_failure_counts_as_scored_despite_host_and_score_failure(self):
        side = self.side([
            {"id": "invalid", "system": "harmonic", "trajectory_error": 1000000., "energy_drift": None,
                "failed": True, "execution_status": "failed"},
            {"id": "valid", "system": "harmonic", "trajectory_error": .25, "energy_drift": .5,
                "failed": False, "execution_status": "succeeded"}],
            observation="OBS-candidate", trajectory=500000.125, energy=.5, failed_cases=1)
        cases, systems, _ = self.export(side, status="succeeded")
        system = systems[0]
        self.assertEqual("succeeded", system["job_execution_status"])
        self.assertEqual("2", system["scored_count"])
        self.assertEqual("1", system["failed_cases"])
        self.assertEqual(("1", "1", "0"), tuple(system[key] for key in
            ("host_execution_completed_count", "host_execution_failed_count", "host_execution_not_completed_count")))
        self.assertEqual("True", cases[0]["failed"])
        self.assertEqual("1000000.0", cases[0]["trajectory_error"])
        self.assertEqual("scored_trajectory_validity_only", cases[0]["failed_basis"])

    def test_missing_case_fields_or_partial_coverage_do_not_export_zero_counts(self):
        for label, cases, expected_scored, summary_count in (
            ("missing_host_status", [{"id": "a", "system": "harmonic", "trajectory_error": .25}], "1", 1),
            ("missing_score_field", [{"id": "a", "system": "harmonic"}], "", 1),
            ("partial_cases", [{"id": "a", "system": "harmonic", "trajectory_error": .25,
                "execution_status": "succeeded"}], "", 2),
            ("no_cases", [], "", 1)):
            with self.subTest(label=label):
                side = self.side(cases)
                side["systems"]["harmonic"]["case_count"] = summary_count
                exported_cases, systems, _ = self.export(side)
                system = systems[0]
                self.assertEqual(expected_scored, system["scored_count"])
                self.assertTrue(all(system[key] == "" for key in
                    ("host_execution_completed_count", "host_execution_failed_count", "host_execution_not_completed_count")))
                if label == "missing_host_status":
                    self.assertEqual("", exported_cases[0]["host_case_execution_status"])


if __name__ == "__main__":
    unittest.main()
