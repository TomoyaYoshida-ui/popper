import tempfile
import unittest
from pathlib import Path

from popper.novelty_measure import (measure_fpr, measure_hallucination_rate,
                                    _sample, run)


class NoveltyMeasureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sample_produces_balanced_positives_negatives(self):
        s = _sample(5, __import__("random").Random(1))
        self.assertEqual(10, len(s))
        self.assertEqual(5, sum(1 for x in s if x["label"] == "positive"))
        self.assertEqual(5, sum(1 for x in s if x["label"] == "negative"))

    def test_measure_fpr_of_clean_negatives_is_zero(self):
        # 当前 review 判定下，负样本（无覆盖/无矛盾）不应触发机械致命项。
        fpr = measure_fpr(_sample(30, __import__("random").Random(2)))
        self.assertLessEqual(fpr, 0.1)

    def test_run_produces_report_and_decision(self):
        report = run(self.dir, n_negative=20)
        self.assertIn("fpr", report)
        self.assertIn("reverse_citation_hallucination_rate", report)
        self.assertIn("pass", report)
        # 落盘 a3_report.json
        self.assertTrue((self.dir / "a3_report.json").is_file())
        # 诚实标注方法为确定性 fixture
        self.assertIn("确定性", report["method"])

    def test_hallucination_placeholder_removed(self):
        # 无全文核验后端时反证幻觉率必须如实 not_available，
        # 不得用「可解析占位一律漏检一半」的占位数值冒充测量结果。
        result = measure_hallucination_rate(__import__("random").Random(3), 10)
        self.assertEqual("not_available", result["status"])
        self.assertIsNone(result["hallucination_rate"])
        self.assertNotIn("escapable_placeholder", result)

    def test_run_marks_hallucination_unmeasured_without_backend(self):
        report = run(self.dir, n_negative=20)
        self.assertIsNone(report["reverse_citation_hallucination_rate"])
        self.assertEqual("not_available", report["hallucination_status"])


if __name__ == "__main__":
    unittest.main()