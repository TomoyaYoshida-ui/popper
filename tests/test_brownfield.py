import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, file_hash
from popper.brownfield import Brownfield
from popper.evidence import EvidenceStore


def _make_workspace(root):
    (root / "train.py").write_text("def main():\n    model.fit(X_train, y_train)\n", encoding="utf-8")
    (root / "config.yaml").write_text("lr: 0.001\n", encoding="utf-8")
    (root / "results").mkdir(parents=True, exist_ok=True)
    (root / "results" / "train.log").write_text("epoch: 1 accuracy: 0.95\nepoch: 2 loss: 0.1\n",
                                               encoding="utf-8")
    (root / "paper.md").write_text("# Title\n\n准确率达到 [[claim:C001]] 0.95 [[/claim]]。\n",
                                   encoding="utf-8")
    return root


class BrownfieldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_generates_manifest_and_rejects_empty(self):
        _make_workspace(self.root)
        manifest = Brownfield(self.root).load()
        types = {f["type"] for f in manifest["files"]}
        self.assertIn("experiment_script", types)
        self.assertIn("manuscript", types)
        self.assertIn("log", types)
        self.assertTrue((self.root / "workspace_manifest.json").is_file())

        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        # workspace 存在但无稿件/代码 → 拒绝
        with self.assertRaisesRegex(ProtocolError, "至少需要"):
            Brownfield(empty).load()

    def test_manifest_via_cli_load_needs_manifest_for_ingest(self):
        _make_workspace(self.root)
        bf = Brownfield(self.root)
        with self.assertRaisesRegex(ProtocolError, "先运行"):
            bf.ingest()  # 未 load

    def test_ingest_extracts_log_metrics_candidates(self):
        ws = _make_workspace(self.root)
        bf = Brownfield(ws)
        bf.load()
        out = bf.ingest(confirm_all=False)
        self.assertGreaterEqual(out["count"], 2)
        first = out["candidates"][0]
        self.assertIn("source_file", first)
        self.assertIn("line_number", first)
        self.assertIn("mapped_metric_name", first)
        self.assertIn("raw_text", first)
        # 未确认不进入权威 results（但 candidates 已列出待用户确认）
        self.assertFalse(first["confirmed"])
        confirmed_out = bf.ingest(confirm_all=True)
        self.assertTrue(all(c["confirmed"] for c in confirmed_out["candidates"]))

    def test_reproduce_match_and_mismatch(self):
        ws = _make_workspace(self.root)
        bf = Brownfield(ws)
        bf.load()
        bf.ingest(confirm_all=True)
        # 与候选接近的论文值 → match；极端差异 → mismatch
        result = bf.reproduce({"accuracy": 0.95}, tolerance=0.01)
        self.assertEqual("repro_match", result["status"])
        mismatch = bf.reproduce({"accuracy": 0.5}, tolerance=0.01)
        self.assertEqual("repro_mismatch", mismatch["status"])

    def test_audit_real_manuscript(self):
        ws = _make_workspace(self.root)
        store = EvidenceStore(self.root / "ev")
        store.register_claim("C001", "认证准确率", "claim")
        log = ws / "results" / "train.log"
        store.bind("C001", "E001", "0.95", "run#x:1", str(log),
                   sha256=file_hash(log), selector="text:0.95")
        ref = self.root / "reference.json"
        ref.write_text('{"reference_id":"R1"}', encoding="utf-8")
        store.register_reference("R1", "real", metadata_artifact=ref)
        bf = Brownfield(ws)
        report = bf.audit(store, ws / "paper.md")
        self.assertEqual("C001", list(report["claims"].keys())[0])

    def test_gap_report_readiness_and_missing_experiments(self):
        ws = _make_workspace(self.root)
        store = EvidenceStore(self.root / "ev")
        store.register_claim("C001", "认证准确率", "claim")
        ref = self.root / "reference.json"
        ref.write_text('{"reference_id":"R1"}', encoding="utf-8")
        store.register_reference("R1", "real", metadata_artifact=ref)
        log = ws / "results" / "train.log"
        store.bind("C001", "E001", "0.95", "run#x:1", str(log),
                   sha256=file_hash(log), selector="text:0.95")
        bf = Brownfield(ws)
        report = bf.gap_report(store, ws / "paper.md")
        self.assertEqual(100.0, report["readiness_pct"])
        self.assertEqual([], report["missing_evidence"])
        self.assertIn("R1", report["r1_r7"])


if __name__ == "__main__":
    unittest.main()
