"""treatment-effect-v1 域包与「统计单元」回炉抽象的契约测试（PILOT3 / §8.2 第三试点）。

覆盖四件事：
1. 分析单元成为一等统计单位：repeat_unit=analysis_unit、unit_values 从数据导出、
   experiment.json 不写 seeds 而预注册 significance_ratio / min_units_for_significance；
2. gates 的 bootstrap 对 analysis_unit 开放统计主张（满足判据 statistical_claim=True，
   claim 结论把统计判据当必要条件），对 train_seed/independent_run 保持恒描述性；
3. 非随机切分：区块整块分配、跨划分不重叠、按 id 严格排序，违规立即失败；
4. 试点工程 examples/treatment-effect 跑通 init → search → freeze → confirm → replay，
   claim statistical_claim=True，replay 不执行候选代码，且 holdout 明确拒绝 staged 形状。
"""
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import Experiment, ProtocolError, initialize, read_json, validate_spec
from popper.domains import protocol
from popper.gates import (GateError, claim_outcome, claim_stats, l1_significance)
from popper.research.confirmation_runner import require_aligned_prediction

PILOT = Path(__file__).resolve().parents[1] / "examples" / "treatment-effect"
PILOT_FILES = ("experiment.json", "model.py", "train.json", "dev.json", "test.json")

PACK_ID = "treatment-effect-v1"


def _block_rows(blocks, prefix="s"):
    """构造最小可估计队列：每区块处理组/对照组各 3 行，真实效应恒为 2.0。"""
    rows = []
    for block in blocks:
        for treated in (0, 1):
            for i in range(3):
                rows.append({"id": f"{prefix}-b{block}-t{treated}-{i}", "block": block,
                             "x": 0.5 * treated, "treated": treated,
                             "outcome": 1.0 + 2.0 * treated, "tau": 2.0})
    return rows


class TreatmentEffectPackTests(unittest.TestCase):
    def setUp(self):
        self.pack = protocol.get(PACK_ID)

    def test_repeat_unit_and_data_derived_units(self):
        self.assertEqual("analysis_unit", self.pack.repeat_unit())
        self.assertTrue(self.pack.units_from_data)
        self.assertEqual((3, 4, 5), self.pack.unit_values(_block_rows([5, 3, 4, 3])))

    def test_invocation_uses_cohort_and_estimate_template(self):
        invocation = self.pack.invocation()
        self.assertEqual({"inputs": "cohort.json", "config": "estimator.json"},
                         invocation.input_basenames())
        self.assertEqual("estimate-7.json", invocation.prediction_name(7))

    def test_project_inputs_strips_true_effect(self):
        projected = self.pack.project_inputs(_block_rows([1]))
        self.assertTrue(projected)
        self.assertTrue(all(set(row) == {"id", "block", "x", "treated", "outcome"}
                            for row in projected))

    def test_block_score_uses_controller_held_truth(self):
        rows = _block_rows([1, 2])
        self.assertEqual(0.0, self.pack.score(rows, {"ate_estimate": 2.0}, None, unit=1))
        # 允许任意符号的估计值（标量字段不要求为正）。
        self.assertEqual(3.5, self.pack.score(rows, {"ate_estimate": -1.5}, None, unit=2))

    def test_score_requires_unit_and_existing_block(self):
        rows = _block_rows([1, 2])
        with self.assertRaises(ProtocolError):
            self.pack.score(rows, {"ate_estimate": 2.0}, None)
        with self.assertRaises(ProtocolError):
            self.pack.score(rows, {"ate_estimate": 2.0}, None, unit=9)

    def test_self_reported_metric_and_bad_fields_rejected(self):
        rows = _block_rows([1, 2])
        with self.assertRaises(ProtocolError) as caught:
            self.pack.score(rows, {"ate_estimate": 2.0, "ate_error": 0.0}, None, unit=1)
        self.assertIn("不得自行声明指标", str(caught.exception))
        with self.assertRaises(ProtocolError):
            self.pack.validate_rows([
                {"id": "x", "block": 1, "x": 0.0, "treated": 2, "outcome": 1.0, "tau": 2.0},
                {"id": "y", "block": 1, "x": 0.0, "treated": 0, "outcome": 1.0, "tau": 2.0},
                {"id": "z", "block": 2, "x": 0.0, "treated": 1, "outcome": 1.0, "tau": 2.0},
                {"id": "w", "block": 2, "x": 0.0, "treated": 0, "outcome": 1.0, "tau": 2.0},
            ], None)

    def test_validate_rows_rejects_single_arm_and_single_block(self):
        single_arm = _block_rows([1, 2])
        for row in single_arm:
            if row["block"] == 2:
                row["treated"] = 1
        with self.assertRaises(ProtocolError) as caught:
            self.pack.validate_rows(single_arm, None)
        self.assertIn("单臂区块", str(caught.exception))
        with self.assertRaises(ProtocolError):
            self.pack.validate_rows(_block_rows([1]), None)

    def test_validate_splits_enforces_nonrandom_block_assignment(self):
        pack = self.pack
        # 合规：区块整块分配且 id 按 train < dev < test 严格排序。
        self.assertIsNone(pack.validate_splits(
            [_block_rows([1, 2], "a"), _block_rows([3, 4], "b"), _block_rows([5, 6], "c")]))
        # 跨划分混用区块（随机切行会造成的形状）必须失败。
        with self.assertRaises(ProtocolError) as caught:
            pack.validate_splits(
                [_block_rows([1, 2], "a"), _block_rows([2, 3], "b"), _block_rows([3, 4], "c")])
        self.assertIn("整块分配", str(caught.exception))
        # 区块 id 乱序（未按冻结的确定性顺序分配）必须失败。
        with self.assertRaises(ProtocolError) as caught:
            pack.validate_splits(
                [_block_rows([1, 4], "a"), _block_rows([2, 3], "b"), _block_rows([5, 6], "c")])
        self.assertIn("确定性顺序", str(caught.exception))
        # 单区块划分不构成单元层抽样。
        with self.assertRaises(ProtocolError):
            pack.validate_splits(
                [_block_rows([1], "a"), _block_rows([3, 4], "b"), _block_rows([5, 6], "c")])


class SpecValidationTests(unittest.TestCase):
    def _spec(self):
        return json.loads((PILOT / "experiment.json").read_text(encoding="utf-8"))

    def test_pilot_spec_is_valid_without_seeds(self):
        validate_spec(self._spec())

    def test_seeds_forbidden_and_ratio_preregistered(self):
        spec = self._spec()
        spec["seeds"] = [1, 2]
        with self.assertRaises(ProtocolError) as caught:
            validate_spec(spec)
        self.assertIn("seeds", str(caught.exception))
        spec = self._spec()
        del spec["significance_ratio"]
        with self.assertRaises(ProtocolError) as caught:
            validate_spec(spec)
        self.assertIn("significance_ratio", str(caught.exception))
        spec = self._spec()
        spec["significance_ratio"] = 0.5  # 必须严格高于 0.5
        with self.assertRaises(ProtocolError):
            validate_spec(spec)


class AnalysisUnitStatisticsTests(unittest.TestCase):
    def test_consistent_units_support_statistical_claim(self):
        baseline = {unit: 1.0 + 0.05 * unit for unit in range(17, 25)}
        candidate = {unit: 0.2 for unit in range(17, 25)}
        result = l1_significance(
            {"baseline_per_seed": baseline, "candidate_per_seed": candidate,
             "seeds": list(range(17, 25)), "direction": "min",
             "repeat_unit": "analysis_unit", "significance_ratio": 0.95},
            None, random.Random(0))
        self.assertTrue(result["statistical_claim"])
        self.assertFalse(result["descriptive"])
        self.assertTrue(result["passed"])
        self.assertEqual(8, result["n_units"])
        self.assertEqual(1.0, result["p_direction_consistent"])
        self.assertNotIn("n_seeds", result)

    def test_mixed_units_fail_preregistered_ratio(self):
        baseline = {unit: 1.0 for unit in range(8)}
        candidate = {unit: (0.2 if unit < 2 else 1.5) for unit in range(8)}
        result = l1_significance(
            {"baseline_per_seed": baseline, "candidate_per_seed": candidate,
             "seeds": list(range(8)), "direction": "min",
             "repeat_unit": "analysis_unit", "significance_ratio": 0.95},
            None, random.Random(0))
        self.assertFalse(result["statistical_claim"])
        self.assertTrue(result["descriptive"])
        self.assertFalse(result["passed"])

    def test_single_unit_is_descriptive_and_blocks_claim(self):
        result = l1_significance(
            {"baseline_per_seed": {1: 1.0}, "candidate_per_seed": {1: 0.2},
             "seeds": [1], "direction": "min", "repeat_unit": "analysis_unit"},
            None, random.Random(0))
        self.assertFalse(result["statistical_claim"])
        self.assertFalse(result["passed"])
        self.assertEqual(1, result["n_units"])

    def test_unknown_repeat_unit_rejected(self):
        context = {"baseline_per_seed": {1: 1.0}, "candidate_per_seed": {1: 0.9},
                   "seeds": [1], "repeat_unit": "village"}
        with self.assertRaises(GateError):
            l1_significance(context, None)

    def test_claim_outcome_requires_statistics_for_analysis_units(self):
        good = {"statistical_claim": True}
        weak = {"statistical_claim": False}
        self.assertEqual("supports_threshold",
                         claim_outcome(0.8, 0.5, good, "analysis_unit"))
        # 效应量过阈但单元层统计不达标 → 不允许支持结论。
        self.assertEqual("insufficient_evidence",
                         claim_outcome(0.8, 0.5, weak, "analysis_unit"))
        # 训练种子域包的统计永远描述性，结论只取决于效应量阈值。
        self.assertEqual("supports_threshold",
                         claim_outcome(0.8, 0.5, {"statistical_claim": False}, "train_seed"))
        self.assertEqual("insufficient_evidence",
                         claim_outcome(0.4, 0.5, None, "independent_run"))

    def test_claim_stats_keys_follow_unit_kind(self):
        unit_stats = l1_significance(
            {"baseline_per_seed": {u: 1.0 for u in range(8)},
             "candidate_per_seed": {u: 0.2 for u in range(8)},
             "seeds": list(range(8)), "direction": "min",
             "repeat_unit": "analysis_unit", "significance_ratio": 0.95},
            None, random.Random(0))
        projected = claim_stats(unit_stats, "analysis_unit")
        self.assertEqual({"p_direction_consistent", "bootstrap_n", "n_units", "descriptive",
                          "statistical_claim", "repeat_unit"}, set(projected))
        seed_stats = l1_significance(
            {"baseline_per_seed": {1: 1.0}, "candidate_per_seed": {1: 0.5},
             "seeds": [1], "direction": "min"}, None, random.Random(0))
        projected = claim_stats(seed_stats, "train_seed")
        self.assertEqual({"p_direction_consistent", "bootstrap_n", "n_seeds", "descriptive",
                          "statistical_claim", "repeat_unit"}, set(projected))
        self.assertFalse(projected["statistical_claim"])


class PilotTreatmentEffectEndToEndTests(unittest.TestCase):
    """试点领域 treatment-effect-v1：五阶段跑通，统计主张为真，replay 不执行代码。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in PILOT_FILES:
            shutil.copyfile(PILOT / name, self.root / name)
        self.exp = None

    def tearDown(self):
        if self.exp:
            self.exp.close()
        self.temp.cleanup()

    def start(self):
        initialize(self.root)
        self.exp = Experiment(self.root)
        return self.exp

    def test_five_phases_with_analysis_unit_statistics_and_offline_replay(self):
        exp = self.start()
        self.assertEqual(PACK_ID, exp.state()["evaluator_id"])
        self.assertNotIn("seeds", exp.state()["spec"])
        self.assertEqual(2, len(exp.search(True)))
        exp.freeze()
        claim = exp.confirm(True)
        self.assertEqual("supports_threshold", claim["status"])
        self.assertIs(claim["stats"]["statistical_claim"], True)
        self.assertFalse(claim["stats"]["descriptive"])
        self.assertEqual("analysis_unit", claim["stats"]["repeat_unit"])
        self.assertEqual(8, claim["stats"]["n_units"])
        self.assertEqual("absolute_ate_error_reduction", claim["unit"])
        self.assertGreaterEqual(claim["delta"], 0.5)
        test_runs = {result["config"]["estimator"]: result for result in exp.results("test")}
        # 重复取值是 test 划分导出的区块 17..24，存档字段仍叫 per_seed/n_seeds。
        self.assertEqual([17, 18, 19, 20, 21, 22, 23, 24],
                         [point["seed"] for point in test_runs["dif"]["per_seed"]])
        self.assertEqual(8, test_runs["dif"]["n_seeds"])
        self.assertLess(test_runs["adj"]["mean"], 0.5)
        self.assertGreater(test_runs["dif"]["mean"], 0.7)
        for result in exp.results():
            run_dir = exp.home / "runs" / result["run_id"]
            units = [point["seed"] for point in result["per_seed"]]
            self.assertTrue((run_dir / "cohort.json").is_file())
            self.assertTrue((run_dir / "estimator.json").is_file())
            for unit in units:
                self.assertTrue((run_dir / f"estimate-{unit}.json").is_file())
            self.assertFalse((run_dir / "inputs.json").exists())
            self.assertFalse((run_dir / "train.json").exists())
            self.assertFalse((run_dir / f"predictions-{units[0]}.json").exists())
            # 标签剥离：候选输入制品里不允许出现真实效应 tau。
            cohort = read_json(run_dir / "cohort.json")
            self.assertTrue(cohort)
            self.assertTrue(all("tau" not in row for row in cohort))
        with patch("popper.sandbox.launch",
                   side_effect=AssertionError("replay must not execute candidate code")):
            replay = exp.replay()
        self.assertEqual(len(exp.results()), replay["runs_recomputed"])
        self.assertTrue(replay["claim_recomputed"])
        self.assertTrue(Path(exp.report()).is_file())

    def test_treatment_pilot_cannot_enter_holdout(self):
        with self.assertRaises(ProtocolError) as caught:
            require_aligned_prediction(PACK_ID)
        self.assertIn("staged_artifacts", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

