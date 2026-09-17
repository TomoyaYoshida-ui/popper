import copy
import runpy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import Experiment, ProtocolError, digest, file_hash, initialize, read_json, write_json
from popper.research.actions import REQUEST_CONFIRMATION, RUN_EXPERIMENT, ActionProposal
from popper.research.confirmation_bundle import export_confirmation_bundle, verify_bundle
from popper.research.confirmation_contracts import (
    confirmation_service_hash, load_private_key, public_key_b64, sign_payload,
)
from popper.research.contracts import HypothesisStatus
from popper.research.controller import ResearchController
from popper.research.evaluation_service import scoring_code_hash
from popper.research.models import EvidenceDrivenPolicy
from popper.research.revisions import CodeEdit


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


def ready_generated_controller(root):
    """Real dev execution fixture; its executor is explicitly test-only."""
    root = Path(root)
    project = root / "project"
    project.mkdir()
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, project / name)
    spec = read_json(project / "experiment.json")
    spec["seeds"] = [11, 29]
    write_json(project / "experiment.json", spec)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](project)
    initialize(project)
    exp = Experiment(project)
    try:
        state = exp.verify_inputs()
    finally:
        exp.close()
    service_key = load_private_key(root / "service-private.pem", create=True)
    runner_key = load_private_key(root / "runner-private.pem", create=True)
    service_public = public_key_b64(service_key)
    payload = {
        "schema_version": "1.0", "kind": "holdout_contract",
        "issuer_id": digest(service_public)[:24], "dataset_id": "quadratic-fixture",
        "dataset_version": "1", "evaluation_group": "bundle-test",
        "train_sha256": state["input_hashes"][spec["train"]],
        "dev_sha256": state["input_hashes"][spec["dev"]],
        "holdout_sha256": "1" * 64, "features_sha256": "2" * 64,
        "evaluator_id": state["evaluator_id"], "evaluator_hash": state["evaluator_hash"],
        "scoring_code_sha256": scoring_code_hash(), "service_code_sha256": confirmation_service_hash(),
        "seeds": spec["seeds"], "min_effect": spec["min_improvement"],
        "runtime_id": "test-runtime", "runner_public_key": public_key_b64(runner_key),
        "allowed_backend": "injected_test_executor", "trust": "engineering_same_account",
    }
    payload["contract_id"] = "HC-" + digest(payload)[:24]
    envelope = sign_payload(payload, service_key)

    class Policy(EvidenceDrivenPolicy):
        name = "bundle_test_policy"

        def propose_revision(self, objective, hypothesis, config, code_files, **kwargs):
            source = next(row for row in code_files if row["path"] == "model.py")
            changed = source["content"].replace("weights = solve(matrix, rhs)",
                                                 "weights = list(solve(matrix, rhs))")
            return {"edits": [CodeEdit("model.py", changed, source["sha256"])],
                    "rationale": "Execute the frozen quadratic revision for the export fixture."}

    policy = Policy()
    ResearchController.initialize(project, root / "research", policy=policy,
                                  confirmation_contract=envelope, confirmation_public_key=service_public)
    controller = ResearchController(root / "research", policy=policy)

    def executor(command, workspace, env, stdout, stderr, job_spec):
        subprocess.run(command, cwd=workspace, env=env, stdout=stdout, stderr=stderr,
                       timeout=job_spec.timeout_seconds, check=True)

    controller.worker.executor = executor
    control_id = controller.manifest["control_hypothesis_id"]
    baseline = controller._execute_development(
        control_id, ActionProposal(RUN_EXPERIMENT, "Run the original registered baseline.", control_id),
        True, False)
    controller.store.set_hypothesis_status(control_id, 1, HypothesisStatus.INCONCLUSIVE)
    selected = next(row for row in controller.status()["candidates"] if row["config"] == {"degree": 2})
    result = controller.implement(selected["hypothesis_id"])
    controller.store.set_hypothesis_status(selected["hypothesis_id"], 1, HypothesisStatus.SUPPORTED_IN_SCOPE)
    controller._record(
        ActionProposal(REQUEST_CONFIRMATION, "The generated revision improved development MSE.",
                       selected["hypothesis_id"]),
        (baseline["observation_id"], result["observation"]["observation_id"]))
    return controller, envelope, service_public, service_key


class ConfirmationBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        try:
            cls.controller, cls.envelope, cls.public_key, cls.private_key = ready_generated_controller(cls.shared.name)
            cls.reference = Path(cls.shared.name) / "reference-bundle"
            cls.manifest = export_confirmation_bundle(cls.controller, cls.envelope, cls.public_key, cls.reference)
        except Exception:
            if hasattr(cls, "controller"):
                cls.controller.close()
            cls.shared.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.controller.close()
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def bundle_copy(self):
        target = self.root / "bundle"
        shutil.copytree(self.reference, target)
        return target

    def test_export_preserves_actual_revision_and_baseline_without_holdout_side_effects(self):
        before = self.controller.status()
        core_before = self.controller.exp.state()
        output = self.root / "export"
        with patch.object(self.controller.exp, "evaluate", side_effect=AssertionError("No experiment during export")):
            manifest = export_confirmation_bundle(self.controller, self.envelope, self.public_key, output)
        checked, contract = verify_bundle(output, self.public_key)
        self.assertEqual(manifest, checked)
        self.assertEqual(self.envelope["payload"], contract)
        self.assertEqual(before, self.controller.status())
        self.assertEqual(core_before, self.controller.exp.state())
        self.assertEqual([], self.controller.exp.results("test"))
        identity = manifest["identity"]
        self.assertEqual({"degree": 1}, identity["control"]["config"])
        self.assertEqual({"degree": 2}, identity["candidate"]["config"])
        revision_root = self.controller.revisions.path(identity["candidate"]["revision_id"])
        self.assertEqual((revision_root / "code/model.py").read_bytes(), (output / "candidate/code/model.py").read_bytes())
        self.assertEqual((revision_root / "revision.json").read_bytes(), (output / "candidate/revision.json").read_bytes())
        self.assertEqual((self.controller.project / "model.py").read_bytes(), (output / "control/code/model.py").read_bytes())
        self.assertNotEqual(identity["control"]["files"], identity["candidate"]["files"])
        self.assertEqual({"bundle.json", "contract.json", "train.json", "control/code/model.py",
                          "candidate/code/model.py", "candidate/revision.json"},
                         {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()})

    def test_existing_output_is_never_overwritten(self):
        target = self.root / "existing"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")
        with self.assertRaises(ProtocolError):
            export_confirmation_bundle(self.controller, self.envelope, self.public_key, target)
        self.assertEqual("preserve", marker.read_text(encoding="utf-8"))
        self.assertEqual([marker], list(target.iterdir()))

    def test_mismatched_signed_contract_is_rejected_before_output(self):
        for field, value in (("seeds", [11]), ("train_sha256", "f" * 64),
                             ("dev_sha256", "e" * 64), ("min_effect", 0.5),
                             ("evaluation_group", "late-replacement-group"),
                             ("scoring_code_sha256", "d" * 64)):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.envelope["payload"])
                payload[field] = value
                payload.pop("contract_id")
                payload["contract_id"] = "HC-" + digest(payload)[:24]
                envelope = sign_payload(payload, self.private_key)
                output = self.root / field
                with self.assertRaises(ProtocolError):
                    export_confirmation_bundle(self.controller, envelope, self.public_key, output)
                self.assertFalse(output.exists())

    def test_unbound_legacy_study_cannot_export(self):
        manifest = copy.deepcopy(self.controller.manifest)
        manifest.pop("scoring_code_sha256")
        with patch.object(self.controller, "manifest", manifest):
            with self.assertRaises(ProtocolError):
                export_confirmation_bundle(self.controller, self.envelope, self.public_key, self.root / "legacy")
        self.assertFalse((self.root / "legacy").exists())

    def test_missing_or_failed_execution_gate_cannot_export(self):
        real_collect = self.controller.worker.collect
        for gate in (None, {"passed": False, "reason": "changed_code_not_executed"}):
            def altered(job_id):
                receipt = copy.deepcopy(real_collect(job_id))
                receipt["execution_gate"] = gate
                return receipt
            with self.subTest(gate=gate), patch.object(self.controller.worker, "collect", side_effect=altered):
                with self.assertRaises(ProtocolError):
                    export_confirmation_bundle(self.controller, self.envelope, self.public_key, self.root / "no-gate")
        self.assertFalse((self.root / "no-gate").exists())

    def test_selected_observation_cannot_be_substituted_with_unbound_run(self):
        status = copy.deepcopy(self.controller.status())
        selected_id = self.manifest["identity"]["dev_observation_id"]
        next(row for row in status["observations"] if row["observation_id"] == selected_id)["run_id"] = "RUN-unrelated"
        with patch.object(self.controller, "status", return_value=status):
            with self.assertRaises(ProtocolError):
                export_confirmation_bundle(self.controller, self.envelope, self.public_key, self.root / "unbound")

    def test_modified_revision_source_blocks_export(self):
        revision_root = self.controller.revisions.path(self.manifest["identity"]["candidate"]["revision_id"])
        source = revision_root / "code/model.py"
        original = source.read_bytes()
        try:
            source.write_bytes(original + b"\n# post-experiment mutation\n")
            with self.assertRaises(ProtocolError):
                export_confirmation_bundle(self.controller, self.envelope, self.public_key, self.root / "tampered")
        finally:
            source.write_bytes(original)

    def test_verifier_rejects_modified_training_code_revision_and_extra_labels(self):
        for relative in ("train.json", "candidate/code/model.py", "control/code/model.py",
                         "candidate/revision.json", "test.json"):
            with self.subTest(relative=relative):
                target = self.root / relative.replace("/", "-")
                shutil.copytree(self.reference, target)
                path = target / relative
                path.write_text("[]", encoding="utf-8")
                with self.assertRaises(ProtocolError):
                    verify_bundle(target, self.public_key)

    def test_verifier_rejects_unsafe_code_path_and_wrong_signer(self):
        target = self.bundle_copy()
        another = load_private_key(self.root / "another.pem", create=True)
        with self.assertRaises(ProtocolError):
            verify_bundle(target, public_key_b64(another))
        manifest = read_json(target / "bundle.json")
        manifest["identity"]["candidate"]["files"]["../escape.py"] = "0" * 64
        manifest["submission_id"] = "SUB-" + digest(manifest["identity"])[:24]
        write_json(target / "bundle.json", manifest)
        with self.assertRaises(ProtocolError):
            verify_bundle(target, self.public_key)

    def test_verifier_rejects_symlink_even_when_target_bytes_match(self):
        target = self.bundle_copy()
        source = target / "train.json"
        external = self.root / "external.json"
        source.rename(external)
        try:
            source.symlink_to(external)
        except OSError:
            external.rename(source)
            self.skipTest("Creating symlinks requires Windows developer mode or privileges")
        with self.assertRaises(ProtocolError):
            verify_bundle(target, self.public_key)

    def test_copy_failure_cleans_only_owned_staging_and_leaves_no_output(self):
        real_copy = shutil.copyfile
        calls = []

        def fail_copy(source, target, *args, **kwargs):
            calls.append(target)
            if len(calls) == 3:
                raise OSError("injected copy failure")
            return real_copy(source, target, *args, **kwargs)

        output = self.root / "failed"
        marker = self.root / "preserve.txt"
        marker.write_text("keep", encoding="utf-8")
        with patch("popper.research.confirmation_bundle.shutil.copyfile", side_effect=fail_copy):
            with self.assertRaises(ProtocolError):
                export_confirmation_bundle(self.controller, self.envelope, self.public_key, output)
        self.assertEqual(3, len(calls))
        self.assertFalse(output.exists())
        self.assertEqual([marker], list(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
