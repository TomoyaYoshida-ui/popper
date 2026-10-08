"""Machine evidence and implementation invariants; no technical documents are read."""
import json
import unittest
from pathlib import Path
from popper.core import EVALUATORS, validate_spec
from popper.research.metrics import registry, fixtures_pass
REPO = Path(__file__).resolve().parents[1]
BLIND_REPORT = REPO / "evaluation/runs/bp-20260913T083501Z-c32a/blind-repair-report.json"
TREE_REPORT = REPO / "evaluation/runs/mlebench-lite/tree-report.json"

class ImplementationEvidenceTests(unittest.TestCase):
    def test_metric_registry_is_complete_and_scoring_fixtures_pass(self):
        self.assertEqual(set(EVALUATORS), set(registry()))
        self.assertTrue(fixtures_pass())

    def test_bundled_examples_have_valid_executable_protocols(self):
        for name in ("quadratic", "breast-cancer-wisconsin", "pima-zero-imputation", "ode-convergence", "throughput-sort", "treatment-effect"):
            with self.subTest(example=name):
                value = json.loads((REPO / "examples" / name / "experiment.json").read_text(encoding="utf-8"))
                validate_spec(value)

    def test_blind_report_is_internally_consistent(self):
        """产物自身也要自洽：三臂与四种条件各自求和都得等于结论命中数与轨迹总数。"""
        if not BLIND_REPORT.is_file():
            self.skipTest(f"{BLIND_REPORT.name} 不在本机")
        report = json.loads(BLIND_REPORT.read_text(encoding="utf-8"))
        arms, conditions = report["by_comparator"], report["by_condition"]
        self.assertEqual(sum(a["conclusion_matched"] for a in arms.values()),
                         report["conclusion_matched"])
        self.assertEqual(sum(c["conclusion_matched"] for c in conditions.values()),
                         report["conclusion_matched"])
        self.assertEqual(sum(a["planned"] for a in arms.values()), report["planned_trajectories"])
        self.assertEqual(sum(c["planned"] for c in conditions.values()),
                         report["planned_trajectories"])
        self.assertLessEqual(report["conclusion_matched"], report["valid_loops"])
        self.assertLessEqual(report["valid_loops"], report["planned_trajectories"])
        self.assertFalse(report["composite"]["is_single_frozen_run"],
                         "该报告不再 post_fix_composite，技术方案里的描述需重写")
        self.assertFalse(report["claims"]["adaptive_gain_claimed"],
                         "产物不再声明自适应增益为否，文档的保守口径需同步上调")
        self.assertFalse(report["claims"]["scientific_accuracy_claimed"])

    def test_search_report_records_real_budget_and_confirmation_counts(self):
        self.assertTrue(TREE_REPORT.is_file())
        report = json.loads(TREE_REPORT.read_text(encoding="utf-8"))
        self.assertEqual(sum(task["confirmation_calls"] for task in report["tasks"]), report["confirmation_calls"])
        self.assertLessEqual(report["tokens_used_total"], report["budget_confirmed_tokens"])
