"""Portable artifact-only tests; no model calls, worker launches or campaign data."""
import copy
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, digest, file_hash, read_json, write_json
from popper.domains.oscillator_trajectory import PACK
from popper.research import development_evidence as evidence
from popper.research.execution_jobs import RevisionExecutionJobStore
from popper.research.revisions import RevisionStore
from popper.research.workers.base import InputArtifact, JobSpec, WorkerReceipt


class DevelopmentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project, self.run = self.root / "project", self.root / "research"
        self.project.mkdir()
        self.run.mkdir()
        self.rows = [{"id": system + "-case", "system": system, "omega": 1.0,
            "q0": 1.0, "v0": 0.0, "t_end": 2.0, "budget": 256,
            "times": [0.0, 1.0, 2.0], "reference": [[1.0, 0.0], [0.9, 0.1], [0.8, 0.2]]}
            for system in ("harmonic", "pendulum")]
        write_json(self.project / "dev.json", self.rows)
        write_json(self.project / "evaluation-policy.json", {"candidate_development_limit": 6})
        self.scoring_hash = evidence.scoring_code_hash()
        self.manifest = {"project": str(self.project), "scorer_id": PACK.evaluator_id,
            "scoring_code_sha256": self.scoring_hash, "study_id": "S-fixture",
            "control_hypothesis_id": "H-control", "repeat_values": [0],
            "metric": {"name": "trajectory_error", "direction": "min"}, "min_meaningful_effect": 0.02}
        self.state = {"ok": True, "manifest": self.manifest, "state_version": 45,
            "experiment_graph": {"integrity": {"ok": True}},
            "budget": {"cap": 9, "spent": 2, "reserved": 0, "available": 7}, "runs": [], "observations": []}
        self.core = {"input_hashes": {"dev.json": file_hash(self.project / "dev.json")},
                     "spec": {"provenance_files": ["evaluation-policy.json"]}}
        write_json(self.run / "research.json", self.manifest)
        self.features = self.run / "features.json"
        self.config = self.run / "solver.json"
        write_json(self.features, PACK.project_inputs(self.rows))
        write_json(self.config, {"opaque_fixture_configuration": True})
        self.binding = self.run / "oscillator-worker-binding.json"
        write_json(self.binding, {"kind": "windows_metered_rhs_v1", "research_sha256": file_hash(self.run / "research.json"), "protected_files": {}})
        self.queue_path = self.run / "revision-execution-jobs.sqlite"
        self.sides = {}
        self.patches = [patch.object(evidence, "Experiment"), patch.object(evidence, "ResearchStore"),
                        patch.object(evidence, "read_research_state", side_effect=lambda _: copy.deepcopy(self.state))]
        self.exp, self.store, self.readout = [item.start() for item in self.patches]
        self.exp.return_value.verify_inputs.return_value = self.core
        self.store.return_value.verify.return_value = {"ok": True}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def snapshot(self):
        return {str(path.relative_to(self.root)): ("file", file_hash(path)) if path.is_file() else ("dir", None)
                for path in self.root.rglob("*")}

    def side(self, name, *, legacy=False, failed=False, seeds=(0,), delta=0.1, missing_rhs=False, failed_cases=()):
        hypothesis = "H-control" if name == "control" else "H-" + name
        design = "D-" + name
        content = ("# fixture only; never executed\nvalue = " + repr(name) + "\n").encode()
        identity = {"hypothesis_id": hypothesis, "design_id": design, "actor": "agent"}
        revision_id = "REV-" + digest(identity)[:24]
        code_hash = hashlib.sha256(content).hexdigest()
        revision = {"revision_id": revision_id, "identity": identity, "status": "immutable", "files": {"solver.py": code_hash}}
        revision_reader = RevisionStore(self.run / "revisions")
        revision_reader.materialize(revision_id, revision, {"solver.py": content})
        revision_hash = file_hash(self.run / "revisions" / revision_id / "revision.json")
        run_id = "RUN-" + digest({"revision_id": revision_id, "split": "dev"})[:24]
        self.state["runs"].append({"run_id": run_id, "scope": "dev", "revision_id": revision_id,
                                  "design_id": design, "revision_manifest_sha256": revision_hash})
        queue = RevisionExecutionJobStore(self.queue_path)
        try:
            queued = queue.enqueue("fixture-" + name, revision_id)
            queue.claim(queued["job_id"])
            if failed:
                queue.finish(queued["job_id"], error="fixture process failed")
            else:
                queue.finish(queued["job_id"], run_id=run_id)
        finally:
            queue.close()
        request_id = "EVAL-" + digest(name)[:16]
        workers, predictions, scores = [], [], []
        for seed in seeds:
            tracks = []
            for row in self.rows:
                states = copy.deepcopy(row["reference"])
                for sample in states[1:]:
                    sample[0] += delta * (seed + 1)
                tracks.append({"id": row["id"], "states": None if row["id"] in failed_cases else states})
            payload = {"trajectories": tracks}
            key = f"{request_id}:seed:{seed}:dev-v1" if legacy else f"{run_id}:seed:{seed}:execution-v1"
            spec = JobSpec(key, revision_id, revision_hash, design, "solver.py",
                inputs=(InputArtifact(str(self.features), "instances.json", file_hash(self.features), "development_features_without_labels"),
                        InputArtifact(str(self.config), "solver.json", file_hash(self.config), "frozen_intervention")),
                outputs=(f"outputs/trajectories-{seed}.json",))
            directory = self.run / "jobs" / spec.job_id
            outputs = directory / "workspace" / "outputs"
            outputs.mkdir(parents=True)
            artifacts = {}
            if not failed:
                output = outputs / f"trajectories-{seed}.json"
                write_json(output, payload)
                artifacts[f"outputs/trajectories-{seed}.json"] = file_hash(output)
                predictions.append({"seed": seed, "sha256": file_hash(output)})
                scores.append(PACK.score(self.rows, payload, "trajectory_error"))
            rhs = {"kind": "host_metered_rhs_execution", "job_id": spec.job_id,
                "input_sha256": digest(PACK.project_inputs(self.rows)), "source_sha256": revision["files"],
                "config_sha256": digest(read_json(self.config)), "payload": payload if not failed else None,
                "cases": [{"case_id": row["id"], "budget": row["budget"], "query_count": 128,
                    "status": "failed" if failed or row["id"] in failed_cases else "succeeded"} for row in self.rows]}
            if not missing_rhs:
                write_json(outputs / "rhs-receipt.json", rhs)
                artifacts["outputs/rhs-receipt.json"] = file_hash(outputs / "rhs-receipt.json")
            status = "implementation_failed" if failed else "succeeded"
            receipt = WorkerReceipt(spec.job_id, status, revision_id, revision_hash, artifacts, 0.25,
                                    "windows_low_integrity_metered_rhs").payload()
            receipt["worker_binding_sha256"] = file_hash(self.binding)
            write_json(directory / "receipt.json", receipt)
            write_json(directory / "job.json", {"job_id": spec.job_id, "spec": spec.payload(), "status": status,
                                               "receipt_sha256": file_hash(directory / "receipt.json")})
            workers.append(directory)
        observation = None
        if not failed:
            evaluation = self.run / "evaluations" / request_id
            evaluation.mkdir(parents=True)
            request = {"request_id": request_id, "dataset_sha256": file_hash(self.project / "dev.json"),
                "scoring_code_sha256": self.scoring_hash, "evaluator_id": PACK.evaluator_id, "predictions": predictions}
            write_json(evaluation / "request.json", request)
            score = math.fsum(scores) / len(scores)
            write_json(evaluation / "response.json", {"request_id": request_id, "request_sha256": digest(request),
                       "mean": score, "scoring_code_sha256": self.scoring_hash})
            observation = {"observation_id": "OBS-" + name, "hypothesis_id": hypothesis, "design_id": design,
                "run_id": run_id, "scope": "dev", "scorer_id": PACK.evaluator_id, "value": score,
                "artifact_id": str(evaluation / "response.json"), "artifact_sha256": file_hash(evaluation / "response.json")}
            self.state["observations"].append(observation)
        side = {"job_id": queued["job_id"], "revision_id": revision_id, "run_id": run_id,
                "workers": workers, "observation": observation, "request_id": request_id}
        self.sides[name] = side
        return side

    def fixture(self, **candidate_options):
        seeds = candidate_options.get("seeds", (0,))
        self.manifest["repeat_values"] = list(seeds)
        self.state["manifest"] = copy.deepcopy(self.manifest)
        write_json(self.run / "research.json", self.manifest)
        write_json(self.binding, {"kind": "windows_metered_rhs_v1", "research_sha256": file_hash(self.run / "research.json"), "protected_files": {}})
        self.side("control", delta=0.2, seeds=seeds)
        return self.side("candidate", **candidate_options)

    def read(self):
        return evidence.build_development_evidence(self.project, self.run, self.sides["candidate"]["job_id"])

    def update_artifact(self, relative, transform):
        directory = self.sides["candidate"]["workers"][0]
        path = directory / "workspace" / relative
        value = read_json(path)
        transform(value)
        write_json(path, value)
        receipt = read_json(directory / "receipt.json")
        receipt["artifacts"][relative] = file_hash(path)
        write_json(directory / "receipt.json", receipt)
        manifest = read_json(directory / "job.json")
        manifest["receipt_sha256"] = file_hash(directory / "receipt.json")
        write_json(directory / "job.json", manifest)

    def update_request(self, transform):
        observation = self.sides["candidate"]["observation"]
        response_path = Path(observation["artifact_id"])
        path = response_path.parent / "request.json"
        request = read_json(path)
        transform(request)
        write_json(path, request)
        response = read_json(response_path)
        response["request_sha256"] = digest(request)
        write_json(response_path, response)
        observation["artifact_sha256"] = file_hash(response_path)

    def assert_rejected_unchanged(self, pattern):
        before = self.snapshot()
        with self.assertRaisesRegex(ProtocolError, pattern):
            self.read()
        self.assertEqual(before, self.snapshot())

    def test_native_latest_job_is_verified_without_worker_or_revision_constructor_or_writes(self):
        current = self.fixture()
        older = self.side("older", delta=0.9)
        before = self.snapshot()
        with patch.object(RevisionStore, "__init__", side_effect=AssertionError("must not construct RevisionStore")), \
                patch("popper.research.workers.local.LocalWorker.__init__", side_effect=AssertionError("must not construct Worker")):
            result = self.read()
        self.assertEqual(current["revision_id"], result["revision_id"])
        self.assertNotEqual(older["revision_id"], result["revision_id"])
        self.assertEqual(current["observation"]["value"], result["candidate"]["trajectory_error"])
        self.assertEqual((2, 2, 256), tuple(result["candidate"][key] for key in ("execution_cases", "trajectory_valid_cases", "trusted_rhs_queries")))
        self.assertEqual(2, result["current_budget"]["candidate_development"]["used"])
        self.assertEqual(before, self.snapshot())
        self.exp.assert_called_with(self.project, readonly=True)
        self.store.assert_called_with(self.run / "research.sqlite", readonly=True)
        encoded = json.dumps(result)
        self.assertNotIn('"reference"', encoded)
        self.assertNotIn('"payload"', encoded)
        self.assertNotIn('"spec"', encoded)
        self.assertEqual(digest({key: value for key, value in result.items() if key != "evidence_sha256"}), result["evidence_sha256"])

    def test_legacy_scoring_request_key_is_supported(self):
        self.fixture(legacy=True)
        result = self.read()
        self.assertEqual("OBS-candidate", result["candidate"]["observation_id"])
        self.assertEqual(2, result["candidate"]["total_cases"])

    def test_multiple_seeds_are_scored_and_counted_once_per_distinct_revision(self):
        self.fixture(seeds=(0, 7))
        result = self.read()
        self.assertEqual(4, result["candidate"]["scored_cases"])
        self.assertEqual({0, 7}, {case["seed"] for case in result["candidate"]["cases"]})
        self.assertEqual(512, result["candidate"]["trusted_rhs_queries"])
        self.assertEqual(1, result["current_budget"]["candidate_development"]["used"])

    def test_failed_execution_without_observation_never_fabricates_scores(self):
        self.fixture(failed=True)
        result = self.read()
        self.assertEqual("failed", result["candidate"]["execution_status"])
        for key in ("trajectory_error", "observation_id"):
            self.assertIsNone(result["candidate"][key])
        self.assertEqual(0, result["candidate"]["scored_cases"])
        self.assertEqual(256, result["candidate"]["trusted_rhs_queries"])
        self.assertIsNone(result["comparison"]["relative_improvement"])
        self.assertFalse(result["comparison"]["development_threshold_met"])
        self.assertTrue(all(case["trajectory_error"] is None for case in result["candidate"]["cases"]))

    def test_missing_failed_rhs_receipt_does_not_invent_queries(self):
        self.fixture(failed=True, missing_rhs=True)
        result = self.read()
        self.assertIsNone(result["candidate"]["trusted_rhs_queries"])
        self.assertEqual(0, result["candidate"]["trusted_rhs_queries_known_partial"])
        self.assertFalse(result["candidate"]["trusted_rhs_complete"])
        self.assertEqual(0.25, result["candidate"]["elapsed_seconds"])
        self.assertFalse(result["candidate"]["workers"][0]["trusted_rhs_available"])
        self.assertIsNone(result["candidate"]["trajectory_error"])

    def test_finite_large_errors_are_negative_results_not_execution_failures(self):
        self.fixture(delta=70000)
        result = self.read()
        self.assertTrue(math.isfinite(result["candidate"]["trajectory_error"]))
        self.assertGreater(result["candidate"]["trajectory_error"], 50000)
        self.assertEqual("succeeded", result["candidate"]["execution_status"])
        self.assertFalse(result["comparison"]["development_threshold_met"])

    def test_scored_failed_case_retains_frozen_penalty_and_null_energy(self):
        self.fixture(failed_cases=("harmonic-case",))
        result = self.read()
        failed = next(case for case in result["candidate"]["cases"] if case["id"] == "harmonic-case")
        self.assertTrue(failed["failed"])
        self.assertEqual(1000000, failed["trajectory_error"])
        self.assertIsNone(failed["energy_drift"])
        self.assertEqual(1, result["candidate"]["trajectory_valid_cases"])

    def test_fake_rhs_budget_is_rejected_even_after_rehashing_receipts(self):
        self.fixture()
        self.update_artifact("outputs/rhs-receipt.json", lambda value: value["cases"][0].update(budget=10000))
        self.assert_rejected_unchanged("count/status")

    def test_fake_rhs_count_types_and_over_budget_are_rejected(self):
        for count in (True, -1, 257, 128.0, "128"):
            with self.subTest(count=count):
                if not self.sides:
                    self.fixture()
                self.update_artifact("outputs/rhs-receipt.json", lambda value: value["cases"][0].update(query_count=count))
                self.assert_rejected_unchanged("count/status")

    def test_cross_job_rhs_is_rejected(self):
        self.fixture()
        self.update_artifact("outputs/rhs-receipt.json", lambda value: value.update(job_id="JOB-other"))
        self.assert_rejected_unchanged("ownership/input/code")

    def test_rhs_source_and_input_and_config_mismatch_are_rejected(self):
        self.fixture()
        for field in ("source_sha256", "input_sha256", "config_sha256"):
            with self.subTest(field=field):
                directory = self.sides["candidate"]["workers"][0]
                original = read_json(directory / "workspace/outputs/rhs-receipt.json")
                self.update_artifact("outputs/rhs-receipt.json", lambda value: value.update({field: "0" * 64}))
                self.assert_rejected_unchanged("ownership/input/code")
                self.update_artifact("outputs/rhs-receipt.json", lambda value: (value.clear(), value.update(original)))

    def test_feature_file_hash_tamper_is_rejected(self):
        self.fixture()
        features = read_json(self.features)
        features[0]["omega"] = 99.0
        write_json(self.features, features)
        self.assert_rejected_unchanged("artifact changed.*features")

    def test_rehashed_feature_rows_cannot_replace_frozen_development_inputs(self):
        self.fixture()
        features = read_json(self.features)
        features[0]["omega"] = 99.0
        write_json(self.features, features)
        for directory in self.sides["candidate"]["workers"]:
            manifest = read_json(directory / "job.json")
            manifest["spec"]["inputs"][0]["sha256"] = file_hash(self.features)
            write_json(directory / "job.json", manifest)
        self.assert_rejected_unchanged("feature rows")

    def test_output_hash_tamper_is_rejected(self):
        self.fixture()
        path = self.sides["candidate"]["workers"][0] / "workspace/outputs/trajectories-0.json"
        write_json(path, {"trajectories": []})
        self.assert_rejected_unchanged("Worker artifact changed")

    def test_rehashed_output_is_still_bound_to_scoring_predictions(self):
        self.fixture()
        self.update_artifact("outputs/trajectories-0.json", lambda value: value["trajectories"][0]["states"][1].__setitem__(0, 1000))
        self.assert_rejected_unchanged("scoring prediction binding")

    def test_scoring_prediction_hash_tamper_is_rejected(self):
        self.fixture()
        self.update_request(lambda request: request["predictions"][0].update(sha256="0" * 64))
        self.assert_rejected_unchanged("scoring prediction binding")

    def test_coherently_forged_score_is_rejected_by_rescoring(self):
        self.fixture()
        observation = self.sides["candidate"]["observation"]
        response_path = Path(observation["artifact_id"])
        response = read_json(response_path)
        response["mean"] = 0.0
        write_json(response_path, response)
        observation["value"], observation["artifact_sha256"] = 0.0, file_hash(response_path)
        self.assert_rejected_unchanged("rescore differs")

    def test_scoring_response_hash_tamper_is_rejected(self):
        self.fixture()
        write_json(Path(self.sides["candidate"]["observation"]["artifact_id"]), {})
        self.assert_rejected_unchanged("artifact changed.*evaluation")

    def test_duplicate_rhs_case_cannot_hide_a_missing_case(self):
        self.fixture()
        self.update_artifact("outputs/rhs-receipt.json", lambda value: value["cases"].__setitem__(1, copy.deepcopy(value["cases"][0])))
        self.assert_rejected_unchanged("case identities")

    def test_duplicate_scoring_seed_is_rejected(self):
        self.fixture()
        self.update_request(lambda request: request["predictions"].append(copy.deepcopy(request["predictions"][0])))
        self.assert_rejected_unchanged("duplicate scoring seeds")

    def test_wrong_registered_seed_coverage_is_rejected(self):
        self.fixture()
        self.state["manifest"]["repeat_values"] = [0, 7]
        self.assert_rejected_unchanged("seed coverage")

    def test_run_and_observation_must_belong_to_this_revision_and_development_scope(self):
        self.fixture()
        observation = self.sides["candidate"]["observation"]
        observation["design_id"] = "D-other"
        self.assert_rejected_unchanged("Observation ownership/scope")
        observation["design_id"] = "D-candidate"
        target = next(row for row in self.state["runs"] if row["run_id"] == observation["run_id"])
        target["scope"] = "confirmation"
        self.assert_rejected_unchanged("Run ownership/scope")

    def test_revision_code_tamper_is_rejected(self):
        self.fixture()
        path = self.run / "revisions" / self.sides["candidate"]["revision_id"] / "code/solver.py"
        path.write_text("tampered = True\n", encoding="utf-8")
        self.assert_rejected_unchanged("CodeRevision.*SHA-256")

    def test_stale_research_version_is_rejected_without_writes(self):
        self.fixture()
        initial = copy.deepcopy(self.state)
        changed = {**initial, "state_version": 46}
        self.readout.side_effect = [initial, changed]
        self.assert_rejected_unchanged("changed while reading")

    def test_stale_execution_queue_is_rejected_without_extra_writes(self):
        self.fixture()
        original = evidence._queue
        count = 0

        def read_queue(run):
            nonlocal count
            jobs, sha = original(run)
            count += 1
            return jobs, "0" * 64 if count == 2 else sha

        with patch.object(evidence, "_queue", side_effect=read_queue):
            self.assert_rejected_unchanged("changed while reading")

    def test_queue_event_tamper_is_rejected(self):
        self.fixture()
        queue = RevisionExecutionJobStore(self.queue_path)
        try:
            with queue._conn:
                queue._conn.execute("UPDATE revision_execution_job_events SET payload='{}' WHERE job_id=?", (self.sides["candidate"]["job_id"],))
        finally:
            queue.close()
        self.assert_rejected_unchanged("queue integrity")

    def test_worker_binding_receipt_cannot_be_replaced_by_an_unbound_backend(self):
        self.fixture()
        directory = self.sides["candidate"]["workers"][0]
        receipt = read_json(directory / "receipt.json")
        receipt["worker_binding_sha256"] = "0" * 64
        write_json(directory / "receipt.json", receipt)
        manifest = read_json(directory / "job.json")
        manifest["receipt_sha256"] = file_hash(directory / "receipt.json")
        write_json(directory / "job.json", manifest)
        self.assert_rejected_unchanged("Worker binding mismatch")

    def test_declared_protected_source_change_is_rejected(self):
        self.fixture()
        protected = self.run / "reference-private.json"
        write_json(protected, {"private_reference": [1, 2, 3]})
        binding = read_json(self.binding)
        binding["protected_files"] = {str(protected): file_hash(protected)}
        write_json(self.binding, binding)
        write_json(protected, {"private_reference": [4, 5, 6]})
        self.assert_rejected_unchanged("changed.*protected_source")

    def test_duplicate_trajectory_is_rejected_even_with_consistent_outer_hashes(self):
        self.fixture()
        output_name = "outputs/trajectories-0.json"
        self.update_artifact(output_name, lambda value: value["trajectories"].__setitem__(1, copy.deepcopy(value["trajectories"][0])))
        directory = self.sides["candidate"]["workers"][0]
        self.update_artifact("outputs/rhs-receipt.json", lambda value: value.update(payload=read_json(directory / "workspace" / output_name)))
        self.update_request(lambda request: request["predictions"][0].update(sha256=file_hash(directory / "workspace" / output_name)))
        self.assert_rejected_unchanged("repeated trajectory ID")

    def test_artifact_change_after_rescoring_is_caught_before_return(self):
        self.fixture()
        output = self.sides["candidate"]["workers"][0] / "workspace/outputs/trajectories-0.json"
        original = PACK.diagnostics
        modified = False

        def rescore(rows, payload):
            nonlocal modified
            result = original(rows, payload)
            if not modified:
                write_json(output, {"trajectories": []})
                modified = True
            return result

        # The simulated adversary changes a source; the reader itself performs no
        # writes. Its final source check must reject the previously computed value.
        with patch.object(PACK, "diagnostics", side_effect=rescore):
            with self.assertRaisesRegex(ProtocolError, "changed while reading"):
                self.read()

    def test_missing_revision_directory_is_rejected_without_creating_worker_or_revision_roots(self):
        queue = RevisionExecutionJobStore(self.queue_path)
        try:
            job = queue.enqueue("missing-revision", "REV-" + "1" * 24)
            queue.claim(job["job_id"])
            queue.finish(job["job_id"], error="source never created")
        finally:
            queue.close()
        before = self.snapshot()
        with self.assertRaisesRegex(ProtocolError, "immutable revisions missing"):
            evidence.build_development_evidence(self.project, self.run, job["job_id"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.run / "jobs").exists())
        self.assertFalse((self.run / "revisions").exists())

    def test_missing_queue_does_not_create_database_worker_or_revision_directory(self):
        before = self.snapshot()
        with self.assertRaises(FileNotFoundError):
            evidence.build_development_evidence(self.project, self.run, "JOBR-missing")
        self.assertEqual(before, self.snapshot())
        for name in ("jobs", "revisions", "revision-execution-jobs.sqlite"):
            self.assertFalse((self.run / name).exists())

    def test_nonterminal_job_is_not_reported_as_completed(self):
        self.fixture()
        queue = RevisionExecutionJobStore(self.queue_path)
        try:
            pending = queue.enqueue("pending-candidate", self.sides["candidate"]["revision_id"])
        finally:
            queue.close()
        with self.assertRaisesRegex(ProtocolError, "terminal Job"):
            evidence.build_development_evidence(self.project, self.run, pending["job_id"])


if __name__ == "__main__":
    unittest.main()
