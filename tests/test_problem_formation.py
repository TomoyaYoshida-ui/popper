"""问题形成夹具。不调用模型，不下载数据，不写验收报告。"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from popper.core import ProtocolError, read_json, validate_spec
from popper.research.formulate import approve_problem, propose_problem
from popper.research.intake import IntakeLog, intake

RATIOS = {"train_ratio": "0.600000", "dev_ratio": "0.200000", "holdout_ratio": "0.200000"}


def _project(root):
    (root / "table.csv").write_text(
        "\ufeffage,income,label\n1,1.0,0\n2,,1\n3,3.0,0\n4,4.0,1\n", encoding="utf-8")
    intake(root, raw="table.csv", split_strategy="random", seed=3,
           source_note="fixture", target_column="label", **RATIOS)
    return read_json(root / ".popper-intake" / "holdout-seal.json")


def _proposal(seal, **overrides):
    body = {
        "question": "年龄与收入能否预测标签",
        "hypothesis": {
            "explanations": [
                {"id": "linear-signal", "statement": "年龄和收入的数值组合预测标签",
                 "prediction": "线性候选的开发集误差小于常数基线 0.1"},
                {"id": "missingness", "statement": "标签只由收入是否缺失决定",
                 "prediction": "线性候选的开发集误差不小于常数基线"},
            ],
        },
        "target_column": "label",
        "task_shape": "aligned_prediction",
        "metric": "mse-v1",
        "min_improvement": {"value": 0.1, "unit": "squared_error", "reason": "夹具阈值"},
        "repeat_unit": "train_seed",
        "stop_rule": {"kind": "reach_baseline"},
        "split_declaration_sha256": seal["split_declaration_sha256"],
    }
    body.update(overrides)
    return body


class ProblemFormationTests(unittest.TestCase):
    def test_fixture_reply_becomes_a_valid_experiment(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            seal = _project(root)
            proposed = propose_problem(root, _proposal(seal))
            approved = approve_problem(root, proposed["proposal_id"])
            spec = read_json(root / "experiment.json")
            validate_spec(spec)
            self.assertEqual(spec["metric"], {"name": "mse", "direction": "min"})
            self.assertEqual(spec["min_improvement"], 0.1)
            log = IntakeLog(root / ".popper-intake" / "intake.sqlite")
            try:
                sealed = log.event("holdout_sealed")
                problem = log.event("problem_approved")
                self.assertGreater(problem["seq"], sealed["seq"])
                self.assertEqual(problem["payload"]["experiment_sha256"],
                                 approved["experiment_sha256"])
            finally:
                log.close()

    def test_unknown_metric_and_target_are_rejected_before_approval(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            seal = _project(root)
            with self.assertRaises(ProtocolError) as metric:
                propose_problem(root, _proposal(seal, metric="elo-v1"))
            self.assertEqual(str(metric.exception), "指标不在登记表内")
            with self.assertRaises(ProtocolError) as target:
                propose_problem(root, _proposal(seal, target_column="not-a-column"))
            self.assertEqual(str(target.exception), "目标列不在数据卡内")

    def test_approved_problem_rejects_later_changes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            seal = _project(root)
            proposed = propose_problem(root, _proposal(seal))
            approve_problem(root, proposed["proposal_id"])
            with self.assertRaises(ProtocolError) as changed:
                propose_problem(root, _proposal(seal, metric="mae-v1"))
            self.assertEqual(str(changed.exception),
                             "批准后不能改指标、阈值或确认集，请使用 research followup")
            with self.assertRaises(ProtocolError) as again:
                approve_problem(root, proposed["proposal_id"])
            self.assertIn("research followup", str(again.exception))

    def test_identical_predictions_are_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            seal = _project(root)
            proposal = _proposal(seal)
            proposal["hypothesis"]["explanations"][1]["prediction"] = (
                "  线性候选的开发集误差小于常数基线   0.1 ")
            with self.assertRaises(ProtocolError) as same:
                propose_problem(root, proposal)
            self.assertEqual(str(same.exception), "问题书必须有两个预测不同的解释")

    def test_approved_problem_rejects_a_lower_threshold(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            seal = _project(root)
            proposed = propose_problem(root, _proposal(seal))
            approve_problem(root, proposed["proposal_id"])
            lowered = _proposal(seal)
            lowered["min_improvement"] = {
                "value": 0.01, "unit": "squared_error", "reason": "把栏杆放低",
            }
            with self.assertRaises(ProtocolError) as changed:
                propose_problem(root, lowered)
            self.assertEqual(str(changed.exception),
                             "批准后不能改指标、阈值或确认集，请使用 research followup")
