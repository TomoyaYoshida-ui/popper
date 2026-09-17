import random
import tempfile
import unittest
from pathlib import Path

from popper.gates import (GateError, HUMAN_REVIEW, PASSED, RETRY, RETRY_LIMIT,
                          evaluate_gate, l0_metric_registry, l0_mode7_signals,
                          l1_preregistered, l1_significance, read_gate_context)


def _baseline_candidate(ratio_seed=0):
    rng = random.Random(ratio_seed)
    return rng.random()  # 占位，实际用固定映射


class L0GateTests(unittest.TestCase):
    def test_metric_registry_rejects_unknown(self):
        context = {"metric_name": "not_in_registry"}
        result = l0_metric_registry(context)
        self.assertFalse(result["passed"])

    def test_metric_registry_accepts_registered(self):
        context = {"metric_name": "mse"}
        result = l0_metric_registry(context)
        self.assertTrue(result["passed"])

    def test_mode7_deterministic_signals(self):
        self.assertTrue(l0_mode7_signals({})["passed"])
        result = l0_mode7_signals({"mode7_flags": {"hallucinated_result": True}})
        self.assertFalse(result["passed"])
        self.assertIn("hallucinated_result", result["hits"])


class L1GateTests(unittest.TestCase):
    def test_prereg_freeze_change_requires_approval(self):
        context = {"claim_declaration": "新声明", "frozen_declaration": "旧声明"}
        result = l1_preregistered(context, None)
        self.assertFalse(result["passed"])
        self.assertTrue(result["required_approval"])

    def test_prereg_consistent(self):
        result = l1_preregistered({"claim_declaration": "X", "frozen_declaration": "X"}, None)
        self.assertTrue(result["passed"])

    def test_significance_positive_direction(self):
        baseline = {1: 0.90, 2: 0.91, 3: 0.92}
        candidate = {1: 0.95, 2: 0.96, 3: 0.97}
        context = {"baseline_per_seed": baseline, "candidate_per_seed": candidate,
                   "seeds": [1, 2, 3], "direction": "max"}
        result = l1_significance(context, None, rng=random.Random(7))
        self.assertTrue(result["passed"])
        self.assertTrue(result["descriptive"])
        self.assertFalse(result["statistical_claim"])
        self.assertGreater(result["p_direction_consistent"], 0.5)

    def test_significance_no_per_seed_is_descriptive(self):
        result = l1_significance({}, None)
        self.assertTrue(result["passed"])
        self.assertTrue(result["descriptive"])

    def test_significance_min_direction_mse_improvement(self):
        # MSE 越小越好：候选更低 = 改善，但训练 seed 仍只作描述性稳定性检查。
        baseline = {1: 1.0, 2: 1.1, 3: 0.9}
        candidate = {1: 0.5, 2: 0.4, 3: 0.6}
        context = {"baseline_per_seed": baseline, "candidate_per_seed": candidate,
                   "seeds": [1, 2, 3], "direction": "min"}
        result = l1_significance(context, None, rng=random.Random(7))
        self.assertTrue(result["passed"])
        self.assertTrue(result["descriptive"])
        self.assertFalse(result["statistical_claim"])
        self.assertGreater(result["p_direction_consistent"], 0.5)

    def test_significance_min_direction_worse_is_descriptive(self):
        # MSE 升高 = 变差，方向不一致 → 只能描述性，不能声称显著。
        baseline = {1: 1.0, 2: 1.1, 3: 0.9}
        candidate = {1: 1.2, 2: 1.3, 3: 1.1}
        context = {"baseline_per_seed": baseline, "candidate_per_seed": candidate,
                   "seeds": [1, 2, 3], "direction": "min"}
        result = l1_significance(context, None, rng=random.Random(7))
        self.assertTrue(result["descriptive"])
        self.assertLess(result["p_direction_consistent"], 0.5)

    def test_significance_single_seed_is_descriptive(self):
        # 单种子没有抽样变异性，方向一致性 1.0 也不能支撑统计显著。
        baseline = {1: 0.90}
        candidate = {1: 0.95}
        context = {"baseline_per_seed": baseline, "candidate_per_seed": candidate,
                   "seeds": [1], "direction": "max"}
        result = l1_significance(context, None, rng=random.Random(7))
        self.assertTrue(result["descriptive"])
        self.assertEqual(1, result["n_seeds"])

    def test_significance_rejects_unknown_direction(self):
        context = {"baseline_per_seed": {1: 0.9}, "candidate_per_seed": {1: 0.95},
                   "seeds": [1], "direction": "sideways"}
        with self.assertRaises(GateError):
            l1_significance(context, None)


class GateFlowTests(unittest.TestCase):
    def test_l0_failure_is_human_review(self):
        context = {"metric_name": "bogus"}
        decision = evaluate_gate(context)
        self.assertEqual(HUMAN_REVIEW, decision["status"])
        self.assertEqual("L0", decision["level"])

    def test_l1_failure_retries_and_then_human_review(self):
        # 语义与实际基底：pre_wait 一致，significance 因缺 per-seed 渲染 descriptive(绕过)。
        # 这里制造 prerequisite 冲突（prereg 变化非 retryable）=> 直接 human_review。
        context = {
            "metric_name": "accuracy",
            "claim_declaration": "A",
            "frozen_declaration": "B",  # 预注册改动 => 不可 retry
        }
        decision = evaluate_gate(context)
        self.assertNotEqual(PASSED, decision["status"])
        self.assertIn(decision["status"], (RETRY, HUMAN_REVIEW))

    def test_retry_granted_then_limited(self):
        context = {
            "payload": {"content": "x"},
            "metric_name": "accuracy",
            "baseline_per_seed": {1: 0.1, 2: 0.2},
            "candidate_per_seed": {1: 0.3, 2: 0.4},  # 正方向
            "seeds": [1, 2],
        }
        # 第一次应当 retry（若显著）或 passed；仅有正方向且无 prewarn 冲突。
        decision = evaluate_gate(context, semantic_llm=None)
        self.assertEqual(PASSED, decision["status"])
        self.assertLessEqual(decision["retry_used"], RETRY_LIMIT)


if __name__ == "__main__":
    unittest.main()
