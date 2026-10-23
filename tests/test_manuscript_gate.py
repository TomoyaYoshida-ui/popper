"""Manuscript writing gate: markers required; context ids must exist."""
import tempfile
import unittest
from pathlib import Path

from popper.campaign_nodes import materialize_node
from popper.core import ProtocolError
from popper.materialize import Materializer
from popper.orchestrator import CampaignFatal
from popper.research.manuscript_gate import (
    GATE_FAILED, enforce_manuscript_writing, validate_manuscript_writing,
)


KNOWN = ("OBS-known", "LIT-abc", "SCOOP-xyz", "FT-full1")


class ManuscriptGateUnitTests(unittest.TestCase):
    def test_plain_template_without_numbers_passes(self):
        text = "方法描述协议。结论：阈值达成。"
        result = validate_manuscript_writing(text, known_ids=())
        self.assertTrue(result["passed"])
        self.assertEqual("passed", result["status"])

    def test_numeric_claim_with_valid_markers_passes(self):
        text = (
            "确认准确率为 [[claim:C001]]95.3%[[/claim]]"
            "（[[ev:OBS-known]]，[[ref:LIT-abc]]）。"
        )
        result = validate_manuscript_writing(text, known_ids=KNOWN)
        self.assertTrue(result["passed"])
        self.assertEqual(["OBS-known", "LIT-abc"], result["cited_context_ids"])

    def test_numeric_claim_without_markers_fails(self):
        text = "确认准确率为 95.3%，MAE 改善 0.12。"
        result = validate_manuscript_writing(text, known_ids=KNOWN)
        self.assertFalse(result["passed"])
        self.assertEqual(GATE_FAILED, result["status"])
        self.assertTrue(any(p["type"] == "缺标记" for p in result["problems"]))

    def test_numeric_claim_with_fake_id_fails(self):
        text = (
            "准确率 [[claim:C001]]95.3%[[/claim]] 见 [[ev:OBS-fake]] "
            "与 [[ref:LIT-missing]]。"
        )
        result = validate_manuscript_writing(text, known_ids=KNOWN)
        self.assertFalse(result["passed"])
        self.assertEqual(GATE_FAILED, result["status"])
        kinds = {p["type"] for p in result["problems"]}
        self.assertIn("无效 id", kinds)
        texts = {p["text"] for p in result["problems"] if p["type"] == "无效 id"}
        self.assertIn("OBS-fake", texts)
        self.assertIn("LIT-missing", texts)

    def test_markers_without_context_id_fail_for_numeric(self):
        text = "准确率为 [[claim:C001]]95.3%[[/claim]]。"
        result = validate_manuscript_writing(text, known_ids=KNOWN)
        self.assertFalse(result["passed"])
        self.assertTrue(any(p["type"] == "缺上下文引用" for p in result["problems"]))

    def test_enforce_raises_protocol_error(self):
        manuscript = {
            "title": "t",
            "abstract": "准确率达到 90%。",
            "sections": [],
        }
        with self.assertRaises(ProtocolError) as ctx:
            enforce_manuscript_writing(manuscript, known_ids=KNOWN)
        self.assertIn(GATE_FAILED, str(ctx.exception))
        self.assertEqual(GATE_FAILED, ctx.exception.gate_status)


class MaterializeWritingGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "model.py").write_text("def main():\n    return 0\n", encoding="utf-8")
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_materialize_refuses_unmarked_numeric_claim(self):
        manuscript = {
            "title": "结果",
            "abstract": "MAE 改善 0.12。",
            "sections": [{"heading": "结论", "body": "准确率 95%。"}],
        }
        with self.assertRaises(ProtocolError) as ctx:
            Materializer(self.project).materialize(
                manuscript, self.out, known_ids=KNOWN)
        self.assertIn("写作门禁", str(ctx.exception))
        self.assertFalse(any(self.out.iterdir()))

    def test_materialize_accepts_marked_valid_ids(self):
        manuscript = {
            "title": "结果",
            "abstract": "见证据。",
            "sections": [{
                "heading": "结论",
                "body": "准确率 [[claim:C1]]95.3%[[/claim]]（[[ev:OBS-known]]）。",
            }],
        }
        result = Materializer(self.project).materialize(
            manuscript, self.out, known_ids=KNOWN)
        self.assertEqual("materialized", result["status"])
        self.assertEqual("passed", result["writing_gate"])
        self.assertTrue(Path(result["manuscript"]).is_file())

    def test_campaign_materialize_fatal_on_gate_failure(self):
        run_dir = self.root / "camp"
        run_dir.mkdir()
        state = {
            "project": str(self.project),
            "materialize": {
                "manuscript": {
                    "title": "c",
                    "abstract": "准确率 99%。",
                    "sections": [{"heading": "方法", "body": "b"}],
                },
                "template": "md",
                "disclosure": "nature",
            },
        }
        with self.assertRaises(CampaignFatal) as ctx:
            materialize_node(run_dir, state)
        self.assertIn("写作门禁", str(ctx.exception))
        self.assertFalse((self.project / "deliverables").exists())


if __name__ == "__main__":
    unittest.main()
