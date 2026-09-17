import copy
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from popper.core import EVALUATORS, ProtocolError, digest, write_json
from popper.research.confirmation_contracts import (load_private_key, public_key_b64,
    sign_payload, validate_contract, validate_result, validate_ticket, verify_envelope)
from popper.research.confirmation_service import HoldoutService


class HoldoutServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rows = [{"id": "a", "x": 1, "y": 2}, {"id": "b", "x": 2, "y": 4}]
        self.split_rows = {
            "train": [{"id": "train-a", "x": -1, "y": -2}, {"id": "train-b", "x": -2, "y": -4}],
            "dev": [{"id": "dev-a", "x": 3, "y": 6}, {"id": "dev-b", "x": 4, "y": 8}],
            "holdout": self.rows}
        for split, rows in self.split_rows.items():
            write_json(self.root / f"{split}.json", rows)
        self.runner_key = load_private_key(self.root / "runner-key.pem", create=True)
        self.service = HoldoutService(self.root / "service")
        self.contract = self.register()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def register(self, group="group-1", **overrides):
        kwargs = dict(dataset_id="dataset-1", dataset_version="v1", evaluation_group=group,
            train_path=self.root / "train.json", dev_path=self.root / "dev.json",
            holdout_path=self.root / "holdout.json", evaluator_id="mse-v1", seeds=[11, 29],
            min_effect=0.1, runtime_id="runtime-1", runner_public_key=public_key_b64(self.runner_key))
        kwargs.update(overrides)
        return self.service.register(**kwargs)

    def submission(self, contract=None, family="family-1"):
        envelope = contract or self.contract
        contract = envelope["payload"]
        identity = {"study_id": "study-1", "family_id": family,
            "contract_id": contract["contract_id"], "contract_sha256": digest(envelope),
            "source_manifest_sha256": "a" * 64, "hypothesis_id": "H-1", "hypothesis_version": 1,
            "design_id": "D-1", "dev_observation_id": "OBS-1", "dev_observation_sha256": "b" * 64,
            "train_sha256": contract["train_sha256"], "dev_sha256": contract["dev_sha256"],
            "metric": EVALUATORS[contract["evaluator_id"]]["metric"],
            "evaluator_id": contract["evaluator_id"], "seeds": contract["seeds"],
            "min_effect": contract["min_effect"],
            "control": {"config": {"degree": 1}, "entrypoint": "model.py", "files": {"model.py": "1" * 64}},
            "candidate": {"config": {"degree": 2}, "entrypoint": "model.py", "files": {"model.py": "2" * 64},
                "revision_id": "REV-" + "3" * 24, "revision_manifest_sha256": "4" * 64,
                "execution_changes": {"model.py": [1]}}}
        return {"schema_version": "1.0", "kind": "confirmation_submission",
                "submission_id": "SUB-" + digest(identity)[:24], "identity": identity}

    def predictions(self):
        return {arm: [{"seed": seed, "rows": [{"id": row["id"],
            "prediction": row["y"] if arm == "candidate" else 0} for row in self.rows]}
            for seed in [11, 29]] for arm in ("control", "candidate")}

    def receipt(self, ticket, submission, predictions, contract=None, **overrides):
        contract = (contract or self.contract)["payload"]
        ticket = ticket["payload"]
        payload = {"schema_version": "1.0", "kind": "confirmation_runner_receipt",
            "ticket_id": ticket["ticket_id"], "contract_id": ticket["contract_id"],
            "submission_id": ticket["submission_id"], "submission_sha256": ticket["submission_sha256"],
            "runtime_id": contract["runtime_id"], "backend": contract["allowed_backend"],
            "status": "succeeded", "code_hashes": {arm: submission["identity"][arm]["files"]
                for arm in ("control", "candidate")}, "predictions_sha256": digest(predictions),
            "error_type": None}
        payload.update(overrides)
        return sign_payload(payload, self.runner_key)

    def start(self, contract=None):
        submission = self.submission(contract)
        ticket = self.service.begin(submission)
        predictions = self.predictions()
        return submission, ticket, predictions, self.receipt(ticket, submission, predictions, contract)

    def test_register_is_fixed_signed_and_private(self):
        self.assertEqual(self.contract, self.register())
        self.assertEqual("engineering_same_account", validate_contract(
            self.contract, self.service.public_key)["trust"])
        self.assertNotIn(str(self.root), str(self.contract))
        with self.assertRaises(ProtocolError):
            self.register(min_effect=0.2)
        damaged = copy.deepcopy(self.contract)
        damaged["payload"]["min_effect"] = 0
        with self.assertRaises(ProtocolError):
            validate_contract(damaged, self.service.public_key)
        original = self.service.public_key
        self.service.close()
        self.service = HoldoutService(self.root / "service")
        self.assertEqual(original, self.service.public_key)
        self.assertEqual(self.contract, self.register())

    def test_register_rejects_ids_shared_between_any_two_splits(self):
        for left, right in (("train", "dev"), ("train", "holdout"), ("dev", "holdout")):
            with self.subTest(left=left, right=right):
                bad = [{"id": self.split_rows[left][0]["id"], "x": 100, "y": 200}]
                write_json(self.root / "overlap.json", bad)
                with self.assertRaisesRegex(ProtocolError, "id 重叠"):
                    self.register("overlapping-id", **{right + "_path": self.root / "overlap.json"})
        self.assertEqual(1, self.service._db.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])

    def test_register_rejects_renamed_duplicate_samples_between_any_two_splits(self):
        for left, right in (("train", "dev"), ("train", "holdout"), ("dev", "holdout")):
            with self.subTest(left=left, right=right):
                bad = [{**self.split_rows[left][0], "id": "renamed-sample"}]
                write_json(self.root / "duplicate.json", bad)
                with self.assertRaisesRegex(ProtocolError, "重复样本"):
                    self.register("duplicate-content", **{right + "_path": self.root / "duplicate.json"})
        self.assertEqual(1, self.service._db.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])

    def test_register_rejects_cross_split_classification_dimension_mismatch(self):
        for name, rows in {
            "train": [{"id": "train", "features": [1, 2], "label": 0}],
            "dev": [{"id": "dev", "features": [3], "label": 1}],
            "holdout": [{"id": "holdout", "features": [4, 5], "label": 0}],
        }.items():
            write_json(self.root / f"binary-{name}.json", rows)
        with self.assertRaisesRegex(ProtocolError, "特征维度不一致"):
            self.register("dimension-mismatch", evaluator_id="binary-accuracy-v1",
                **{name + "_path": self.root / f"binary-{name}.json" for name in ("train", "dev", "holdout")})
        self.assertEqual(1, self.service._db.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])

    def test_begin_consumes_service_group_before_releasing_features(self):
        submission, ticket, _, _ = self.start()
        self.assertEqual(ticket, self.service.begin(submission))
        validate_ticket(ticket, self.contract, submission, self.service.public_key)
        self.assertEqual([{"id": "a", "x": 1}, {"id": "b", "x": 2}], self.service.features(ticket))
        with self.assertRaisesRegex(ProtocolError, "已消费"):
            self.service.begin(self.submission(family="different-family"))
        forged = sign_payload(ticket["payload"], self.runner_key)
        with self.assertRaises(ProtocolError):
            self.service.features(forged)

    def test_complete_scores_all_seeds_and_replays_without_scoring(self):
        submission, ticket, predictions, receipt = self.start()
        result = self.service.complete(ticket, receipt, predictions)
        payload = validate_result(result, self.contract, submission, self.service.public_key)
        self.assertEqual(10, payload["control"]["mean"])
        self.assertEqual(0, payload["candidate"]["mean"])
        self.assertEqual(10, payload["effect"])
        self.assertTrue(payload["passed"])
        self.assertEqual([11, 29], [row["seed"] for row in payload["candidate"]["per_seed"]])
        with patch("popper.research.confirmation_service.score", side_effect=AssertionError("rescore")):
            self.assertEqual(result, self.service.complete(ticket, receipt, predictions))
        with patch("popper.research.confirmation_service.confirmation_service_hash", return_value="0" * 64):
            with self.assertRaisesRegex(ProtocolError, "服务实现"):
                self.service.complete(ticket, receipt, predictions)
        with self.assertRaises(ProtocolError):
            self.service.features(ticket)
        altered = copy.deepcopy(predictions)
        altered["candidate"][0]["rows"][0]["prediction"] = 1
        with self.assertRaisesRegex(ProtocolError, "终态"):
            self.service.complete(ticket, self.receipt(ticket, submission, altered), altered)

    def test_incomplete_duplicate_or_boolean_seeds_are_signed_terminal_failures(self):
        for index, kind in enumerate(("missing", "duplicate", "boolean")):
            with self.subTest(kind=kind):
                contract = self.register(f"seed-{index}")
                submission, ticket, predictions, _ = self.start(contract)
                if kind == "missing":
                    predictions["candidate"].pop()
                elif kind == "duplicate":
                    predictions["candidate"][1]["seed"] = 11
                else:
                    predictions["candidate"][0]["seed"] = True
                receipt = self.receipt(ticket, submission, predictions, contract)
                result = self.service.complete(ticket, receipt, predictions)
                payload = validate_result(result, contract, submission, self.service.public_key)
                self.assertEqual("failed", payload["status"])
                self.assertIsNone(payload["effect"])
                self.assertEqual(result, self.service.complete(ticket, receipt, predictions))

    def test_runner_signature_runtime_backend_code_and_prediction_bindings(self):
        cases = [{"runtime_id": "wrong"}, {"backend": "untrusted"},
                 {"code_hashes": {"control": {}, "candidate": {}}},
                 {"predictions_sha256": "0" * 64}, {"submission_sha256": "0" * 64},
                 {"ticket_id": "wrong"}, {"signature": "wrong"}]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                contract = self.register(f"runner-{index}")
                submission, ticket, predictions, receipt = self.start(contract)
                if "signature" in changes:
                    receipt = sign_payload(receipt["payload"], self.service._key)
                else:
                    receipt = self.receipt(ticket, submission, predictions, contract, **changes)
                with patch("popper.research.confirmation_service.score") as scorer:
                    result = self.service.complete(ticket, receipt, predictions)
                scorer.assert_not_called()
                self.assertEqual("failed", validate_result(
                    result, contract, submission, self.service.public_key)["status"])

    def test_scorer_failure_does_not_allow_new_predictions_or_second_scoring(self):
        submission, ticket, predictions, receipt = self.start()
        with patch("popper.research.confirmation_service.score", side_effect=RuntimeError("private detail")):
            result = self.service.complete(ticket, receipt, predictions)
        payload = validate_result(result, self.contract, submission, self.service.public_key)
        self.assertEqual("failed", payload["status"])
        self.assertEqual("RuntimeError", payload["error_type"])
        self.assertNotIn("private detail", str(result))
        self.service.close()
        self.service = HoldoutService(self.root / "service")
        with patch("popper.research.confirmation_service.score", side_effect=AssertionError("rescore")):
            self.assertEqual(result, self.service.complete(ticket, receipt, predictions))

    def test_scoring_interruption_can_only_be_failed_explicitly(self):
        submission, ticket, predictions, receipt = self.start()
        with patch("popper.research.confirmation_service.score", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.complete(ticket, receipt, predictions)
        self.service.close()
        self.service = HoldoutService(self.root / "service")
        with self.assertRaisesRegex(ProtocolError, "不能重算"):
            self.service.complete(ticket, receipt, predictions)
        result = self.service.fail_interrupted(ticket)
        self.assertEqual("Interrupted", validate_result(
            result, self.contract, submission, self.service.public_key)["error_type"])
        self.assertEqual(result, self.service.complete(ticket, receipt, predictions))
        self.assertEqual(result, self.service.fail_interrupted(ticket))

    def test_interrupted_issued_ticket_never_accepts_later_runner_output(self):
        submission, ticket, predictions, receipt = self.start()
        result = self.service.fail_interrupted(ticket)
        payload = validate_result(result, self.contract, submission, self.service.public_key)
        self.assertEqual(digest(None), payload["runner_receipt_sha256"])
        self.assertEqual("InterruptedBeforeReceipt", payload["error_type"])
        with self.assertRaisesRegex(ProtocolError, "终态"):
            self.service.complete(ticket, receipt, predictions)
        with self.assertRaises(ProtocolError):
            self.service.begin(self.submission(family="another-family"))

    def test_concurrent_families_cannot_consume_one_group_twice(self):
        barrier = threading.Barrier(2)

        def attempt(family):
            service = HoldoutService(self.root / "service")
            try:
                barrier.wait(timeout=5)
                try:
                    return service.begin(self.submission(family=family))
                except ProtocolError:
                    return None
            finally:
                service.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["family-a", "family-b"]))
        self.assertEqual(1, sum(result is not None for result in results))
        self.assertEqual(1, self.service._db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0])

    def test_private_label_or_service_change_fails_closed_with_signed_result(self):
        submission, ticket, predictions, receipt = self.start()
        with patch("popper.research.confirmation_service.confirmation_service_hash", return_value="0" * 64):
            with self.assertRaisesRegex(ProtocolError, "服务实现"):
                self.service.begin(submission)
            result = self.service.complete(ticket, receipt, predictions)
        self.assertEqual("failed", validate_result(
            result, self.contract, submission, self.service.public_key)["status"])
        other = self.register("labels-change")
        submission, ticket, predictions, receipt = self.start(other)
        with self.service._db:
            self.service._db.execute("UPDATE contracts SET labels=? WHERE contract_id=?",
                (b"[]", other["payload"]["contract_id"]))
        result = self.service.complete(ticket, receipt, predictions)
        self.assertEqual("failed", validate_result(
            result, other, submission, self.service.public_key)["status"])

    def test_binary_accuracy_is_independently_recomputed(self):
        rows = [{"id": "a", "features": [1], "label": 0},
                {"id": "b", "features": [2], "label": 1}]
        write_json(self.root / "binary.json", rows)
        write_json(self.root / "binary-train.json", [{"id": "train", "features": [-2], "label": 0}])
        write_json(self.root / "binary-dev.json", [{"id": "dev", "features": [-1], "label": 1}])
        contract = self.register("binary", holdout_path=self.root / "binary.json",
                                 train_path=self.root / "binary-train.json", dev_path=self.root / "binary-dev.json",
                                 evaluator_id="binary-accuracy-v1")
        submission = self.submission(contract)
        ticket = self.service.begin(submission)
        predictions = {arm: [{"seed": seed, "rows": [{"id": row["id"],
            "prediction": row["label"] if arm == "candidate" else 0} for row in rows]}
            for seed in [11, 29]] for arm in ("control", "candidate")}
        result = self.service.complete(ticket, self.receipt(ticket, submission, predictions, contract), predictions)
        payload = validate_result(result, contract, submission, self.service.public_key)
        self.assertEqual(0.5, payload["effect"])
        self.assertTrue(payload["passed"])


if __name__ == "__main__":
    unittest.main()
