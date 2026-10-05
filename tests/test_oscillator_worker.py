import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from popper import sandbox
from popper.core import ProtocolError, file_hash, initialize, read_json, write_json
from popper.domains.oscillator_trajectory import PACK
from popper.research.controller import ResearchController
from popper.research.oscillator_study import baseline_rk4
from popper.research.oscillator_worker import MeteredOscillatorWorker, freeze_binding, controller_for_run
from popper.core import Experiment
from popper.research.revisions import RevisionStore, CodeEdit
from popper.research.readout import read_research_state
from popper.research.workers.base import JobSpec, InputArtifact, TERMINAL_JOB_STATUSES
from tests.test_oscillator_broker import row


@unittest.skipUnless(os.name == "nt", "Windows domain Worker")
class OscillatorWorkerTests(unittest.TestCase):
    def setUp(self):
        if not sandbox.available() or not sandbox.seal_read_available():
            self.skipTest("Windows read isolation unavailable")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project, self.run = self.root / "project", self.root / "research"
        self.project.mkdir()
        self.module = Path(__file__).resolve().parents[1] / "popper/research/oscillator_baseline.py"
        shutil.copyfile(self.module, self.project / "model.py")
        self.rows = [row()]
        private = []
        for split, frequency in (("train", 1), ("dev", 2), ("test", 3)):
            case = {**row(), "id": split, "omega": frequency}
            states, _ = baseline_rk4(case)
            path = self.project / f"{split}.json"
            write_json(path, [{**case, "reference": states}])
            private.append(path)
        write_json(self.project / "experiment.json", {
            "name": "isolated metered test", "objective": "simulation only",
            "entrypoint": "model.py", "code_files": ["model.py"],
            "train": "train.json", "dev": "dev.json", "test": "test.json",
            "baseline": {"arm": "rk4"}, "candidates": [{"arm": "agent"}],
            "metric": {"name": "trajectory_error", "direction": "min"},
            "seeds": [0], "budget": 1, "timeout_seconds": 20,
            "min_improvement": 0.01})
        initialize(self.project)
        ResearchController.initialize(self.project, self.run)
        state = read_research_state(self.run)
        manifest = read_json(self.run / "research.json")
        h = manifest["candidate_hypothesis_ids"][0]
        d = next(d["design_id"] for d in state["designs"] if d["hypothesis_id"] == h)
        store = RevisionStore(self.run / "revisions")
        self.revision = store.create(self.project, h, d,
            [CodeEdit("model.py", (self.project / "model.py").read_text() + "\n# Agent test\n",
                      file_hash(self.project / "model.py"))], actor="agent")
        write_json(self.root / "instances.json", self.rows)
        write_json(self.root / "config.json", {"arm": "agent"})
        scorer = Path(__file__).resolve().parents[1] / "popper/domains/oscillator_trajectory.py"
        binding = freeze_binding(self.run, [*private, scorer])
        protected = read_json(binding["path"])["protected_files"]
        for cache in scorer.parent.joinpath("__pycache__").glob(scorer.stem + ".*.pyc"):
            self.assertIn(str(cache.resolve()), protected)
        self.worker = MeteredOscillatorWorker(self.run / "jobs", store, binding["path"])
        self.processes = []
        spawn = self.worker._spawn_supervisor
        def captured_spawn(job_id):
            process = spawn(job_id)
            self.processes.append(process)
            return process
        self.worker._spawn_supervisor = captured_spawn
        self.spec = JobSpec("oscillator-native-worker", self.revision["revision_id"],
            file_hash(store.path(self.revision["revision_id"]) / "revision.json"), d, "model.py",
            inputs=(InputArtifact(str(self.root / "instances.json"), "instances.json",
                                  file_hash(self.root / "instances.json"), "development_features_without_labels"),
                    InputArtifact(str(self.root / "config.json"), "solver.json",
                                  file_hash(self.root / "config.json"), "frozen_intervention")),
            outputs=("outputs/trajectories-0.json",), timeout_seconds=20, require_edit_coverage=True)

    def tearDown(self):
        for process in self.processes:
            process.wait(timeout=10)
        self.temp.cleanup()

    def test_detached_execution_reconnect_and_trusted_artifacts(self):
        self.worker.launch(self.spec)
        deadline = time.monotonic() + 30
        while self.worker.poll(self.spec.job_id)["status"] not in TERMINAL_JOB_STATUSES:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)
        receipt = self.worker.collect(self.spec.job_id)
        self.assertEqual("succeeded", receipt["status"],
                         (self.run / "jobs" / self.spec.job_id / "stderr.log").read_text() if receipt["status"] != "succeeded" else "")
        self.assertEqual("windows_low_integrity_metered_rhs", receipt["execution_backend"])
        self.assertTrue(receipt["execution_gate"]["passed"])
        execution = read_json(self.run / "jobs" / self.spec.job_id / "workspace/outputs/rhs-receipt.json")
        self.assertEqual(256, execution["cases"][0]["query_count"])
        expected, _ = baseline_rk4(row())
        self.assertEqual(expected, execution["payload"]["trajectories"][0]["states"])
        restarted = MeteredOscillatorWorker(self.run / "jobs", self.run / "revisions", self.run / "oscillator-worker-binding.json")
        self.assertEqual(receipt, restarted.run(self.spec))
        self.assertEqual(1, len(restarted.list_jobs()))

    def test_changed_reference_binding_rejects_before_launch(self):
        (self.project / "test.json").write_text("[]")
        with self.assertRaises(ProtocolError):
            self.worker._verify_binding()

    def test_controller_records_actual_observation_and_replayable_predictions(self):
        controller = controller_for_run(self.run)
        delegate = controller.worker.delegate
        spawn = delegate._spawn_supervisor
        def captured(job_id):
            process = spawn(job_id)
            self.processes.append(process)
            return process
        delegate._spawn_supervisor = captured
        try:
            result = controller.execute_revision(self.revision["revision_id"])
            self.assertEqual("succeeded", result["status"])
            self.assertIsNotNone(result["observation"]["observation_id"])
            self.assertTrue(all(r["execution_backend"] == "windows_low_integrity_metered_rhs"
                                for r in result["receipts"]))
        finally:
            controller.close()
        experiment = Experiment(self.project)
        try:
            self.assertEqual("verified", experiment.replay()["status"])
        finally:
            experiment.close()


if __name__ == "__main__":
    unittest.main()
