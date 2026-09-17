import copy
import runpy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.capabilities import extra_available
from popper.core import ProtocolError, digest, initialize, read_json, write_json
from popper.research.actions import REQUEST_CONFIRMATION, RUN_EXPERIMENT, ActionProposal
from popper.research.confirmation_contracts import load_private_key, public_key_b64, sign_payload
from popper.research.confirmation_service import HoldoutService
from popper.research.contracts import HypothesisStatus
from popper.research.controller import ResearchController
from popper.research.models import EvidenceDrivenPolicy
from popper.research.revisions import CodeEdit
from tests.test_research_confirmation_bundle import EXAMPLE


@unittest.skipUnless(extra_available("confirmation"),
                     "confirmation extra 未安装（cryptography）：核心零依赖 job 显式跳过，"
                     "装齐 extras 的 job 逐条真跑")
class ExternalConfirmationControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.project / name)
        spec = read_json(self.project / "experiment.json")
        spec["seeds"] = [11, 29]
        write_json(self.project / "experiment.json", spec)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        self.runner_key = load_private_key(self.root / "runner.pem", create=True)
        self.service = HoldoutService(self.root / "private-service")
        self.contract = self.service.register(
            dataset_id="quadratic-controller-test", dataset_version="1", evaluation_group="registered-first",
            train_path=self.project / "train.json", dev_path=self.project / "dev.json",
            holdout_path=self.project / "test.json", evaluator_id="mse-v1", seeds=spec["seeds"],
            min_effect=spec["min_improvement"], runtime_id="fixture-runtime",
            runner_public_key=public_key_b64(self.runner_key), allowed_backend="injected_test_executor")

        class Policy(EvidenceDrivenPolicy):
            name = "external_confirmation_fixture"

            def propose_revision(self, objective, hypothesis, config, code_files, **kwargs):
                source = next(row for row in code_files if row["path"] == "model.py")
                changed = source["content"].replace("weights = solve(matrix, rhs)",
                                                     "weights = list(solve(matrix, rhs))")
                return {"edits": [CodeEdit("model.py", changed, source["sha256"])],
                        "rationale": "Run the registered quadratic implementation."}

        self.policy = Policy()
        self.run_dir = self.root / "research"
        ResearchController.initialize(self.project, self.run_dir, policy=self.policy,
            confirmation_contract=self.contract, confirmation_public_key=self.service.public_key)
        self.controller = ResearchController(self.run_dir, policy=self.policy)

        def executor(command, workspace, env, stdout, stderr, spec):
            subprocess.run(command, cwd=workspace, env=env, stdout=stdout, stderr=stderr,
                           timeout=spec.timeout_seconds, check=True)

        self.controller.worker.executor = executor
        control_id = self.controller.manifest["control_hypothesis_id"]
        baseline = self.controller._execute_development(
            control_id, ActionProposal(RUN_EXPERIMENT, "Run the original baseline.", control_id), True, False)
        self.controller.store.set_hypothesis_status(control_id, 1, HypothesisStatus.INCONCLUSIVE)
        self.selected = next(row for row in self.controller.status()["candidates"] if row["config"] == {"degree": 2})
        self.generated = self.controller.implement(self.selected["hypothesis_id"])
        self.controller.store.set_hypothesis_status(self.selected["hypothesis_id"], 1, HypothesisStatus.SUPPORTED_IN_SCOPE)
        self.controller._record(
            ActionProposal(REQUEST_CONFIRMATION, "The generated candidate passed development screening.",
                           self.selected["hypothesis_id"]),
            (baseline["observation_id"], self.generated["observation"]["observation_id"]))

    def tearDown(self):
        self.controller.close()
        self.service.close()
        self.temp.cleanup()

    def events(self):
        return self.controller.store._unsafe_conn.execute(
            "SELECT * FROM events ORDER BY seq").fetchall()

    def complete_fixture_runner(self, submission):
        """Sign fixture predictions; actual runner execution has separate tests."""
        ticket = self.service.begin(submission)
        self.assertEqual(ticket, self.service.begin(submission))
        features = self.service.features(ticket)
        self.assertTrue(all(set(row) == {"id", "x"} for row in features))
        predictions = {role: [
            {"seed": seed, "rows": [{"id": row["id"], "prediction":
                 0.5 + 0.7 * row["x"] + 1.8 * row["x"] ** 2 if role == "candidate" else 0}
                 for row in features]}
            for seed in self.contract["payload"]["seeds"]]
            for role in ("control", "candidate")}
        payload = {"schema_version": "1.0", "kind": "confirmation_runner_receipt",
            "ticket_id": ticket["payload"]["ticket_id"], "contract_id": ticket["payload"]["contract_id"],
            "submission_id": submission["submission_id"], "submission_sha256": digest(submission),
            "runtime_id": self.contract["payload"]["runtime_id"],
            "backend": self.contract["payload"]["allowed_backend"], "status": "succeeded",
            "code_hashes": {role: submission["identity"][role]["files"] for role in ("control", "candidate")},
            "predictions_sha256": digest(predictions), "error_type": None}
        receipt = sign_payload(payload, self.runner_key)
        result = self.service.complete(ticket, receipt, predictions)
        self.assertTrue(result["payload"]["passed"])
        with patch("popper.research.confirmation_service.score", side_effect=AssertionError("Cannot rescore a consumed ticket")):
            self.assertEqual(result, self.service.complete(ticket, receipt, predictions))
        return result

    def test_registered_generated_revision_confirms_and_resumes_idempotently(self):
        before = self.controller.status()
        self.assertEqual({"contract": self.contract, "pinned_public_key": self.service.public_key},
                         self.controller.manifest["external_confirmation"])
        prepared = self.controller.prepare_external_confirmation()
        identity = prepared["submission"]["identity"]
        self.assertEqual(self.generated["revision"]["revision_id"], identity["candidate"]["revision_id"])
        self.assertEqual(self.generated["revision"]["files"], identity["candidate"]["files"])
        self.assertEqual(self.generated["observation"]["observation_id"], identity["dev_observation_id"])
        frozen = self.controller.status()
        self.assertEqual("external_confirmation_pending", frozen["phase"])
        self.assertEqual(before["budget"]["spent"], frozen["budget"]["spent"])
        self.assertEqual(2.0, frozen["budget"]["reserved"])
        self.assertEqual(before["budget"]["available"] - 2, frozen["budget"]["available"])
        frozen_events = self.events()
        self.assertEqual(prepared, self.controller.prepare_external_confirmation())
        self.assertEqual(frozen_events, self.events())
        result = self.complete_fixture_runner(prepared["submission"])
        final = self.controller.accept_external_confirmation(result)
        self.assertEqual("concluded", final["phase"])
        self.assertTrue(final["integrity"]["ok"])
        confirmations = [row for row in final["observations"] if row["scope"] == "confirmation"]
        self.assertEqual(2, len(confirmations))
        self.assertEqual(before["observations"], [row for row in final["observations"] if row["scope"] == "dev"])
        selected_observation = next(row for row in confirmations if row["hypothesis_id"] == identity["hypothesis_id"])
        self.assertEqual(identity["design_id"], selected_observation["design_id"])
        self.assertEqual(identity["hypothesis_version"], selected_observation["hypothesis_version"])
        self.assertEqual(result["payload"]["candidate"]["mean"], selected_observation["value"])
        self.assertEqual(before["budget"]["spent"] + 2, final["budget"]["spent"])
        self.assertEqual(0.0, final["budget"]["reserved"])
        self.assertEqual(before["budget"]["cap"], final["budget"]["cap"])
        self.assertEqual(before["budget"]["family_id"], final["budget"]["family_id"])
        self.assertEqual([], self.controller.exp.results("test"))

        final_events = self.events()
        self.controller.close()
        self.controller = ResearchController(self.run_dir)
        with patch.object(self.controller.store.observations, "add",
                          side_effect=AssertionError("Duplicate observation")):
            replay = self.controller.accept_external_confirmation(result)
        self.assertEqual(final, replay)
        self.assertEqual(final_events, self.events())

    def test_freezing_blocks_research_implementation_and_local_confirmation(self):
        self.controller.prepare_external_confirmation()
        before = self.controller.status()
        before_events = self.events()
        with patch.object(self.policy, "choose", side_effect=AssertionError("No new decision")), \
                patch.object(self.policy, "propose_revision", side_effect=AssertionError("No new revision")), \
                patch.object(self.controller.exp, "evaluate", side_effect=AssertionError("No local evaluation")):
            self.assertEqual(before, self.controller.run(trusted_local=True, max_steps=1))
            with self.assertRaises(ProtocolError):
                self.controller.implement(self.selected["hypothesis_id"])
            with self.assertRaises(ProtocolError):
                self.controller.confirm(trusted_local=True)
        self.assertEqual(before, self.controller.status())
        self.assertEqual(before_events, self.events())
        self.assertEqual([], self.controller.exp.results("test"))

    def test_wrong_signature_and_tampered_result_have_no_acceptance_side_effects(self):
        prepared = self.controller.prepare_external_confirmation()
        result = self.complete_fixture_runner(prepared["submission"])
        invalid = [sign_payload(result["payload"], self.runner_key)]
        tampered = copy.deepcopy(result)
        tampered["payload"]["candidate"]["mean"] += 1
        invalid.append(tampered)
        before = self.controller.status()
        before_events = self.events()
        path = self.run_dir / "external-confirmation" / "result.json"
        for receipt in invalid:
            with self.subTest(receipt_public_key=receipt["public_key_id"]):
                with self.assertRaises(ProtocolError):
                    self.controller.accept_external_confirmation(receipt)
                self.assertEqual(before_events, self.events())
                self.assertEqual(before, self.controller.status())
                self.assertFalse(path.exists())
        accepted = self.controller.accept_external_confirmation(result)
        original = path.read_bytes()
        accepted_events = self.events()
        try:
            path.write_bytes(original + b" ")
            with self.assertRaises(ProtocolError):
                self.controller.accept_external_confirmation(result)
            self.assertEqual(accepted_events, self.events())
        finally:
            path.write_bytes(original)
        self.assertEqual(accepted, self.controller.status())

    def test_unenrolled_study_cannot_prepare_confirmation(self):
        other_dir = self.root / "not-enrolled"
        ResearchController.initialize(self.project, other_dir)
        other = ResearchController(other_dir)
        try:
            before = other.status()
            with self.assertRaises(ProtocolError):
                other.prepare_external_confirmation()
            self.assertEqual(before, other.status())
            self.assertFalse((other_dir / "external-confirmation").exists())
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
