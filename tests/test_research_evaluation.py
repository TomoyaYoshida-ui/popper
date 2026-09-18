import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from popper.core import EVALUATORS, ProtocolError, digest, file_hash, read_json, write_json
from popper.research import evaluation_service


class IndependentEvaluatorContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # 归一到无 8.3 短名的真实路径：产品侧对工程根做 resolve()，而 GitHub Windows
        # runner 的 TEMP 本身就是 C:\Users\RUNNER~1\... 形态。不归一，测试里的字面
        # 路径与产品里的解析路径永远对不上：写进契约的 dataset 字符串差一截，而按
        # `path == request_path` 打桩的用例会静默不匹配（注入没发生，断言却过了）。
        root = Path(self.temp.name).resolve()
        self.project = root / "project"
        self.project.mkdir()
        for split in ("train", "dev", "test"):
            write_json(self.project / f"{split}.json", [{"id": "a", "x": 1, "y": 2}])
        self.state = {
            "spec": {"train": "train.json", "dev": "dev.json", "test": "test.json",
                     "seeds": [11, 29]},
            "input_hashes": {f"{split}.json": file_hash(self.project / f"{split}.json")
                             for split in ("train", "dev", "test")},
            "evaluator_id": "mse-v1",
            "evaluator_hash": digest(EVALUATORS["mse-v1"]),
        }
        self.predictions = []
        for seed, value in ((11, 2), (29, 4)):
            path = self.project / f"predictions-{seed}.json"
            write_json(path, [{"id": "a", "prediction": value}])
            self.predictions.append({"seed": seed, "path": str(path), "sha256": file_hash(path)})
        self.output_dir = root / "evaluations"
        self.evaluator = evaluation_service.IndependentEvaluator(self.project, self.output_dir)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, request_id="R-valid"):
        return {
            "schema_version": "2.0", "request_id": request_id,
            "dataset": str(self.project / "dev.json"),
            "dataset_sha256": self.state["input_hashes"]["dev.json"],
            "evaluator_id": self.state["evaluator_id"],
            "evaluator_hash": self.state["evaluator_hash"],
            "expected_seeds": [11, 29],
            "scoring_code_sha256": evaluation_service.scoring_code_hash(),
            "predictions": copy.deepcopy(self.predictions),
        }

    def subprocess_response(self, mutate=lambda response: None):
        def run(command, **kwargs):
            request_path = Path(command[command.index("--request") + 1])
            response_path = Path(command[command.index("--response") + 1])
            response = evaluation_service.evaluate_request(read_json(request_path))
            mutate(response)
            # Deliberately allow NaN/Infinity so response validation, rather than
            # the trusted fixture serializer, must reject malformed values.
            response_path.write_text(json.dumps(response), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return run

    def test_v2_request_scores_all_registered_seeds_and_binds_implementation(self):
        request = self.request()
        result = evaluation_service.evaluate_request(request)
        self.assertEqual({
            "schema_version", "request_id", "request_sha256", "evaluator_id",
            "evaluator_hash", "per_seed", "mean", "std", "service_version",
            "service_sha256", "scoring_code_sha256", "trust",
        }, set(result))
        self.assertEqual("2.0", result["schema_version"])
        self.assertEqual(request["request_id"], result["request_id"])
        self.assertEqual(digest(request), result["request_sha256"])
        self.assertEqual([{"seed": 11, "value": 0.0}, {"seed": 29, "value": 4.0}],
                         result["per_seed"])
        self.assertEqual(2.0, result["mean"])
        self.assertAlmostEqual(math.sqrt(8), result["std"])
        self.assertEqual(evaluation_service.scoring_code_hash(), result["scoring_code_sha256"])
        self.assertEqual(file_hash(Path(evaluation_service.__file__)), result["service_sha256"])
        self.assertEqual(evaluation_service.SERVICE_VERSION, result["service_version"])
        self.assertEqual("separate_process_same_account", result["trust"])

    def test_v3_request_recomputes_preregistered_slice_scores(self):
        rows = [{"id": "inner", "x": 0.5, "y": 1.0},
                {"id": "outer", "x": 2.0, "y": 4.0}]
        write_json(self.project / "dev.json", rows)
        self.state["input_hashes"]["dev.json"] = file_hash(self.project / "dev.json")
        self.state["spec"]["analysis_slices"] = [
            {"slice_id": "s0", "rule": {"kind": "abs_x_le", "value": 1.0}},
            {"slice_id": "s1", "rule": {"kind": "abs_x_gt", "value": 1.0}},
        ]
        for item, offset in zip(self.predictions, (0.0, 1.0)):
            write_json(Path(item["path"]), [
                {"id": "inner", "prediction": 1.0 + offset},
                {"id": "outer", "prediction": 4.0 + offset},
            ])
            item["sha256"] = file_hash(item["path"])

        result = self.evaluator.score_artifacts(
            self.state, "dev", "R-sliced", self.predictions)
        request = read_json(self.output_dir / "R-sliced" / "request.json")

        self.assertEqual("3.0", request["schema_version"])
        self.assertEqual(["s0", "s1"], [item["slice_id"] for item in result["slices"]])
        self.assertEqual([1, 1], [item["n"] for item in result["slices"]])
        self.assertEqual([0.0, 1.0],
                         [point["value"] for point in result["slices"][0]["per_seed"]])

    def test_request_requires_exact_v2_fields_and_registered_implementation(self):
        invalid = []
        original = self.request()
        for field in original:
            missing = copy.deepcopy(original)
            missing.pop(field)
            invalid.append(missing)
        invalid.extend([
            {**original, "schema_version": "1.0"},
            {**original, "unexpected": True},
            {**original, "scoring_code_sha256": "0" * 64},
            {**original, "evaluator_hash": "0" * 64},
            {**original, "evaluator_id": "unknown"},
        ])
        for request in invalid:
            with self.subTest(fields=sorted(request), version=request.get("schema_version")):
                with self.assertRaises(ProtocolError):
                    evaluation_service.evaluate_request(request)

    def test_request_rejects_missing_extra_duplicate_or_noninteger_seeds(self):
        request = self.request()
        invalid_predictions = [
            [], self.predictions[:1],
            self.predictions + [{**self.predictions[0], "seed": 42}],
            [self.predictions[0], self.predictions[0]],
            [{**self.predictions[0], "seed": True}, self.predictions[1]],
            [{**self.predictions[0], "seed": 11.0}, self.predictions[1]],
            [{**self.predictions[0], "seed": "11"}, self.predictions[1]],
            [{**self.predictions[0], "unexpected": 1}, self.predictions[1]],
        ]
        for predictions in invalid_predictions:
            with self.subTest(predictions=predictions):
                with self.assertRaises(ProtocolError):
                    evaluation_service.evaluate_request({**request, "predictions": predictions})
        for seeds in ([], [11, 11], [True, 29], [11.0, 29], ["11", 29], "11,29"):
            with self.subTest(expected_seeds=seeds):
                with self.assertRaises(ProtocolError):
                    evaluation_service.evaluate_request({**request, "expected_seeds": seeds})

    def test_request_detects_tampered_prediction_artifact(self):
        request = self.request()
        write_json(Path(self.predictions[0]["path"]), [{"id": "a", "prediction": 999}])
        with self.assertRaises(ProtocolError):
            evaluation_service.evaluate_request(request)

    def test_score_artifacts_accepts_reordered_seeds_in_registered_order(self):
        result = self.evaluator.score_artifacts(
            self.state, "dev", "R-reordered", list(reversed(self.predictions)))
        request = read_json(self.output_dir / "R-reordered" / "request.json")
        self.assertEqual("2.0", request["schema_version"])
        self.assertEqual([11, 29], request["expected_seeds"])
        self.assertEqual([11, 29], [row["seed"] for row in request["predictions"]])
        self.assertEqual([11, 29], [row["seed"] for row in result["per_seed"]])
        self.assertEqual(2.0, result["mean"])
        self.assertEqual(evaluation_service.scoring_code_hash(), request["scoring_code_sha256"])
        self.assertEqual(file_hash(Path(result["artifact_id"])), result["artifact_sha256"])

    def test_score_artifacts_allows_test_split(self):
        with patch.object(evaluation_service.subprocess, "run",
                          side_effect=self.subprocess_response()) as run:
            result = self.evaluator.score_artifacts(self.state, "test", "R-test", self.predictions)
        self.assertEqual(1, run.call_count)
        self.assertEqual(2.0, result["mean"])
        request = read_json(self.output_dir / "R-test" / "request.json")
        self.assertEqual(str(self.project / "test.json"), request["dataset"])

    def test_disallowed_split_rejected_before_subprocess(self):
        for split in ("train", "confirmation", "", "unknown"):
            with self.subTest(split=split):
                with patch.object(evaluation_service.subprocess, "run") as run:
                    with self.assertRaises(ProtocolError):
                        self.evaluator.score_artifacts(self.state, split, "R-split", self.predictions)
                    run.assert_not_called()
        self.assertEqual([], list(self.output_dir.rglob("request.json")))

    def test_invalid_seed_manifest_rejected_before_request_or_subprocess(self):
        manifests = [self.predictions[:1], [], self.predictions + [self.predictions[0]],
                     self.predictions + [{**self.predictions[0], "seed": 42}]]
        for predictions in manifests:
            with self.subTest(seeds=[row["seed"] for row in predictions]):
                with patch.object(evaluation_service.subprocess, "run") as run:
                    with self.assertRaises(ProtocolError):
                        self.evaluator.score_artifacts(self.state, "dev", "R-seeds", predictions)
                    run.assert_not_called()
        self.assertEqual([], list(self.output_dir.rglob("request.json")))

    def test_original_data_tamper_rejected_without_writing_request(self):
        write_json(self.project / "dev.json", [{"id": "a", "x": 1, "y": 999}])
        with patch.object(evaluation_service.subprocess, "run") as run:
            with self.assertRaises(ProtocolError):
                self.evaluator.score_artifacts(self.state, "dev", "R-data-tamper", self.predictions)
            run.assert_not_called()
        self.assertEqual([], list(self.output_dir.rglob("request.json")))

    def test_request_ids_cannot_escape_or_alias_evidence_directories(self):
        invalid_ids = ["", ".", "..", "../escape", "a/b", "a\\b", "a:b", "with space",
                       " R1", "R1\n", "中文", "A" * 129, None, True, 123]
        for request_id in invalid_ids:
            with self.subTest(request_id=request_id):
                with self.assertRaises(ProtocolError):
                    evaluation_service.evaluate_request(self.request(request_id))
                with patch.object(evaluation_service.subprocess, "run") as run:
                    with self.assertRaises(ProtocolError):
                        self.evaluator.score_artifacts(self.state, "dev", request_id, self.predictions)
                    run.assert_not_called()
        self.assertEqual([], list(self.output_dir.rglob("request.json")))

    def test_forged_response_identity_fields_are_rejected(self):
        forged = {
            "schema_version": "1.0", "request_id": "R-unrelated",
            "request_sha256": "0" * 64, "evaluator_id": "binary-accuracy-v1",
            "evaluator_hash": "0" * 64, "service_version": "unrelated-service",
            "service_sha256": "0" * 64, "scoring_code_sha256": "0" * 64,
            "trust": "securely_verified",
        }
        for index, (field, value) in enumerate(forged.items()):
            with self.subTest(field=field):
                mutate = lambda response, key=field, replacement=value: response.update({key: replacement})
                with patch.object(evaluation_service.subprocess, "run",
                                  side_effect=self.subprocess_response(mutate)):
                    with self.assertRaises(ProtocolError):
                        self.evaluator.score_artifacts(
                            self.state, "dev", f"R-identity-{index}", self.predictions)

    def test_forged_response_statistics_and_seed_values_are_rejected(self):
        forged = [
            {"mean": 999.0}, {"std": 999.0}, {"mean": True}, {"std": False},
            {"mean": float("nan")}, {"std": float("inf")},
            {"per_seed": []},
            {"per_seed": [{"seed": 11, "value": 0.0}]},
            {"per_seed": [{"seed": 11, "value": 0.0}, {"seed": 11, "value": 4.0}]},
            {"per_seed": [{"seed": 11, "value": 0.0}, {"seed": 42, "value": 4.0}]},
            {"per_seed": [{"seed": 11, "value": 0.0}, {"seed": True, "value": 4.0}]},
            {"per_seed": [{"seed": 11, "value": True}, {"seed": 29, "value": 4.0}]},
            {"per_seed": [{"seed": 11, "value": float("nan")}, {"seed": 29, "value": 4.0}]},
            {"per_seed": [{"seed": 11, "value": 0.0}, {"seed": 29, "value": float("inf")}]},
            {"per_seed": [{"seed": 11, "value": "0"}, {"seed": 29, "value": 4.0}]},
            {"per_seed": [{"seed": 11, "value": 0.0, "unexpected": 1},
                          {"seed": 29, "value": 4.0}]},
        ]
        for index, replacement in enumerate(forged):
            with self.subTest(replacement=replacement):
                mutate = lambda response, value=replacement: response.update(value)
                with patch.object(evaluation_service.subprocess, "run",
                                  side_effect=self.subprocess_response(mutate)):
                    with self.assertRaises(ProtocolError):
                        self.evaluator.score_artifacts(
                            self.state, "dev", f"R-numeric-{index}", self.predictions)

    def test_response_requires_exact_fields(self):
        mutations = [lambda response: response.update(unexpected=True),
                     lambda response: response.pop("std")]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                with patch.object(evaluation_service.subprocess, "run",
                                  side_effect=self.subprocess_response(mutate)):
                    with self.assertRaises(ProtocolError):
                        self.evaluator.score_artifacts(
                            self.state, "dev", f"R-fields-{index}", self.predictions)

    def test_negative_std_is_rejected_even_within_zero_comparison_tolerance(self):
        same_score = copy.deepcopy(self.predictions)
        second_path = Path(same_score[1]["path"])
        write_json(second_path, [{"id": "a", "prediction": 2}])
        same_score[1]["sha256"] = file_hash(second_path)

        def tiny_negative_std(response):
            self.assertEqual([0.0, 0.0], [row["value"] for row in response["per_seed"]])
            self.assertEqual(0.0, response["std"])
            response["std"] = -1e-14

        with patch.object(evaluation_service.subprocess, "run",
                          side_effect=self.subprocess_response(tiny_negative_std)) as run:
            with self.assertRaises(ProtocolError):
                self.evaluator.score_artifacts(
                    self.state, "dev", "R-negative-std", same_score)
        run.assert_called_once()

    def test_same_request_id_with_different_request_cannot_overwrite_evidence(self):
        request_id = "R-preserve"
        with patch.object(evaluation_service.subprocess, "run",
                          side_effect=self.subprocess_response()) as run:
            original = self.evaluator.score_artifacts(self.state, "dev", request_id, self.predictions)
        run.assert_called_once()
        target = self.output_dir / request_id
        original_request = (target / "request.json").read_bytes()
        original_response = (target / "response.json").read_bytes()
        changed_path = self.project / "changed-predictions-11.json"
        write_json(changed_path, [{"id": "a", "prediction": 1}])
        changed = [{"seed": 11, "path": str(changed_path), "sha256": file_hash(changed_path)},
                   self.predictions[1]]

        with patch.object(evaluation_service.subprocess, "run") as run:
            with self.assertRaises(ProtocolError):
                self.evaluator.score_artifacts(self.state, "dev", request_id, changed)
            run.assert_not_called()
        self.assertEqual(original_request, (target / "request.json").read_bytes())
        self.assertEqual(original_response, (target / "response.json").read_bytes())
        self.assertEqual(original["artifact_sha256"], file_hash(target / "response.json"))

    def test_concurrent_request_creation_preserves_conflicting_request(self):
        request_id = "R-concurrent"
        request_path = self.output_dir / request_id / "request.json"
        competing = self.request(request_id)
        alternate_path = self.project / "competing-predictions-11.json"
        write_json(alternate_path, [{"id": "a", "prediction": 7}])
        competing["predictions"][0] = {
            "seed": 11, "path": str(alternate_path), "sha256": file_hash(alternate_path),
        }
        competing_text = json.dumps(competing)
        actual_open = Path.open
        injected = []

        def interleave_creation(path, mode="r", *args, **kwargs):
            if path == request_path and mode == "x":
                # The competing writer wins after the existence check but
                # before this evaluator's exclusive open reaches the OS.
                self.assertFalse(path.exists())
                with actual_open(path, "x", encoding="utf-8") as stream:
                    stream.write(competing_text)
                injected.append(path)
            # Execute the actual exclusive open: the filesystem must raise
            # FileExistsError, exercising the conflict-handling branch.
            return actual_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", new=interleave_creation):
            with patch.object(evaluation_service.subprocess, "run") as run:
                with self.assertRaises(ProtocolError):
                    self.evaluator.score_artifacts(
                        self.state, "dev", request_id, self.predictions)
                run.assert_not_called()
        self.assertEqual([request_path], injected)
        self.assertEqual(competing_text.encode("utf-8"), request_path.read_bytes())
        self.assertEqual(competing, read_json(request_path))
        self.assertFalse((request_path.parent / "response.json").exists())


if __name__ == "__main__":
    unittest.main()
