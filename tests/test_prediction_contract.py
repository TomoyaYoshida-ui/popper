import unittest

from popper.core import ProtocolError
from popper.research.prediction import (build_prediction, evaluate_prediction,
    render_prediction, research_contract, validate_prediction, validate_prediction_text)
from popper.review import MODE7, RejectionReview


class PredictionContractTests(unittest.TestCase):
    def test_accuracy_prediction_cannot_validate_macro_f1(self):
        with self.assertRaisesRegex(ProtocolError, "准确率"):
            validate_prediction_text("准确率预计超过 0.577", "multiclass-macro-f1-v1")
        value = build_prediction("multiclass-macro-f1-v1", .05)
        self.assertIn("macro_f1", render_prediction(value))
        for field, replacement in (("metric", "accuracy"), ("unit", "macro_f1"),
                                   ("direction", "min"), ("threshold", float("nan"))):
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                validate_prediction({**value, field: replacement})

    def test_execution_threshold_and_scientific_evidence_are_distinct(self):
        prediction = build_prediction("mse-v1", .1)
        result = evaluate_prediction(prediction, .2,
            statistical_evidence={"statistical_claim": True}, repeat_unit="train_seed")
        self.assertEqual("met", result["status"])
        self.assertFalse(result["statistical_evidence_sufficient"])
        self.assertEqual("unverified", result["mechanism_status"])
        self.assertEqual("inconclusive", evaluate_prediction(prediction, .2,
            evidence_sufficient=False)["status"])
        self.assertEqual("not_evaluated", evaluate_prediction(prediction,
            execution_succeeded=False)["status"])

    def test_authorization_hash_ignores_growing_state_but_not_contract(self):
        value = {"study_id": "S-a", "metric": {"name": "mse"}, "budget_cap": 4}
        self.assertEqual(research_contract(value), research_contract({**value, "state_version": 8}))
        self.assertNotEqual(research_contract(value), research_contract({**value, "budget_cap": 5}))

    def test_empty_fraud_report_is_incomplete_not_clean(self):
        result = RejectionReview().assess({})
        self.assertEqual("warn", result["rejection_side"]["risks"]["R4"]["status"])
        self.assertEqual("incomplete", result["fraud_side"]["summary"]["status"])
        self.assertTrue(all(row["status"] == "unknown" for row in result["fraud_side"]["modes"]))
        checked = {key: {"completed": True, "status": "clean", "evidence": "checked artifact"}
                   for key in MODE7}
        self.assertEqual("pass", RejectionReview(verified_checks=checked).assess({})
                         ["fraud_side"]["summary"]["status"])
        self.assertEqual("incomplete", RejectionReview().assess({"mode7_checks": checked})
                         ["fraud_side"]["summary"]["status"])
        checked[MODE7[0]]["completed"] = False
        self.assertEqual("incomplete", RejectionReview(verified_checks=checked).assess({})
                         ["fraud_side"]["summary"]["status"])
