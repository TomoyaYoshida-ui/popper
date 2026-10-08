"""任务契约与指标登记表。不调用模型，不写迁移对照报告。"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from popper.core import ProtocolError, evaluator_pack, score
from popper.research.contract import compile_contract
from popper.research.intake import intake
from popper.research.metrics import (detect_unversioned_change, fixtures_pass, register_metric,
                                     registry)

ROWS = [{"id": "a", "x": 1, "y": 3}, {"id": "b", "x": 2, "y": 5}]
PREDICTIONS = [{"id": "a", "prediction": 1}, {"id": "b", "prediction": 4}]


def _contract(**overrides):
    body = {
        "target_column": "band_gap_eV",
        "task_shape": "aligned_prediction",
        "metric": "mse-v1",
        "repeat_unit": "train_seed",
        "split_constraints": {"group_by": "material_family", "no_overlap": True},
        "id_column": "material_id",
    }
    body.update(overrides)
    return body


class TaskContractTests(unittest.TestCase):
    def test_compiled_contract_matches_handwritten_mse(self):
        compiled = compile_contract(_contract())
        self.assertEqual(compiled.score(ROWS, PREDICTIONS), score(ROWS, PREDICTIONS, "mse-v1"))
        handwritten = evaluator_pack("mse-v1")
        splits = (ROWS, ROWS, ROWS)
        self.assertEqual(compiled.validate_splits(splits), handwritten.validate_splits(splits))
        bad = [{"id": "a", "x": 1}]
        with self.assertRaises(ProtocolError) as compiled_error:
            compiled.validate_rows(bad, "train")
        with self.assertRaises(ProtocolError) as handwritten_error:
            handwritten.validate_rows(bad, "train")
        self.assertEqual(str(compiled_error.exception), str(handwritten_error.exception))

    def test_unknown_metric_and_shape_are_rejected(self):
        with self.assertRaises(ProtocolError) as metric:
            compile_contract(_contract(metric="elo-v1"))
        self.assertEqual(str(metric.exception), "指标不在登记表内")
        with self.assertRaises(ProtocolError) as shape:
            compile_contract(_contract(task_shape="materials-science"))
        self.assertEqual(str(shape.exception), "任务形状不在已实现集合内")

    def test_registry_fixtures_and_hash_change(self):
        self.assertEqual(len(registry()), 12)
        self.assertTrue(fixtures_pass())
        recorded = registry()["mse-v1"]["evaluator_hash"]
        self.assertEqual(recorded, detect_unversioned_change("mse-v1", recorded))
        with self.assertRaises(ProtocolError) as changed:
            detect_unversioned_change("mse-v1", "0" * 64)
        self.assertEqual(str(changed.exception), "指标实现已变但 id 未换")

    def test_registered_custom_metric_is_not_scored(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "table.csv").write_text("a,b\n1,0\n2,1\n3,0\n4,1\n5,0\n", encoding="utf-8")
            intake(root, raw="table.csv", split_strategy="random", seed=1,
                   source_note="fixture", target_column="b",
                   train_ratio="0.600000", dev_ratio="0.200000", holdout_ratio="0.200000")
            (root / "metric.py").write_text("def score():\n    return 0\n", encoding="utf-8")
            (root / "fixture.json").write_text("{}\n", encoding="utf-8")
            registered = register_metric(
                root, "custom-error-v1", "metric.py", "fixture.json", approver="reviewer")
            self.assertEqual(registered["metric_id"], "custom-error-v1")
            with self.assertRaises(ProtocolError) as rejected:
                compile_contract(_contract(metric="custom-error-v1"))
            self.assertEqual(str(rejected.exception), "指标不在登记表内")
