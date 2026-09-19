import copy
import importlib.util
import inspect
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper import sandbox
from popper.core import ProtocolError, digest, file_hash, initialize, read_json, write_json
from popper.domains import ALIGNED_PREDICTION, STAGED_ARTIFACTS, protocol, register
from popper.domains.protocol import Invocation, MetricSpec
from popper.domains.staged import StagedArtifactsPack
from popper.research import confirmation_runner
from popper.research.confirmation_contracts import load_private_key, public_key_b64, verify_envelope
from popper.research.confirmation_runner import (BACKEND, require_supported_shape,
                                                 run_confirmation_bundle)
from popper.research.confirmation_service import HoldoutService
from popper.research.revisions import CodeEdit, RevisionStore


MODEL_SOURCE = '''import argparse
import json
import os
from pathlib import Path

SCALE = 1.0

parser = argparse.ArgumentParser()
for name in ("train", "input", "output", "config", "seed"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
assert all(set(row) == {"id", "x"} for row in rows)
assert not any("PRIVATE_KEY" in name or "API_KEY" in name for name in os.environ)
assert {path.name for path in Path("inputs").iterdir()} == {"train.json", "inputs.json", "config.json"}
predictions = [{"id": row["id"], "prediction": row["x"] * SCALE} for row in rows]
Path(args.output).write_text(json.dumps(predictions, allow_nan=False), encoding="utf-8")
'''


THROUGHPUT_MODEL_SOURCE = '''import argparse
import json
import os
from pathlib import Path

FACTOR = 1
ELAPSED = 1.0

parser = argparse.ArgumentParser()
for name in ("warmup", "workload", "output", "config", "seed"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
assert not any("PRIVATE_KEY" in name or "API_KEY" in name for name in os.environ)
assert {path.name for path in Path("inputs").iterdir()} == {"warmup.json", "workload.json", "algorithm.json"}
warmup = json.loads(Path(args.warmup).read_text(encoding="utf-8"))
workload = json.loads(Path(args.workload).read_text(encoding="utf-8"))
settings = json.loads(Path(args.config).read_text(encoding="utf-8"))
assert all(set(row) == {"id", "n"} for row in warmup)
assert all(set(row) == {"id", "n"} for row in workload)
operations = sum(row["n"] for row in workload) * FACTOR * settings["weight"] + int(args.seed)
Path(args.output).write_text(json.dumps({"operations": operations, "elapsed_seconds": ELAPSED}, allow_nan=False), encoding="utf-8")
'''

CONVERGENCE_MODEL_SOURCE = '''import argparse
import json
from pathlib import Path

ORDER = 1

parser = argparse.ArgumentParser()
for name in ("instance", "output", "config", "seed"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
assert {path.name for path in Path("inputs").iterdir()} == {"convergence.json", "scheme.json"}
rows = json.loads(Path(args.instance).read_text(encoding="utf-8"))
scheme = json.loads(Path(args.config).read_text(encoding="utf-8"))
assert scheme["scheme"] == "explicit-euler"
assert scheme["stages"] in (1, 2)
assert all(set(row) == {"id", "steps", "rate", "t_end"} for row in rows)
steps = [row["steps"] for row in rows]
errors = [float(step) ** -ORDER for step in steps]
Path(args.output).write_text(json.dumps({"steps": steps, "errors": errors}, allow_nan=False), encoding="utf-8")
'''


class _ProbeStagedPack(StagedArtifactsPack):
    """探针 staged 域包：只声明形状与调用契约，用于验证 holdout 的形状守卫。"""

    pack_id = "probe-staged"
    evaluator_id = "probe-staged-v1"
    _metrics = (MetricSpec(name="throughput", direction="max", unit="op/s"),)
    _entry = {"id": "probe-staged-v1", "metric": {"name": "throughput", "direction": "max"},
              "definition": "staged shape probe", "dataset": "probe workload"}

    def invocation(self):
        return Invocation(args=(("--workload", "inputs"), ("--output", "prediction"),
                                ("--seed", "seed")),
                          inputs=(("inputs", "workload.json"),),
                          prediction="measurement-{seed}.json")


class _ProbeFromDataPack(_ProbeStagedPack):
    """探针：重复单位取自数据的形状，确认端拿不到标签，必须被守卫拒绝。"""

    pack_id = "probe-from-data"
    evaluator_id = "probe-from-data-v1"
    units_from_data = True
    _entry = {"id": "probe-from-data-v1", "metric": {"name": "throughput", "direction": "max"},
              "definition": "units-from-data shape probe", "dataset": "probe workload"}


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "confirmation extra is not installed")
class ConfirmationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.runner_key = load_private_key(self.root / "runner-key.pem", create=True)
        self.runner_public = public_key_b64(self.runner_key)
        self.service = HoldoutService(self.root / "private-service")
        self.bundle = self.root / "bundle"
        self.output = self.root / "runner-output"
        self.seeds = [11, 29]
        for split, rows in {
            "train": [{"id": "train-1", "x": -1, "y": -2}, {"id": "train-2", "x": 1, "y": 2}],
            "dev": [{"id": "dev-1", "x": 2, "y": 4}],
            "test": [{"id": "test-1", "x": 3, "y": 6}, {"id": "test-2", "x": 4, "y": 8}],
        }.items():
            write_json(self.project / f"{split}.json", rows)
        (self.project / "model.py").write_text(MODEL_SOURCE, encoding="utf-8")
        self.configs = {"control": {"scale": 1.0}, "candidate": {"scale": 2.0}}
        write_json(self.project / "experiment.json", {
            "name": "runner fixture", "objective": "verify signed execution",
            "entrypoint": "model.py", "code_files": ["model.py"],
            "train": "train.json", "dev": "dev.json", "test": "test.json",
            "baseline": self.configs["control"], "candidates": [self.configs["candidate"]],
            "metric": {"name": "mse", "direction": "min"}, "seeds": self.seeds,
            "budget": 1, "timeout_seconds": 10, "min_improvement": 0.1,
        })
        initialize(self.project)
        self.contract = self.service.register(
            dataset_id="runner-fixture", dataset_version="1", evaluation_group="confirm",
            train_path=self.project / "train.json", dev_path=self.project / "dev.json",
            holdout_path=self.project / "test.json", evaluator_id="mse-v1", seeds=self.seeds,
            min_effect=0.1, runtime_id="fixture-runtime", runner_public_key=self.runner_public)

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def prepare_bundle(self, candidate_source=None):
        candidate_source = candidate_source or MODEL_SOURCE.replace("SCALE = 1.0", "SCALE = 2.0")
        store = RevisionStore(self.root / "source-revisions")
        revision = store.create(
            self.project, "H-runner", "D-runner", [CodeEdit(
                "model.py", candidate_source, file_hash(self.project / "model.py"))],
            actor="hand_authored_test_fixture")
        self.bundle.mkdir()
        shutil.copyfile(self.project / "train.json", self.bundle / "train.json")
        shutil.copytree(store.path(revision["revision_id"]) / "code", self.bundle / "candidate/code")
        shutil.copyfile(store.path(revision["revision_id"]) / "revision.json",
                        self.bundle / "candidate/revision.json")
        (self.bundle / "control/code").mkdir(parents=True)
        shutil.copyfile(self.project / "model.py", self.bundle / "control/code/model.py")
        write_json(self.bundle / "contract.json", self.contract)
        contract = self.contract["payload"]
        identity = {
            "study_id": "S-runner", "family_id": "F-runner", "contract_id": contract["contract_id"],
            "contract_sha256": digest(self.contract), "source_manifest_sha256": "a" * 64,
            "hypothesis_id": "H-runner", "hypothesis_version": 1, "design_id": "D-runner",
            "dev_observation_id": "OBS-dev", "dev_observation_sha256": "b" * 64,
            "train_sha256": contract["train_sha256"], "dev_sha256": contract["dev_sha256"],
            "metric": {"name": "mse", "direction": "min"}, "evaluator_id": "mse-v1",
            "seeds": self.seeds, "min_effect": 0.1,
            "control": {"config": self.configs["control"], "entrypoint": "model.py",
                        "files": {"model.py": file_hash(self.project / "model.py")}},
            "candidate": {"config": self.configs["candidate"], "entrypoint": "model.py",
                          "files": revision["files"], "revision_id": revision["revision_id"],
                          "revision_manifest_sha256": file_hash(self.bundle / "candidate/revision.json"),
                          "execution_changes": revision["identity"]["execution_changes"]},
        }
        self.manifest = {"schema_version": "1.0", "kind": "confirmation_submission",
                         "submission_id": "SUB-" + digest(identity)[:24], "identity": identity}
        write_json(self.bundle / "bundle.json", self.manifest)
        self.ticket = self.service.begin(self.manifest)
        self.features = self.service.features(self.ticket)
        return self.manifest

    def run_bundle(self, **overrides):
        values = {"bundle_dir": self.bundle, "ticket": self.ticket, "features": self.features,
                  "service_public_key": self.service.public_key, "runner_private_key": self.runner_key,
                  "output_dir": self.output}
        values.update(overrides)
        return run_confirmation_bundle(**values)

    @unittest.skipUnless(sandbox.available(), "本平台没有可用的沙箱后端（Windows 低完整性 / Linux bubblewrap）")
    def test_real_worker_preserves_revision_covers_seeds_and_signs_without_receiving_labels(self):
        self.prepare_bundle()
        with patch.dict("os.environ", {"RUNNER_PRIVATE_KEY": "synthetic-secret",
                                       "DEEPSEEK_API_KEY": "synthetic-secret"}):
            result = self.run_bundle()
        receipt = verify_envelope(result["runner_receipt"], self.runner_public)
        self.assertEqual("succeeded", receipt["status"])
        self.assertEqual(BACKEND, receipt["backend"])
        self.assertEqual(digest(result["predictions"]), receipt["predictions_sha256"])
        self.assertEqual({role: self.manifest["identity"][role]["files"] for role in ("control", "candidate")},
                         receipt["code_hashes"])
        for role, scale in (("control", 1), ("candidate", 2)):
            self.assertEqual(self.seeds, [item["seed"] for item in result["predictions"][role]])
            self.assertEqual([3 * scale, 4 * scale],
                             [row["prediction"] for row in result["predictions"][role][0]["rows"]])
        for path in (self.output / "jobs").glob("JOB-*/job.json"):
            job = read_json(path)
            generated = job["spec"]["revision_id"] == self.manifest["identity"]["candidate"]["revision_id"]
            self.assertEqual(generated, job["spec"]["require_edit_coverage"])
            self.assertEqual(180, job["spec"]["timeout_seconds"])
            if generated:
                self.assertTrue(read_json(path.parent / "receipt.json")["execution_gate"]["passed"])
        revision_id = self.manifest["identity"]["candidate"]["revision_id"]
        self.assertEqual((self.bundle / "candidate/revision.json").read_bytes(),
                         (self.output / "revisions" / revision_id / "revision.json").read_bytes())
        runtime = read_json(self.output / "runtime.json")
        self.assertEqual("operator_registered_engineering_label", runtime["runtime_id_semantics"])
        self.assertNotIn("private_key", json.dumps(read_json(self.output / "invocation.json")))
        scored = self.service.complete(self.ticket, result["runner_receipt"], result["predictions"])
        self.assertTrue(scored["payload"]["passed"])
        with patch("popper.research.confirmation_runner.LocalWorker") as worker:
            self.assertEqual(result, self.run_bundle())
            worker.assert_not_called()

    def test_invalid_ticket_features_key_or_source_are_rejected_before_execution(self):
        self.prepare_bundle()
        forged = copy.deepcopy(self.ticket)
        forged["payload"]["features_sha256"] = "0" * 64
        wrong_key = load_private_key(self.root / "wrong-runner-key.pem", create=True)
        labelled = [{**row, "y": 0} for row in self.features]
        variants = ({"ticket": forged}, {"features": labelled},
                    {"features": [{"id": "other", "x": 3}]}, {"runner_private_key": wrong_key},
                    {"service_public_key": public_key_b64(wrong_key)})
        with patch("popper.research.confirmation_runner.LocalWorker") as worker:
            for values in variants:
                with self.subTest(fields=list(values)), self.assertRaises(ProtocolError):
                    self.run_bundle(**values)
                self.assertFalse(self.output.exists())
            (self.bundle / "candidate/code/model.py").write_text("raise RuntimeError('tamper')", encoding="utf-8")
            with self.assertRaises(ProtocolError):
                self.run_bundle()
            worker.assert_not_called()

    def test_interrupted_or_conflicting_directory_cannot_execute_again(self):
        self.prepare_bundle()
        self.output.mkdir()
        invocation = {"submission_sha256": digest(self.manifest), "ticket_sha256": digest(self.ticket),
                      "features_sha256": digest(self.features), "runner_public_key": self.runner_public}
        with patch("popper.research.confirmation_runner.LocalWorker") as worker:
            with self.assertRaisesRegex(ProtocolError, "interrupted"):
                self.run_bundle()
            write_json(self.output / "invocation.json", invocation)
            with self.assertRaisesRegex(ProtocolError, "interrupted"):
                self.run_bundle()
            write_json(self.output / "result.json", {"runner_receipt": {}, "predictions": {}})
            with self.assertRaises(ProtocolError):
                self.run_bundle()
            worker.assert_not_called()

    @unittest.skipUnless(sandbox.available(), "本平台没有可用的沙箱后端（Windows 低完整性 / Linux bubblewrap）")
    def test_unexecuted_candidate_gets_failed_signature_and_cannot_retry(self):
        self.prepare_bundle(MODEL_SOURCE + "\ndef unused_change():\n    return 72\n")
        result = self.run_bundle()
        payload = verify_envelope(result["runner_receipt"], self.runner_public)
        self.assertEqual("failed", payload["status"])
        self.assertEqual("ProtocolError", payload["error_type"])
        self.assertEqual([], result["predictions"]["candidate"])
        self.assertEqual(self.seeds, [item["seed"] for item in result["predictions"]["control"]])
        with patch("popper.research.confirmation_runner.LocalWorker") as worker:
            self.assertEqual(result, self.run_bundle())
            worker.assert_not_called()
        scored = self.service.complete(self.ticket, result["runner_receipt"], result["predictions"])
        self.assertEqual("failed", scored["payload"]["status"])
        self.assertIsNone(scored["payload"]["effect"])

    def test_unavailable_sandbox_never_falls_back_and_failed_result_is_tamper_evident(self):
        self.prepare_bundle()
        with patch("popper.research.workers.local.sandbox.available", return_value=False):
            result = self.run_bundle()
        payload = verify_envelope(result["runner_receipt"], self.runner_public)
        self.assertEqual("failed", payload["status"])
        self.assertEqual({"control": [], "candidate": []}, result["predictions"])
        changed = copy.deepcopy(result)
        changed["predictions"]["control"] = [{"seed": 11, "rows": [{"id": "test-1", "prediction": 100}]}]
        write_json(self.output / "result.json", changed)
        with patch("popper.research.confirmation_runner.LocalWorker") as worker:
            with self.assertRaisesRegex(ProtocolError, "signed runner receipt"):
                self.run_bundle()
            worker.assert_not_called()

    @unittest.skipUnless(sandbox.available(), "本平台没有可用的沙箱后端（Windows 低完整性 / Linux bubblewrap）")
    def test_successful_worker_with_wrong_prediction_ids_cannot_get_success_signature(self):
        source = MODEL_SOURCE.replace("SCALE = 1.0", "SCALE = 2.0").replace(
            '"id": row["id"], "prediction":', '"id": "forged-" + row["id"], "prediction":')
        self.prepare_bundle(source)
        result = self.run_bundle()
        payload = verify_envelope(result["runner_receipt"], self.runner_public)
        self.assertEqual("failed", payload["status"])
        self.assertEqual([], result["predictions"]["candidate"])
        candidate_id = self.manifest["identity"]["candidate"]["revision_id"]
        jobs = [read_json(path) for path in (self.output / "jobs").glob("JOB-*/receipt.json")]
        candidate_jobs = [job for job in jobs if job["revision_id"] == candidate_id]
        self.assertEqual(1, len(candidate_jobs))
        self.assertEqual("succeeded", candidate_jobs[0]["status"])
        self.assertTrue(candidate_jobs[0]["execution_gate"]["passed"])

    def test_existing_bundle_cannot_be_used_as_output_directory(self):
        self.prepare_bundle()
        with self.assertRaisesRegex(ProtocolError, "separate"):
            self.run_bundle(output_dir=self.bundle / "outputs")
        self.assertFalse((self.bundle / "outputs").exists())


class _StagedConfirmationFixture:
    """staged_artifacts 外部确认的公共夹具（混入 unittest.TestCase 使用）。

    与逐样本形状的差别只有两处：候选产物是一份 JSON 制品（不是逐样本预测行），
    以及输入/制品的命名由域包声明。执行、签名、独立计分三段式完全共用。
    """

    MODEL_SOURCE = ""
    CANDIDATE_EDITS = ()
    EVALUATOR_ID = ""
    METRIC = {}
    CONFIGS = {}
    SPLITS = {}
    SEEDS = (11, 29)
    MEASUREMENT_FIELDS = frozenset()
    INPUT_BASENAMES = frozenset()
    MIN_EFFECT = 0.0
    EXPECTED_EFFECT = 0.0

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.runner_key = load_private_key(self.root / "runner-key.pem", create=True)
        self.runner_public = public_key_b64(self.runner_key)
        self.service = HoldoutService(self.root / "private-service")
        self.addCleanup(self.service.close)
        self.bundle = self.root / "bundle"
        self.output = self.root / "runner-output"
        self.seeds = list(self.SEEDS)
        for split, rows in self.SPLITS.items():
            write_json(self.project / f"{split}.json", rows)
        (self.project / "model.py").write_text(self.MODEL_SOURCE, encoding="utf-8")
        write_json(self.project / "experiment.json", {
            "name": "staged runner fixture", "objective": "verify staged confirmation",
            "entrypoint": "model.py", "code_files": ["model.py"],
            "train": "train.json", "dev": "dev.json", "test": "test.json",
            "baseline": self.CONFIGS["control"], "candidates": [self.CONFIGS["candidate"]],
            "metric": dict(self.METRIC), "seeds": self.seeds,
            "budget": 1, "timeout_seconds": 30, "min_improvement": 0.1,
        })
        initialize(self.project)
        self.contract = self.service.register(
            dataset_id="staged-runner-fixture", dataset_version="1", evaluation_group="confirm",
            train_path=self.project / "train.json", dev_path=self.project / "dev.json",
            holdout_path=self.project / "test.json", evaluator_id=self.EVALUATOR_ID,
            seeds=self.seeds, min_effect=self.MIN_EFFECT, runtime_id="staged-fixture-runtime",
            runner_public_key=self.runner_public)

    def candidate_source(self):
        """按域包声明的锚点改一处**会被执行**的代码：候选必须真被改了才算执行过。"""
        source = self.MODEL_SOURCE
        for original, replacement in self.CANDIDATE_EDITS:
            if original not in source:
                raise AssertionError(f"候选编辑锚点不存在: {original!r}")
            source = source.replace(original, replacement)
        return source

    def prepare_bundle(self, candidate_source=None):
        source = candidate_source or self.candidate_source()
        store = RevisionStore(self.root / "source-revisions")
        revision = store.create(
            self.project, "H-staged", "D-staged",
            [CodeEdit("model.py", source, file_hash(self.project / "model.py"))],
            actor="hand_authored_test_fixture")
        self.bundle.mkdir()
        shutil.copyfile(self.project / "train.json", self.bundle / "train.json")
        shutil.copytree(store.path(revision["revision_id"]) / "code", self.bundle / "candidate/code")
        shutil.copyfile(store.path(revision["revision_id"]) / "revision.json",
                        self.bundle / "candidate/revision.json")
        (self.bundle / "control/code").mkdir(parents=True)
        shutil.copyfile(self.project / "model.py", self.bundle / "control/code/model.py")
        write_json(self.bundle / "contract.json", self.contract)
        contract = self.contract["payload"]
        identity = {
            "study_id": "S-staged", "family_id": "F-staged", "contract_id": contract["contract_id"],
            "contract_sha256": digest(self.contract), "source_manifest_sha256": "a" * 64,
            "hypothesis_id": "H-staged", "hypothesis_version": 1, "design_id": "D-staged",
            "dev_observation_id": "OBS-staged", "dev_observation_sha256": "b" * 64,
            "train_sha256": contract["train_sha256"], "dev_sha256": contract["dev_sha256"],
            "metric": dict(self.METRIC), "evaluator_id": self.EVALUATOR_ID,
            "seeds": self.seeds, "min_effect": self.MIN_EFFECT,
            "control": {"config": self.CONFIGS["control"], "entrypoint": "model.py",
                        "files": {"model.py": file_hash(self.project / "model.py")}},
            "candidate": {"config": self.CONFIGS["candidate"], "entrypoint": "model.py",
                          "files": revision["files"], "revision_id": revision["revision_id"],
                          "revision_manifest_sha256": file_hash(self.bundle / "candidate/revision.json"),
                          "execution_changes": revision["identity"]["execution_changes"]},
        }
        self.manifest = {"schema_version": "1.0", "kind": "confirmation_submission",
                         "submission_id": "SUB-" + digest(identity)[:24], "identity": identity}
        write_json(self.bundle / "bundle.json", self.manifest)
        self.ticket = self.service.begin(self.manifest)
        self.features = self.service.features(self.ticket)
        return self.manifest

    def run_bundle(self, **overrides):
        values = {"bundle_dir": self.bundle, "ticket": self.ticket, "features": self.features,
                  "service_public_key": self.service.public_key, "runner_private_key": self.runner_key,
                  "output_dir": self.output}
        values.update(overrides)
        return run_confirmation_bundle(**values)

    def staged_input_basenames(self):
        """候选工作区里真正落位的输入名：未声明的角色不该出现（如收敛阶没有 train）。"""
        return {item["target"] for path in (self.output / "jobs").glob("JOB-*/job.json")
                for item in read_json(path)["spec"]["inputs"]}

    @unittest.skipUnless(sandbox.available(), "本平台没有可用的沙箱后端（Windows 低完整性 / Linux bubblewrap）")
    def test_staged_artifact_round_trip_scores_once_and_never_reruns(self):
        self.prepare_bundle()
        result = self.run_bundle()
        receipt = verify_envelope(result["runner_receipt"], self.runner_public)
        self.assertEqual("succeeded", receipt["status"])
        self.assertEqual(BACKEND, receipt["backend"])
        self.assertEqual(digest(result["predictions"]), receipt["predictions_sha256"])
        for role in ("control", "candidate"):
            self.assertEqual(self.seeds, [item["seed"] for item in result["predictions"][role]])
            for item in result["predictions"][role]:
                # 回执里装的是制品原始测量，不是逐样本预测行。
                self.assertEqual(self.MEASUREMENT_FIELDS, set(item["rows"]))
        # 落位名全部取自域包声明：bundle 里那份 train.json 只对声明了 train 的域包落位。
        self.assertEqual(self.INPUT_BASENAMES, self.staged_input_basenames())
        scored = self.service.complete(self.ticket, result["runner_receipt"], result["predictions"])
        self.assertTrue(scored["payload"]["passed"])
        self.assertAlmostEqual(self.EXPECTED_EFFECT, scored["payload"]["effect"], places=9)
        self.assertEqual(self.seeds,
                         [point["seed"] for point in scored["payload"]["control"]["per_seed"]])
        with patch("popper.research.confirmation_runner.LocalWorker") as worker:
            self.assertEqual(result, self.run_bundle())
            worker.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "confirmation extra is not installed")
class StagedThroughputConfirmationTests(_StagedConfirmationFixture, unittest.TestCase):
    """算法吞吐（声明了 train 角色 + independent_run 重复测量）走完整外部确认闭环。"""

    MODEL_SOURCE = THROUGHPUT_MODEL_SOURCE
    # 两份夹具的差异必须落在**会被执行**的代码行上，否则候选作业过不了执行覆盖门。
    CANDIDATE_EDITS = (("FACTOR = 1", "FACTOR = 2"),)
    EVALUATOR_ID = "algorithm-throughput-v1"
    METRIC = {"name": "throughput", "direction": "max"}
    CONFIGS = {"control": {"algorithm": "insertion", "weight": 1},
               "candidate": {"algorithm": "merge", "weight": 1}}
    SPLITS = {
        "train": [{"id": "warmup-1", "n": 200}, {"id": "warmup-2", "n": 300}],
        "dev": [{"id": "dev-1", "n": 500}, {"id": "dev-2", "n": 600}],
        "test": [{"id": "test-1", "n": 600}, {"id": "test-2", "n": 700}],
    }
    MEASUREMENT_FIELDS = frozenset({"operations", "elapsed_seconds"})
    INPUT_BASENAMES = frozenset({"warmup.json", "workload.json", "algorithm.json"})
    # 保留集规模 1300：control 1300 次操作、candidate 2600 次（FACTOR 1→2），均摊后差 1300。
    MIN_EFFECT = 100.0
    EXPECTED_EFFECT = 1300.0

    @unittest.skipUnless(sandbox.available(), "本平台没有可用的沙箱后端（Windows 低完整性 / Linux bubblewrap）")
    def test_candidate_self_reported_metric_is_rejected_by_the_runner(self):
        self.prepare_bundle(THROUGHPUT_MODEL_SOURCE.replace(
            '{"operations": operations, "elapsed_seconds": ELAPSED}',
            '{"operations": operations, "elapsed_seconds": ELAPSED, "throughput": 999}'))
        result = self.run_bundle()
        payload = verify_envelope(result["runner_receipt"], self.runner_public)
        self.assertEqual("failed", payload["status"])
        self.assertEqual("ProtocolError", payload["error_type"])
        self.assertEqual([], result["predictions"]["candidate"])
        self.assertEqual(self.seeds, [item["seed"] for item in result["predictions"]["control"]])
        scored = self.service.complete(self.ticket, result["runner_receipt"], result["predictions"])
        self.assertEqual("failed", scored["payload"]["status"])
        self.assertEqual("RunnerFailed", scored["payload"]["error_type"])
        self.assertIsNone(scored["payload"]["effect"])


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "confirmation extra is not installed")
class StagedConvergenceConfirmationTests(_StagedConfirmationFixture, unittest.TestCase):
    """数值收敛阶：没有 train 角色的 staged 域包，bundle 里的 train.json 不得落位。"""

    MODEL_SOURCE = CONVERGENCE_MODEL_SOURCE
    CANDIDATE_EDITS = (("ORDER = 1", "ORDER = 2"),)
    EVALUATOR_ID = "convergence-order-v1"
    METRIC = {"name": "convergence_order", "direction": "max"}
    CONFIGS = {"control": {"scheme": "explicit-euler", "stages": 1},
               "candidate": {"scheme": "explicit-euler", "stages": 2}}
    SPLITS = {
        "train": [{"id": "train-4", "steps": 4, "rate": 1.0, "t_end": 1.0},
                  {"id": "train-8", "steps": 8, "rate": 1.0, "t_end": 1.0}],
        "dev": [{"id": "dev-16", "steps": 16, "rate": 1.0, "t_end": 1.0},
                {"id": "dev-32", "steps": 32, "rate": 1.0, "t_end": 1.0}],
        "test": [{"id": "test-8", "steps": 8, "rate": 1.0, "t_end": 1.0},
                 {"id": "test-16", "steps": 16, "rate": 1.0, "t_end": 1.0},
                 {"id": "test-32", "steps": 32, "rate": 1.0, "t_end": 1.0}],
    }
    MEASUREMENT_FIELDS = frozenset({"steps", "errors"})
    # 关键：本域包没有 train 角色，bundle 里冻结的 train.json 只在服务侧，不进候选工作区。
    INPUT_BASENAMES = frozenset({"convergence.json", "scheme.json"})
    # 一阶/二阶方案的误差衰减恰好是 step^-1 / step^-2，拟合阶数就是 1 与 2。
    MIN_EFFECT = 0.5
    EXPECTED_EFFECT = 1.0

    @unittest.skipUnless(sandbox.available(), "本平台没有可用的沙箱后端（Windows 低完整性 / Linux bubblewrap）")
    def test_runner_signs_measured_noise_but_the_service_refuses_to_score_it(self):
        self.prepare_bundle(CONVERGENCE_MODEL_SOURCE.replace(
            "errors = [float(step) ** -ORDER for step in steps]",
            "errors = [1.0 for step in steps]"))
        result = self.run_bundle()
        payload = verify_envelope(result["runner_receipt"], self.runner_public)
        # 误差序列长度与取值都合法，runner 只签「候选报了这些测量」，不做领域解释。
        self.assertEqual("succeeded", payload["status"])
        self.assertEqual(self.seeds, [item["seed"] for item in result["predictions"]["candidate"]])
        scored = self.service.complete(self.ticket, result["runner_receipt"], result["predictions"])
        # 「收敛阶」怎么算属于域包：误差不随网格加密下降时不得当作一个偏低的阶数入证。
        self.assertEqual("failed", scored["payload"]["status"])
        self.assertEqual("ProtocolError", scored["payload"]["error_type"])
        self.assertIsNone(scored["payload"]["effect"])
        self.assertIsNone(scored["payload"]["passed"])


class HoldoutShapeGuardTests(unittest.TestCase):
    """形状守卫：holdout 支持预注册 seed 的两种形状，拒绝从数据取重复单位的形状。"""

    def test_aligned_shape_is_returned(self):
        pack = require_supported_shape("mse-v1")
        self.assertEqual("mse-v1", pack.evaluator_id)
        self.assertEqual(ALIGNED_PREDICTION, pack.task_shape)

    def test_staged_shape_is_returned(self):
        register(_ProbeStagedPack())
        self.addCleanup(protocol._PACKS.pop, "probe-staged-v1", None)
        pack = require_supported_shape("probe-staged-v1")
        self.assertEqual(STAGED_ARTIFACTS, pack.task_shape)
        self.assertFalse(pack.units_from_data)

    def test_registered_staged_packs_pass_while_the_analysis_unit_pack_does_not(self):
        """本轮覆盖边界：两个真实 staged 域包放行，分析单元形状仍被守卫挡住。"""
        for evaluator_id in ("algorithm-throughput-v1", "convergence-order-v1"):
            with self.subTest(evaluator_id=evaluator_id):
                pack = require_supported_shape(evaluator_id)
                self.assertEqual(STAGED_ARTIFACTS, pack.task_shape)
                self.assertEqual("independent_run", pack.repeat_unit())
        with self.assertRaisesRegex(ProtocolError, "未支持的任务形状"):
            require_supported_shape("treatment-effect-v1")

    def test_units_from_data_shape_fails_immediately_with_the_real_shape(self):
        register(_ProbeFromDataPack())
        self.addCleanup(protocol._PACKS.pop, "probe-from-data-v1", None)
        with self.assertRaises(ProtocolError) as caught:
            require_supported_shape("probe-from-data-v1")
        self.assertIn("未支持的任务形状", str(caught.exception))
        self.assertIn("重复单位取自数据", str(caught.exception))

    def test_guard_precedes_every_job_and_directory_creation(self):
        """守卫必须早于任何落盘/建 job：否则不支持的形状会留下部分状态。

        用源码行序做静态断言（比 mock 哨兵更直接：这里要证明的是「守卫在任何
        落盘动作之前」而不是「某个对象没被调用」）。
        """
        source = inspect.getsource(confirmation_runner.run_confirmation_bundle)
        guard = source.index("pack = require_supported_shape(")
        for later in ("output.mkdir(", "write_json(", "LocalWorker(", "worker.run("):
            with self.subTest(later=later):
                self.assertLess(guard, source.index(later))


if __name__ == "__main__":
    unittest.main()
