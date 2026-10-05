import copy
import runpy
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from popper.core import ProtocolError, digest, file_hash, initialize, read_json, write_json
from popper.research import agent_revisions
from popper.research.execution_jobs import RevisionExecutionJobStore
from popper.tool_results import summarize_tool_result


class RevisionJobMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.project_fixture("project")
        self.run = self.root / "research"
        self.run.mkdir()
        self.control = "H-registered-baseline"
        write_json(self.run / "research.json", {"control_hypothesis_id": self.control})
        self.ws = SimpleNamespace(evaluation_budget_path=self.root / "budget.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def project_fixture(self, name, *, registered=True, limit=6):
        project = self.root / name
        project.mkdir()
        example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        shutil.copyfile(example / "model.py", project / "model.py")
        runpy.run_path(str(example / "generate_data.py"))["generate"](project)
        spec = read_json(example / "experiment.json")
        if registered:
            spec["provenance_files"] = ["evaluation-policy.json"]
        write_json(project / "experiment.json", spec)
        write_json(project / "evaluation-policy.json", {"candidate_development_limit": limit})
        initialize(project)
        return project

    def revision_fixture(self, name, *, control=False):
        identity = {"hypothesis_id": self.control if control else "H-" + name,
                    "design_id": "D-" + name, "actor": "agent"}
        revision_id = "REV-" + digest(identity)[:24]
        path = self.run / "revisions" / revision_id
        path.mkdir(parents=True)
        write_json(path / "revision.json", {"revision_id": revision_id,
                   "identity": identity, "status": "immutable", "files": {}})
        return revision_id

    def file_snapshot(self):
        return {str(path.relative_to(self.root)): file_hash(path)
                for path in self.root.rglob("*") if path.is_file()}

    def test_counts_distinct_non_control_revisions_in_every_queue_status_readonly(self):
        queue_path = self.run / "revision-execution-jobs.sqlite"
        jobs = RevisionExecutionJobStore(queue_path)
        try:
            for status in ("succeeded", "failed", "cancelled", "queued", "interrupted", "running"):
                revision_id = self.revision_fixture(status)
                job = jobs.enqueue(status, revision_id)
                if status in {"succeeded", "failed", "interrupted", "running"}:
                    jobs.claim(job["job_id"])
                if status == "succeeded":
                    jobs.finish(job["job_id"], run_id="RUN-success")
                elif status == "failed":
                    jobs.finish(job["job_id"], error="fixture failure")
                    jobs.enqueue("same-failed-revision", revision_id)
                elif status == "cancelled":
                    jobs.cancel(job["job_id"], "fixture cancellation")
                elif status == "interrupted":
                    jobs.interrupt_running()
            jobs.enqueue("control", self.revision_fixture("control", control=True))
            before_jobs, before_integrity = jobs.list(), jobs.verify()
        finally:
            jobs.close()
        before_files = self.file_snapshot()
        result = agent_revisions._candidate_development_budget(self.ws, self.run, self.project)
        self.assertEqual({"scope": "candidate_development",
                          "unit": "distinct_non_control_revisions_with_execution_jobs",
                          "limit": 6, "used": 6, "remaining": 0}, result)
        self.assertEqual(before_files, self.file_snapshot())
        readonly = RevisionExecutionJobStore.readonly(queue_path)
        try:
            self.assertEqual(before_jobs, readonly.list())
            self.assertEqual(before_integrity, readonly.verify())
        finally:
            readonly.close()

    def test_missing_queue_reports_zero_without_creating_database_or_revisions(self):
        before = self.file_snapshot()
        with patch.object(RevisionExecutionJobStore, "readonly", side_effect=AssertionError("no queue")):
            result = agent_revisions._candidate_development_budget(self.ws, self.run, self.project)
        self.assertEqual((6, 0, 6), (result["limit"], result["used"], result["remaining"]))
        self.assertFalse((self.run / "revision-execution-jobs.sqlite").exists())
        self.assertFalse((self.run / "revisions").exists())
        self.assertEqual(before, self.file_snapshot())

    def test_scope_requires_evaluation_ledger_and_policy(self):
        with patch.object(agent_revisions, "Experiment", side_effect=AssertionError("out of scope")):
            self.assertIsNone(agent_revisions._candidate_development_budget(
                SimpleNamespace(evaluation_budget_path=None), self.run, self.project))
            self.assertIsNone(agent_revisions._candidate_development_budget(
                self.ws, self.run, self.root / "project-without-policy"))

    def test_policy_must_be_frozen_and_registered(self):
        unregistered = self.project_fixture("unregistered", registered=False)
        with self.assertRaisesRegex(ProtocolError, "冻结"):
            agent_revisions._candidate_development_budget(self.ws, self.run, unregistered)
        write_json(self.project / "evaluation-policy.json", {"candidate_development_limit": 5})
        with self.assertRaisesRegex(ProtocolError, "已注册输入发生修改"):
            agent_revisions._candidate_development_budget(self.ws, self.run, self.project)

    def test_invalid_frozen_limits_are_rejected(self):
        for index, limit in enumerate((True, 0, 7, "6")):
            with self.subTest(limit=limit):
                project = self.project_fixture("invalid-" + str(index), limit=limit)
                with self.assertRaisesRegex(ProtocolError, "限制不合法"):
                    agent_revisions._candidate_development_budget(self.ws, self.run, project)

    def test_tampered_queue_is_rejected_without_changing_evidence(self):
        path = self.run / "revision-execution-jobs.sqlite"
        jobs = RevisionExecutionJobStore(path)
        try:
            job = jobs.enqueue("candidate", self.revision_fixture("candidate"))
            with jobs._conn:
                jobs._conn.execute("UPDATE revision_execution_job_events SET payload='{}' WHERE job_id=?",
                                   (job["job_id"],))
        finally:
            jobs.close()
        before = self.file_snapshot()
        with self.assertRaisesRegex(ProtocolError, "queue integrity"):
            agent_revisions._candidate_development_budget(self.ws, self.run, self.project)
        self.assertEqual(before, self.file_snapshot())

    def test_missing_revision_directory_is_rejected_without_creating_it(self):
        jobs = RevisionExecutionJobStore(self.run / "revision-execution-jobs.sqlite")
        try:
            jobs.enqueue("missing", "REV-" + "1" * 24)
        finally:
            jobs.close()
        before = self.file_snapshot()
        with self.assertRaisesRegex(ProtocolError, "no immutable revisions"):
            agent_revisions._candidate_development_budget(self.ws, self.run, self.project)
        self.assertFalse((self.run / "revisions").exists())
        self.assertEqual(before, self.file_snapshot())

    def test_job_result_preserves_source_version_kernel_budget_and_observation_scope(self):
        budget = {"family_id": "F-existing", "cap": 9, "reserved": 0, "spent": 5, "available": 4}
        state = {"state_version": 45, "budget": budget, "runs": [],
                 "manifest": {"metric": {"name": "fixture_metric", "direction": "min"}},
                 "observations": [{"observation_id": "OBS-current", "run_id": "RUN-current",
                    "scope": "dev", "value": 7, "design_id": "D-current"},
                    {"observation_id": "OBS-other", "run_id": "RUN-other",
                     "scope": "dev", "value": 8, "design_id": "D-current"}]}
        payload = {"job": {"job_id": "JOBR-" + "1" * 24, "revision_id": "REV-current",
                           "run_id": "RUN-current", "status": "succeeded"}}
        before_state, before_payload = copy.deepcopy(state), copy.deepcopy(payload)
        ws = SimpleNamespace(evaluation_budget_path=None, ws_lock=threading.RLock(),
                             revision_job_payload=lambda job_id: payload)
        call = {"tool_name": "research.revision_job",
                "arguments": {"study_id": "S-" + "1" * 16, "job_id": payload["job"]["job_id"]}}
        def query(source):
            with patch.object(agent_revisions, "_run", return_value=self.run), \
                    patch.object(agent_revisions, "preview", return_value={"ok": True}), \
                    patch.object(agent_revisions, "read_research_state", return_value=source), \
                    patch.object(agent_revisions, "RevisionStore") as revisions, \
                    patch.object(agent_revisions, "LocalWorker") as worker:
                revisions.return_value.verify.return_value = {"identity": {"design_id": "D-current"}}
                worker.return_value.list_jobs.return_value = []
                return agent_revisions.execute(ws, call, self.project)

        result = query(state)
        self.assertEqual(45, result["state_version"])
        self.assertEqual(budget, result["budget"])
        self.assertEqual(["OBS-current"], [o["observation_id"] for o in result["observations"]])
        self.assertEqual("min", result["observations"][0]["direction"])
        self.assertNotIn("candidate_development_budget", result)
        self.assertEqual(before_state, state)
        self.assertEqual(before_payload, payload)

        ws.evaluation_budget_path = self.ws.evaluation_budget_path
        evaluation_result = query(state)
        self.assertEqual((6, 0, 6), tuple(evaluation_result["candidate_development_budget"][k]
                                        for k in ("limit", "used", "remaining")))
        with_large_details = {**evaluation_result, "content": "retained detail\n" * 5000}
        summary = summarize_tool_result(with_large_details, "tcl-fixture")
        for field in ("state_version", "budget", "candidate_development_budget", "observations"):
            self.assertEqual(evaluation_result[field], summary[field])
        self.assertIn("details_reference", summary["content"])
        self.assertEqual(("OBS-current", 7, "min"), tuple(summary["observations"][0][k]
                                                        for k in ("observation_id", "value", "direction")))

        legacy_state = copy.deepcopy(state)
        legacy_state.pop("state_version")
        legacy_state.pop("budget")
        legacy_state["manifest"]["metric"].pop("direction")
        ws.evaluation_budget_path = None
        legacy_result = query(legacy_state)
        self.assertNotIn("state_version", legacy_result)
        self.assertNotIn("budget", legacy_result)
        self.assertNotIn("direction", legacy_result["observations"][0])


if __name__ == "__main__":
    unittest.main()
