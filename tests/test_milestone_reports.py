"""锁住格式适配器、M-B、M-C、M-D 的验收报告。只读 JSON，不调用模型。"""
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "evaluation" / "runs"
if not (RUNS / 'data-intake/heart-disease-cleveland/report.json').is_file():
    RUNS = REPO / 'tests/fixtures/maintenance-reports'
INTAKE = json.loads((RUNS / "data-intake" / "heart-disease-cleveland" / "report.json").read_text(encoding="utf-8"))
FORMAT_KEYS = {
    "schema", "case", "milestone", "status", "model_called", "adapter_id", "record_shape",
    "approved_once", "profile_sha256", "seal_sha256", "unregistered_rejected",
    "converted_to_csv", "discipline_allowlist", "metric_from_registry",
}
PROBLEM_KEYS = {
    "schema", "case", "milestone", "status", "model_called", "proposer",
    "source_raw_sha256", "problem",
    "approval", "protocol_sha256", "input_identity_sha256", "holdout_row_set_sha256",
    "confirmation",
}
CONTRACT_KEYS = {
    "schema", "case", "milestone", "status", "model_called", "evaluator_id",
    "source_evaluator_id", "handwritten_score", "contract_score", "scores_equal",
}
LOOP_KEYS = {
    "schema", "case", "milestone", "status", "model_called", "source_raw_sha256",
    "holdout_row_set_sha256", "budget_cap", "contract_evaluator_id", "problem_sha256",
    "candidates", "dev", "confirmation", "ablation_claim_written",
}
TABLE_METRICS = (
    "mse-v1", "mae-v1", "binary-accuracy-v1", "binary-f1-v1", "multiclass-macro-f1-v1",
)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class MilestoneReportTests(unittest.TestCase):
    def test_format_adapter_report(self):
        report = _load(RUNS / "format-adapter" / "fixture-tab" / "report.json")
        self.assertEqual(FORMAT_KEYS, set(report))
        self.assertEqual("popper.format-adapter-report/v1", report["schema"])
        self.assertEqual("format-adapter", report["milestone"])
        self.assertEqual("passed", report["status"])
        self.assertIs(False, report["model_called"])
        self.assertIs(True, report["approved_once"])
        self.assertIs(True, report["unregistered_rejected"])
        self.assertIs(False, report["converted_to_csv"])
        self.assertIs(False, report["discipline_allowlist"])
        self.assertIs(True, report["metric_from_registry"])
        self.assertEqual("table", report["record_shape"])
        self.assertEqual(64, len(report["profile_sha256"]))
        self.assertEqual(64, len(report["seal_sha256"]))

    def test_problem_formation_uses_the_same_public_table(self):
        report = _load(RUNS / "problem-formation" / "heart-disease-cleveland" / "report.json")
        self.assertEqual(PROBLEM_KEYS, set(report))
        self.assertEqual("M-B", report["milestone"])
        self.assertEqual("passed", report["status"])
        self.assertIs(False, report["model_called"])
        self.assertEqual("fixture", report["proposer"])
        self.assertEqual(INTAKE["source"]["raw_sha256"], report["source_raw_sha256"])
        self.assertGreater(report["approval"]["problem_approved_seq"],
                           report["approval"]["holdout_sealed_seq"])
        self.assertEqual(report["protocol_sha256"], report["approval"]["experiment_sha256"])
        self.assertEqual(1, report["confirmation"]["confirmation_calls"])
        self.assertIs(True, report["confirmation"]["confirmation_consumed_once"])
        self.assertEqual("supports_threshold", report["confirmation"]["claim_status"])
        self.assertEqual("multiclass-macro-f1-v1", report["problem"]["metric"])

    def test_five_contract_migrations_match(self):
        for metric_id in TABLE_METRICS:
            report = _load(RUNS / "task-contract" / metric_id / "report.json")
            self.assertEqual(CONTRACT_KEYS, set(report))
            self.assertEqual("M-C", report["milestone"])
            self.assertEqual(metric_id, report["case"])
            self.assertIs(False, report["model_called"])
            self.assertIs(True, report["scores_equal"])
            self.assertEqual(report["handwritten_score"], report["contract_score"])
            self.assertEqual(metric_id, report["source_evaluator_id"])

    def test_contract_loop_has_three_actions_and_one_confirmation(self):
        report = _load(RUNS / "contract-loop" / "heart-disease-cleveland" / "report.json")
        self.assertEqual(LOOP_KEYS, set(report))
        self.assertEqual("M-D", report["milestone"])
        self.assertIs(False, report["model_called"])
        self.assertEqual(INTAKE["source"]["raw_sha256"], report["source_raw_sha256"])
        self.assertEqual(3.0, report["budget_cap"])
        actions = [item["action"] for item in report["candidates"]]
        self.assertEqual(["feature_engineering", "model_family", "ablation"], actions)
        recipes = {item["action"]: item["recipe_sha256"] for item in report["candidates"]}
        self.assertNotEqual(recipes["feature_engineering"], recipes["ablation"])
        self.assertEqual("tree", report["candidates"][1]["template"])
        self.assertIs(False, report["ablation_claim_written"])
        self.assertEqual(1, report["confirmation"]["confirmation_calls"])
        self.assertEqual({"feature_engineering", "model_family", "ablation", "code_only"},
                         set(report["dev"]))
        self.assertIn(report["confirmation"]["claim_status"],
                      {"supports_threshold", "insufficient_evidence"})
