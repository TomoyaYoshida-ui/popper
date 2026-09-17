"""领域包注册表契约测试（批次 B 的出口条件）。

覆盖三件事：
1. 内置域包与两个遗留 evaluator 哈希被冻结，旧实验不会静默失效；
2. 未知 evaluator / 未声明指标必须立即失败，且错误指向「未支持」而非分类标签非法；
3. 新增一个指标只需「一个新文件 + 一行注册」——注册后立刻可从 core 入口使用。
"""
import unittest

from popper import core
from popper.core import ProtocolError, evaluator_metric, evaluator_pack, model_inputs, score
from popper.domains import protocol, register, registered_metrics
from popper.research.evaluation_service import _validate_points

FROZEN_HASHES = {
    "mse-v1": "70c8fcff8d8caec408cbe75c5be06115b4b010bf6931b5e6162aee8f8a55c164",
    "binary-accuracy-v1": "e2ee67d7efd70c0f60a5f56663a01653dc2ceed01c6c549737af58e9536e3252",
}
BUILTIN = sorted(["algorithm-throughput-v1", "binary-accuracy-v1", "binary-f1-v1",
                  "convergence-order-v1", "mae-v1", "mse-v1", "multiclass-macro-f1-v1",
                  "treatment-effect-v1"])

REGRESSION_ROWS = [{"id": "a", "x": 1, "y": 3}, {"id": "b", "x": 2, "y": 5}]
CLASSIFICATION_ROWS = [{"id": "a", "features": [0.0], "label": 1},
                       {"id": "b", "features": [1.0], "label": 0}]


class RegistryTests(unittest.TestCase):
    def test_builtin_evaluators_and_frozen_hashes(self):
        self.assertEqual(BUILTIN, sorted(core.EVALUATORS))
        for evaluator_id, expected in FROZEN_HASHES.items():
            self.assertEqual(expected, core.digest(core.EVALUATORS[evaluator_id]))

    def test_unknown_evaluator_fails_immediately(self):
        for entry in (evaluator_pack,
                      lambda i: core.sample_signature({"id": "a"}, i),
                      lambda i: core.model_inputs([{"id": "a", "x": 1, "y": 2}], i),
                      lambda i: score(REGRESSION_ROWS, [{"id": "a", "prediction": 1},
                                                        {"id": "b", "prediction": 1}], i)):
            with self.assertRaises(ProtocolError) as caught:
                entry("does-not-exist-v1")
            self.assertIn("未支持", str(caught.exception))
            self.assertNotIn("标签", str(caught.exception))

    def test_undeclared_metric_fails_immediately(self):
        with self.assertRaises(ProtocolError) as caught:
            evaluator_metric("mse-v1", "not_a_metric")
        self.assertIn("未支持的指标", str(caught.exception))
        with self.assertRaises(ProtocolError):
            evaluator_metric("mse-v1", "f1")

    def test_unknown_metric_contract_is_reported_as_unsupported(self):
        with self.assertRaises(ProtocolError) as caught:
            core.evaluator_for_metric({"name": "roc_auc", "direction": "max"})
        self.assertIn("未支持的指标契约", str(caught.exception))

    def test_registry_rejects_duplicate_id_and_unimplemented_shape(self):
        with self.assertRaises(ValueError) as caught:
            register(protocol.get("mse-v1"))
        self.assertIn("重复注册", str(caught.exception))

        class StagedPack(protocol.get("mse-v1").__class__):
            pack_id, evaluator_id = "staged-probe", "staged-probe-v1"
            task_shape = "streaming_artifacts"  # 仍未实现的形状

        with self.assertRaises(ValueError) as caught:
            register(StagedPack())
        self.assertIn("未实现的任务形状", str(caught.exception))
        self.assertNotIn("staged-probe-v1", core.EVALUATORS)


class MetricContractTests(unittest.TestCase):
    def test_value_domain_is_two_sided(self):
        f1 = registered_metrics()["f1"]
        self.assertTrue(f1.accepts(0.0))
        self.assertTrue(f1.accepts(1.0))
        self.assertFalse(f1.accepts(1.0001))
        self.assertFalse(f1.accepts(-0.0001))
        self.assertEqual("0 ≤ 值 ≤ 1", f1.range_text())
        mse = registered_metrics()["mse"]
        self.assertTrue(mse.accepts(1e9))
        self.assertEqual("≥ 0", mse.range_text())

    def test_point_validation_uses_declared_domain(self):
        spec = evaluator_metric("binary-f1-v1")
        _validate_points([{"seed": 1, "value": 0.9}], [1], spec, "Dev")
        with self.assertRaises(ProtocolError) as caught:
            _validate_points([{"seed": 1, "value": 1.5}], [1], spec, "Dev")
        self.assertIn("0 ≤ 值 ≤ 1", str(caught.exception))
        _validate_points([{"seed": 1, "value": 1e6}], [1], evaluator_metric("mae-v1"), "Dev")


class NewMetricPackTests(unittest.TestCase):
    def test_mae_regression(self):
        self.assertEqual(1.0, score(REGRESSION_ROWS, [{"id": "a", "prediction": 2},
                                                     {"id": "b", "prediction": 6}], "mae-v1"))
        self.assertEqual([{"id": "a", "x": 1}, {"id": "b", "x": 2}],
                         model_inputs(REGRESSION_ROWS, "mae-v1"))

    def test_binary_f1(self):
        predictions = [{"id": "a", "prediction": 1}, {"id": "b", "prediction": 0}]
        self.assertEqual(1.0, score(CLASSIFICATION_ROWS, predictions, "binary-f1-v1"))
        self.assertEqual(0.0, score(CLASSIFICATION_ROWS, [{"id": "a", "prediction": 0},
                                                          {"id": "b", "prediction": 0}],
                                    "binary-f1-v1"))
        no_positive = [{"id": "a", "features": [0.0], "label": 0}]
        with self.assertRaises(ProtocolError) as caught:
            score(no_positive, [{"id": "a", "prediction": 0}], "binary-f1-v1")
        self.assertIn("不含正类标签", str(caught.exception))

    def test_multiclass_macro_f1_and_label_domain(self):
        rows = [{"id": "a", "features": [0.0], "label": 0}, {"id": "b", "features": [1.0], "label": 1},
                {"id": "c", "features": [2.0], "label": 2}]
        predictions = [{"id": "a", "prediction": 0}, {"id": "b", "prediction": 1},
                       {"id": "c", "prediction": 2}]
        self.assertEqual(1.0, score(rows, predictions, "multiclass-macro-f1-v1"))
        with self.assertRaises(ProtocolError) as caught:
            score(rows, [{"id": "a", "prediction": 0}, {"id": "b", "prediction": 1},
                         {"id": "c", "prediction": 7}], "multiclass-macro-f1-v1")
        self.assertIn("不存在的类别", str(caught.exception))
        with self.assertRaises(ProtocolError) as caught:
            evaluator_pack("multiclass-macro-f1-v1").validate_splits([rows[:1]])
        self.assertIn("至少需要两个类别", str(caught.exception))

    def test_new_pack_is_reachable_by_entrypoints(self):
        """新增指标的改动面：一个新文件 + 一行注册，注册后无需改动判定层。"""
        class DoubledErrorV1(type(protocol.get("mse-v1"))):
            pack_id, evaluator_id = "tabular-regression-doubled", "doubled-error-v1"
            _metrics = (protocol.MetricSpec(name="doubled_error", direction="min",
                                            unit="absolute_error", value_domain=(0, None)),)
            _entry = {"id": "doubled-error-v1", "metric": {"name": "doubled_error", "direction": "min"},
                      "definition": "twice the id-aligned mean absolute error",
                      "dataset": "rows[id,x,y] where x and y are finite numbers"}

            def score_values(self, rows, values, metric_id):
                return 2 * sum(abs(values[row["id"]] - row["y"]) for row in rows) / len(rows)

        register(DoubledErrorV1())
        self.addCleanup(protocol._PACKS.pop, "doubled-error-v1", None)
        predictions = [{"id": "a", "prediction": 2}, {"id": "b", "prediction": 6}]
        self.assertEqual(2.0, score(REGRESSION_ROWS, predictions, "doubled-error-v1"))
        self.assertEqual("doubled-error-v1", core.evaluator_for_metric(
            {"name": "doubled_error", "direction": "min"}))
        self.assertEqual("doubled_error", evaluator_metric("doubled-error-v1").name)


if __name__ == "__main__":
    unittest.main()
