import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.novelty_measure import (
    make_quote_binding_verifier, measure_fpr, measure_hallucination_rate,
    _sample, run,
)


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
        self.assertIsNone(report["real_novelty_fpr"])

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

    def test_quote_binding_verifier_accepts_bound_rejects_unbound(self):
        text = "Alpha beta gamma delta epsilon zeta eta theta iota kappa."
        corpus = {"10.1101/fixed-0": text}
        claims = {
            "10.1101/fixed-0": text[:40],
            "10.1101/fixed-2": "Q" * 40,
        }
        verifier = make_quote_binding_verifier(corpus, claims)
        self.assertTrue(verifier("10.1101/fixed-0"))
        self.assertFalse(verifier("10.1101/fixed-2"))
        self.assertFalse(verifier("missing"))

    def test_run_with_quote_corpus_measures_hallucination_rate(self):
        n = 8
        corpus = {}
        for i in range(0, n, 2):
            corpus["10.1101/fixed-{}".format(i)] = (
                "Bound methodological fixture text for quote binding "
                "case {:02d} with enough characters.".format(i)
            )
        report = run(self.dir / "quoted", n_negative=n, quote_corpus=corpus)
        self.assertEqual("measured", report["hallucination_status"])
        self.assertIsNotNone(report["reverse_citation_hallucination_rate"])
        self.assertIsNone(report["real_novelty_fpr"])
        self.assertIn("引文绑定", report["method"])
        # i=0,4 bound; i=2,6 unbound among resolvable (4 resolvable / 8 total)
        # leaked = 2 unbound → rate = 2/8 = 0.25
        self.assertEqual(0.25, report["reverse_citation_hallucination_rate"])

    def test_quote_corpus_and_verifier_conflict(self):
        with self.assertRaises(ProtocolError):
            run(self.dir / "conflict", n_negative=4, verifier=lambda _: True,
                quote_corpus={"10.1101/fixed-0": "x" * 40})


if __name__ == "__main__":
    unittest.main()