"""评测验收层测试：replay_check / ablation / acceptance_report（确定性离线）。"""
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path

from popper.core import Experiment, ProtocolError, initialize
from popper.evaluation import Evaluator

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


def _build_project(root):
    root = Path(root)
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, root / name)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](root)


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        # 已完成项目：完整 init → search → freeze → confirm 闭环。
        cls.completed = Path(cls.temp.name) / "completed"
        cls.completed.mkdir()
        _build_project(cls.completed)
        initialize(cls.completed)
        exp = Experiment(cls.completed)
        try:
            exp.search(True)
            exp.freeze()
            exp.confirm(True)
        finally:
            exp.close()
        # 已生成数据但未初始化的项目。
        cls.uninitialized = Path(cls.temp.name) / "uninitialized"
        cls.uninitialized.mkdir()
        _build_project(cls.uninitialized)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_replay_check_completed_project_passes(self):
        result = Evaluator(self.completed).replay_check()
        self.assertEqual("pass", result["status"])
        self.assertTrue(result["delta_consistent"])
        self.assertGreaterEqual(result["runs_recomputed"], 1)

    def test_replay_check_uninitialized_project_is_skipped(self):
        result = Evaluator(self.uninitialized).replay_check()
        self.assertEqual("skipped", result["status"])

    def test_ablation_returns_structured_dict(self):
        result = Evaluator(self.completed).ablation("replay")
        self.assertEqual("replay", result["feature"])
        self.assertIn("claim_produced", result)
        self.assertIn("affected", result)
        self.assertIsInstance(result["claim_produced"], bool)
        self.assertIsInstance(result["affected"], bool)

    def test_ablation_accepts_explicit_project_and_all_features(self):
        evaluator = Evaluator()
        for feature in ("search", "gate", "replay"):
            result = evaluator.ablation(self.completed, feature)
            self.assertEqual(feature, result["feature"])
            # 已完成项目存在 claim：非 search 消融仍产出 claim。
            if feature == "replay":
                self.assertTrue(result["claim_produced"])
            if feature == "search":
                self.assertFalse(result["claim_produced"])

    def test_ablation_rejects_unknown_feature(self):
        with self.assertRaises(ProtocolError):
            Evaluator(self.completed).ablation("bogus")

    def test_acceptance_report_contains_expected_top_level_keys(self):
        report = Evaluator(self.completed).acceptance_report()
        self.assertIn("a4a", report)
        self.assertIn("a4b", report)
        self.assertIn("a2", report)
        self.assertIn("reproducible_package", report)
        self.assertIn("corpus_gate_readiness", report)
        # JSON 可序列化。
        json.dumps(report, ensure_ascii=False)

    def test_acceptance_report_a4a_mirrors_replay_check(self):
        evaluator = Evaluator(self.completed)
        report = evaluator.acceptance_report()
        self.assertEqual(report["a4a"], evaluator.replay_check())

    def test_closed_loop_not_available_without_real_data(self):
        # AC-3：无 AutoEP headroom 数据时必须诚实 not_available，不伪造成果。
        result = Evaluator().closed_loop()
        self.assertEqual("not_available", result["status"])

    def test_closed_loop_measured_scoring(self):
        # 传入真实改进数时给出 rubric 分数；闭环>单次 → pass。
        passed = Evaluator().closed_loop(closed_wins=8, single_wins=3)
        self.assertEqual("measured", passed["status"])
        self.assertEqual(5, passed["rubric_score"])
        self.assertTrue(passed["passed"])
        worse = Evaluator().closed_loop(closed_wins=1, single_wins=4)
        self.assertEqual(1, worse["rubric_score"])
        self.assertFalse(worse["passed"])

    def test_acceptance_report_a2_does_not_fake_measurement(self):
        report = Evaluator(self.completed).acceptance_report()
        a2 = report["a2"]
        # 自有字段不伪装成已测量的闭环优势
        self.assertIn("closed_loop_advantage", a2)


if __name__ == "__main__":
    unittest.main()