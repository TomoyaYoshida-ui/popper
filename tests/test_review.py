import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.evidence import EvidenceStore
from popper.review import RejectionReview


class RejectionReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = EvidenceStore(self.dir / "ev")

    def tearDown(self):
        self._tmp.cleanup()

    def test_r1_is_provisional_and_never_binary(self):
        r = RejectionReview()
        result = r.assess({})["rejection_side"]["risks"]["R1"]
        self.assertEqual("warn", result["status"])
        self.assertTrue(result.get("provisional"))

    def test_r2_rejects_small_delta_and_passes_large(self):
        r = RejectionReview()
        small = r.assess({"delta": 0.01, "min_improvement": 0.02})["rejection_side"]["risks"]["R2"]
        self.assertEqual("fail", small["status"])
        large = r.assess({"delta": 0.05, "min_improvement": 0.02})["rejection_side"]["risks"]["R2"]
        self.assertEqual("pass", large["status"])

    def test_r3_requires_ablation_for_each_mechanism(self):
        r = RejectionReview()
        report = {"mechanisms": [{"name": "m1", "ablation": "E1"}, {"name": "m2", "ablation": None}]}
        result = r.assess(report)["rejection_side"]["risks"]["R3"]
        self.assertTrue(result["status"] == "fail")
        self.assertIn("m2", result["reason"])

    def test_r4_detects_contradiction(self):
        r = RejectionReview()
        result = r.assess({"contradiction": True})["rejection_side"]["risks"]["R4"]
        self.assertEqual("fail", result["status"])

    def test_r6_r7_require_user_adjudication(self):
        r = RejectionReview()
        r6 = r.assess({})["rejection_side"]["risks"]["R6"]
        r7 = r.assess({})["rejection_side"]["risks"]["R7"]
        self.assertTrue(r6.get("user_adjudication"))
        self.assertTrue(r7.get("user_adjudication"))

    def test_mode7_detects_hits(self):
        r = RejectionReview()
        report = {"mode7": {"hallucinated_citation": "引用了不存在的文献"}}
        fraud = r.assess(report)["fraud_side"]
        hits = [m for m in fraud["modes"] if m["status"] == "hit"]
        self.assertEqual(["hallucinated_citation"], [h["mode"] for h in hits])
        self.assertEqual("fail", fraud["summary"]["status"])

    def test_summary_flags_mechanical_fatal(self):
        r = RejectionReview()
        report = {"delta": 0.01, "min_improvement": 0.02, "contradiction": True,
                  "mechanisms": []}
        summary = r.assess(report)["rejection_side"]["summary"]
        self.assertEqual("fail", summary["status"])

    def test_from_manuscript_requires_path(self):
        with self.assertRaisesRegex(ProtocolError, "稿件路径"):
            RejectionReview(self.store).from_manuscript(None, {})

    def test_from_manuscript_flags_bare_number_as_fraud_risk(self):
        manuscript = self.dir / "paper.md"
        manuscript.write_text("准确率为 95.3%。[[claim:C001]]", encoding="utf-8")
        self.store.register_claim("C001", "准确率", "claim")
        r = RejectionReview(self.store, manuscript)
        result = r.from_manuscript(manuscript, {"delta": 0.05, "min_improvement": 0.02,
                                                "mechanisms": []})
        modes = {m["mode"]: m for m in result["fraud_side"]["modes"]}
        self.assertEqual("hit", modes["hallucinated_result"]["status"])


if __name__ == "__main__":
    unittest.main()