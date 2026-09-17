import runpy
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from popper.core import EVALUATORS, ProtocolError, digest, file_hash, initialize, read_json
from popper.research.actions import (ADD_CONTROL, REQUEST_CONFIRMATION,
                                     RUN_EXPERIMENT, STOP)
from popper.research.controller import ResearchController
from popper.research.evaluation_service import evaluate_request, scoring_code_hash
from popper.research.models import EvidenceDrivenPolicy
from popper.research.models import DeepSeekResearchPolicy


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class EvidencePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = EvidenceDrivenPolicy()

    def test_result_sign_changes_scientific_action(self):
        self.assertEqual(
            REQUEST_CONFIRMATION,
            self.policy.after_observation("H1", 0.2, 0.1, True).kind)
        self.assertEqual(
            ADD_CONTROL,
            self.policy.after_observation("H1", -0.2, 0.1, True).kind)
        uncertain = self.policy.after_observation("H1", 0.01, 0.1, False)
        self.assertEqual(STOP, uncertain.kind)
        self.assertIn("证据不足", uncertain.rationale)

    def test_policy_never_selects_already_tested_hypothesis(self):
        context = {"candidates": [
            {"hypothesis_id": "H1", "status": "contradicted_in_scope", "config": {"x": 1}},
            {"hypothesis_id": "H2", "status": "untested", "config": {"x": 2}},
        ]}
        proposal = self.policy.choose(context)
        self.assertEqual(RUN_EXPERIMENT, proposal.kind)
        self.assertEqual("H2", proposal.hypothesis_id)


class DeepSeekPolicyContractTests(unittest.TestCase):
    def _policy(self, response):
        policy = object.__new__(DeepSeekResearchPolicy)
        policy.model = "deepseek-flash"
        policy.name = "deepseek_json_policy"
        policy._call = lambda *_: response
        return policy

    def test_generated_hypotheses_are_index_bound(self):
        response = {"hypotheses": [
            {"candidate_index": 1, "mechanism": "m1", "applicability": "a1",
             "predictions": ["p1"], "falsification": ["f1"], "alternatives": ["x1"]},
            {"candidate_index": 0, "mechanism": "m0", "applicability": "a0",
             "predictions": ["p0"], "falsification": ["f0"], "alternatives": ["x0"]},
        ]}
        rows = self._policy(response).generate_hypotheses("q", [{"x": 0}, {"x": 1}])
        self.assertEqual("m0", rows[0]["mechanism"])
        self.assertEqual("m1", rows[1]["mechanism"])

    def test_model_cannot_select_unregistered_hypothesis(self):
        policy = self._policy({"kind": RUN_EXPERIMENT, "hypothesis_id": "FAKE",
                               "rationale": "try", "alternatives": []})
        context = {"study": {"question": "q"},
                   "metric": {"name": "mse", "direction": "min"},
                   "min_meaningful_effect": 0.1,
                   "budget": {"available": 1}, "observations": [],
                   "candidates": [{"hypothesis_id": "H1", "status": "untested",
                                   "config": {"x": 1}}]}
        with self.assertRaisesRegex(ProtocolError, "不存在或已检验"):
            policy.choose(context)

    def test_invalid_choice_gets_one_bounded_correction_with_original_context(self):
        policy = self._policy(None)
        invalid = {"kind": RUN_EXPERIMENT, "hypothesis_id": "FAKE",
                   "rationale": "try", "alternatives": []}
        valid = {"kind": RUN_EXPERIMENT, "hypothesis_id": "H1",
                 "rationale": "registered", "alternatives": []}
        policy._call = Mock(side_effect=[invalid, valid])
        context = {"study": {"question": "q"},
                   "metric": {"name": "mse", "direction": "min"},
                   "min_meaningful_effect": 0.1,
                   "budget": {"available": 1}, "observations": [{"value": 2.0}],
                   "candidates": [{"hypothesis_id": "H1", "status": "untested",
                                   "config": {"x": 1}}]}

        proposal = policy.choose(context)

        self.assertEqual("H1", proposal.hypothesis_id)
        self.assertEqual(2, policy._call.call_count)
        first_payload = policy._call.call_args_list[0].args[1]
        correction = policy._call.call_args_list[1].args[1]
        self.assertEqual(first_payload, correction["research_context"])
        self.assertEqual(invalid, correction["previous_invalid_response"])
        self.assertIn("hypothesis_id", correction["validation_error"])

    def test_repeated_invalid_choice_stops_after_two_calls(self):
        policy = self._policy(None)
        invalid = {"kind": RUN_EXPERIMENT, "hypothesis_id": "FAKE",
                   "rationale": "try", "alternatives": []}
        policy._call = Mock(side_effect=[invalid, dict(invalid)])
        context = {"study": {"question": "q"},
                   "metric": {"name": "mse", "direction": "min"},
                   "min_meaningful_effect": 0.1, "budget": {"available": 1},
                   "observations": [],
                   "candidates": [{"hypothesis_id": "H1", "status": "untested",
                                   "config": {"x": 1}}]}
        with self.assertRaisesRegex(ProtocolError, "hypothesis_id"):
            policy.choose(context)
        self.assertEqual(2, policy._call.call_count)

    def test_code_proposal_accepts_replacement_and_rejects_source_alias(self):
        for key in ("replacement", "source"):
            with self.subTest(key=key):
                policy = self._policy({"edits": [{"path": "helper.py",
                    "original_sha256": None, key: "x = 1\n"}], "rationale": "helper"})
                if key == "replacement":
                    result = policy.propose_revision("q", {}, {}, [])
                    self.assertEqual("x = 1\n", result["edits"][0].replacement)
                else:
                    with self.assertRaisesRegex(ProtocolError, "字段不正确"):
                        policy.propose_revision("q", {}, {}, [])

    def test_empty_code_proposal_accepts_registered_implementation(self):
        policy = self._policy(None)
        valid = {"edits": [], "rationale": "already supported"}
        policy._call = Mock(return_value=valid)
        code_files = [{"path": "model.py", "sha256": "a" * 64, "content": "x = 0\n"}]

        result = policy.propose_revision("q", {"hypothesis_id": "H1"}, {"x": 1}, code_files)

        self.assertEqual((), result["edits"])
        self.assertEqual(1, policy._call.call_count)

    def test_repeated_malformed_code_proposal_stops_after_two_calls(self):
        policy = self._policy(None)
        invalid = {"edits": None, "rationale": "already supported"}
        policy._call = Mock(side_effect=[invalid, dict(invalid)])
        with self.assertRaisesRegex(ProtocolError, "edits"):
            policy.propose_revision("q", {"hypothesis_id": "H1"}, {"x": 1}, [])
        self.assertEqual(2, policy._call.call_count)


class IndependentEvaluationTests(unittest.TestCase):
    def test_request_recomputes_prediction_and_rejects_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = root / "data.json"
            predictions = root / "predictions.json"
            data.write_text('[{"id":"a","x":1,"y":2}]', encoding="utf-8")
            predictions.write_text('[{"id":"a","prediction":2}]', encoding="utf-8")
            request = {"schema_version": "2.0", "request_id": "R1",
                       "dataset": str(data), "dataset_sha256": file_hash(data),
                       "evaluator_id": "mse-v1",
                       "evaluator_hash": digest(EVALUATORS["mse-v1"]),
                       "expected_seeds": [1], "scoring_code_sha256": scoring_code_hash(),
                       "predictions": [{"seed": 1, "path": str(predictions),
                                        "sha256": file_hash(predictions)}]}
            self.assertEqual(0.0, evaluate_request(request)["mean"])
            predictions.write_text('[]', encoding="utf-8")
            with self.assertRaisesRegex(ProtocolError, "摘要变化"):
                evaluate_request(request)


class ResearchControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.run_dir = Path(self.temp.name) / "research-run"
        self.root.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.root / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.root)
        initialize(self.root)
        self.controller = None

    def tearDown(self):
        if self.controller:
            self.controller.close()
        self.temp.cleanup()

    def _open(self):
        ResearchController.initialize(self.root, self.run_dir)
        self.controller = ResearchController(self.run_dir)
        return self.controller

    def test_init_materializes_hypotheses_and_frozen_designs(self):
        controller = self._open()
        status = controller.status()
        self.assertEqual("researching", status["phase"])
        self.assertEqual(3, len(status["candidates"]))
        self.assertTrue(status["integrity"]["ok"])
        self.assertEqual("registered_hypothesis_baseline", status["capability_mode"])
        self.assertEqual(4, len(controller.store.list("design")))
        self.assertTrue(all(d["status"] == "frozen" for d in controller.store.list("design")))

    def test_real_adaptive_loop_and_confirmation(self):
        controller = self._open()
        status = controller.run(trusted_local=True, auto_confirm=True)
        self.assertEqual("concluded", status["phase"])
        self.assertTrue(status["integrity"]["ok"])
        self.assertEqual(2, len([o for o in status["observations"]
                                 if o["scope"] == "confirmation"]))
        self.assertTrue(any(d["action"] == REQUEST_CONFIRMATION
                            for d in status["decisions"]))
        self.assertTrue(any(c["status"] == "supported_in_scope"
                            for c in status["candidates"]))
        self.assertGreater(status["budget"]["spent"], 0)
        self.assertEqual("separate_process_same_account", status["evaluation_trust"])

    def test_resume_does_not_repeat_completed_side_effects(self):
        controller = self._open()
        first = controller.run(trusted_local=True, max_steps=1)
        observation_count = len(first["observations"])
        spent = first["budget"]["spent"]
        controller.close()
        self.controller = ResearchController(self.run_dir)
        second = self.controller.run(trusted_local=True, max_steps=1)
        self.assertGreaterEqual(len(second["observations"]), observation_count)
        self.assertGreaterEqual(second["budget"]["spent"], spent)
        ids = [o["observation_id"] for o in second["observations"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_manifest_tamper_blocks_resume(self):
        controller = self._open()
        controller.close()
        self.controller = None
        manifest = read_json(self.run_dir / "research.json")
        manifest["min_meaningful_effect"] = 0
        from popper.core import write_json
        write_json(self.run_dir / "research.json", manifest)
        with self.assertRaisesRegex(ProtocolError, "manifest 已修改"):
            ResearchController(self.run_dir)

    def test_registered_scoring_implementation_cannot_change_mid_study(self):
        controller = self._open()
        self.assertEqual(scoring_code_hash(), controller.manifest["scoring_code_sha256"])
        with patch("popper.research.controller.scoring_code_hash", return_value="0" * 64):
            with self.assertRaisesRegex(ProtocolError, "计分实现"):
                controller.status()

    def test_legacy_study_is_readable_but_cannot_mix_new_scoring_semantics(self):
        # Build a historical manifest through the normal registration path, so
        # its event chain and scope hash remain valid without a scorer binding.
        from popper.core import write_json as original_write_json

        def write_legacy(path, value):
            if Path(path).name == "research.json":
                value = {k: v for k, v in value.items() if k != "scoring_code_sha256"}
            return original_write_json(path, value)

        with patch("popper.research.controller.write_json", side_effect=write_legacy):
            controller = self._open()
        before = controller.status()
        self.assertTrue(before["integrity"]["ok"])
        candidate = before["candidates"][0]["hypothesis_id"]
        for action in (lambda: controller.run(trusted_local=True),
                       lambda: controller.implement(candidate),
                       lambda: controller.confirm(trusted_local=True)):
            with self.assertRaisesRegex(ProtocolError, "只允许读取和审计"):
                action()
        after = controller.status()
        self.assertEqual(before["budget"], after["budget"])
        self.assertEqual(before["integrity"], after["integrity"])
        self.assertEqual([], after["observations"])

    def test_evaluation_receipt_tamper_blocks_further_research(self):
        controller = self._open()
        status = controller.run(trusted_local=True, max_steps=0)
        baseline = status["observations"][0]
        Path(baseline["artifact_id"]).write_text('{"mean": 999}', encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "完整性"):
            controller.status()

    def test_missing_sandbox_backend_points_to_trusted_local(self):
        """C7：本平台没有沙箱后端时，要让用户看到可行路径而不是两边都被堵死。"""
        controller = self._open()
        calls = {
            "run --sandbox": lambda: controller.run(sandboxed=True),
            "run --sandbox --autonomous-code": lambda: controller.run(
                sandboxed=True, autonomous_code=True),
            "confirm --sandbox": lambda: controller.confirm(sandboxed=True),
        }
        with patch("popper.sandbox.available", return_value=False):
            for label, call in calls.items():
                with self.subTest(label):
                    with self.assertRaises(ProtocolError) as caught:
                        call()
                    message = str(caught.exception)
                    self.assertIn("--trusted-local", message)
                    self.assertIn("--autonomous-code", message)

    def test_available_sandbox_backend_is_not_blocked(self):
        controller = self._open()
        with patch("popper.sandbox.available", return_value=True):
            controller._require_sandbox_backend()


if __name__ == "__main__":
    unittest.main()
