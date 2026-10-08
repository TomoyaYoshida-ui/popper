"""锁住 M-A 验收报告。只读已提交的 JSON，不调用模型，也不读确认集。"""
import json
import unittest
from pathlib import Path

from popper.research.intake import (
    MAX_CELL_CHARS, MAX_COLUMNS, MAX_ROWS, MAX_SOURCE_BYTES,
)

REPO = Path(__file__).resolve().parents[1]
REPORT = REPO / "evaluation" / "runs" / "data-intake" / "heart-disease-cleveland" / "report.json"
PARQUET = REPO / "evaluation" / "runs" / "data-intake" / "parquet-roundtrip.json"
REPORT_KEYS = {
    "schema", "case", "milestone", "status", "model_called", "source", "limits",
    "split_declaration_sha256", "seal_sha256", "holdout_row_set_sha256",
    "data_card_sha256", "holdout_sealed_seq", "data_card_written_seq",
    "label_stats_scope", "recipe_sha256", "recipe_fit_sha256", "receipt_sha256",
    "input_identity_sha256", "compiled_sha256", "evaluator_id", "validate_rows",
    "validate_splits", "confirmation",
}
SOURCE_KEYS = {
    "name", "url", "version", "license", "retrieved_on", "raw_sha256",
    "byte_length", "n_rows", "n_columns", "format", "synthetic", "missing_columns",
}
CONFIRMATION_KEYS = {
    "confirmation_calls", "confirmation_consumed_once", "trust", "evaluator_id",
    "observation_seq", "claim_status",
}


class DataIntakeReportTests(unittest.TestCase):
    def test_public_table_report_closes_milestone(self):
        self.assertTrue(REPORT.is_file(), REPORT.as_posix())
        report = json.loads(REPORT.read_text(encoding="utf-8"))
        self.assertEqual(REPORT_KEYS, set(report))
        self.assertEqual("popper.data-intake-report/v1", report["schema"])
        self.assertEqual("heart-disease-cleveland", report["case"])
        self.assertEqual("M-A", report["milestone"])
        self.assertEqual("passed", report["status"])
        self.assertIs(False, report["model_called"])
        self.assertEqual("train_dev", report["label_stats_scope"])
        self.assertGreater(report["data_card_written_seq"], report["holdout_sealed_seq"])
        self.assertEqual({"train", "dev", "holdout"}, set(report["compiled_sha256"]))
        self.assertEqual({
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_rows": MAX_ROWS,
            "max_columns": MAX_COLUMNS,
            "max_cell_chars": MAX_CELL_CHARS,
        }, report["limits"])
        self.assertTrue(report["validate_rows"])
        self.assertTrue(report["validate_splits"])
        source = report["source"]
        self.assertEqual(SOURCE_KEYS, set(source))
        self.assertTrue(source["url"].startswith("https://"))
        self.assertIs(False, source["synthetic"])
        self.assertEqual("csv", source["format"])
        self.assertTrue(source["missing_columns"])
        for column in source["missing_columns"]:
            self.assertEqual({"name", "missing_rate"}, set(column))
            self.assertGreaterEqual(column["missing_rate"], 0.01)
            self.assertLessEqual(column["missing_rate"], 0.40)
        confirmation = report["confirmation"]
        self.assertEqual(CONFIRMATION_KEYS, set(confirmation))
        self.assertEqual(1, confirmation["confirmation_calls"])
        self.assertIs(True, confirmation["confirmation_consumed_once"])
        self.assertIn(confirmation["trust"], {
            "controller_scored_trusted_local", "controller_scored_os_sandbox"})
        self.assertEqual(report["evaluator_id"], confirmation["evaluator_id"])
        self.assertEqual("supports_threshold", confirmation["claim_status"])
        self.assertIsInstance(confirmation["observation_seq"], int)

    def test_parquet_roundtrip_receipt_is_passed(self):
        self.assertTrue(PARQUET.is_file(), PARQUET.as_posix())
        receipt = json.loads(PARQUET.read_text(encoding="utf-8"))
        self.assertEqual("passed", receipt["status"])
        self.assertIs(False, receipt["model_called"])
        self.assertEqual("parquet", receipt["format"])
        self.assertGreater(receipt["data_card_written_seq"], receipt["holdout_sealed_seq"])
