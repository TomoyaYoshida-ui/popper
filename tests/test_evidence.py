import json
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, file_hash
from popper.domains import registered_metrics
from popper.evidence import (BOUND, EvidenceStore, MetricsRegistry,
                             lint_manuscript, parse_manuscript, run_checks,
                             check_references)


def _register_real(store, directory, reference_id):
    metadata = Path(directory) / f"{reference_id}-metadata.json"
    metadata.write_text(json.dumps({"reference_id": reference_id}), encoding="utf-8")
    return store.register_reference(reference_id, "real", metadata_artifact=metadata)


class MetricsTests(unittest.TestCase):
    def test_default_metrics_and_registry_load(self):
        # 默认指标表由域包注册表导出，不是第三套命名体系。
        expected = {"mse", "accuracy", "mae", "f1", "macro_f1", "throughput",
                    "convergence_order", "ate_error"}
        self.assertEqual(expected, set(registered_metrics()))
        reg = MetricsRegistry()
        self.assertEqual(expected, set(reg.names()))
        self.assertEqual({name: spec.direction for name, spec in registered_metrics().items()},
                         {name: entry["direction"] for name, entry in reg.load()["metrics"].items()})
        reg.require_registered("mse")
        with self.assertRaisesRegex(ProtocolError, "未登记"):
            reg.require_registered("unknown_metric")

    def test_custom_registry_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "metrics.json"
            path.write_text(json.dumps({"schema_version": "1.0",
                                        "metrics": {"f1": {"direction": "max"}}}), encoding="utf-8")
            reg = MetricsRegistry(path)
            self.assertEqual({"f1"}, set(reg.names()))


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name) / "ev"
        self.store = EvidenceStore(self.store_dir)
        self.store.register_claim("C001", "最终测试准确率", "claim")
        self.store.register_claim("M001", "使用 Adam，lr=0.001", "method")
        self.artifact = Path(self._tmp.name) / "results.json"
        self.artifact.write_text('{"seed": 1, "value": 0.9649}', encoding="utf-8")
        self.fulltext = Path(self._tmp.name) / "paper.txt"
        self.fulltext.write_text(
            "The verified full text supports the claim under the stated conditions.",
            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _bind(self, claim_id, evidence_id, value, source_ref, artifact_id=None, **kwargs):
        """绑定并携带真实制品文件哈希（不再允许假摘要兜底）。"""
        kwargs.setdefault("sha256", file_hash(self.artifact))
        kwargs.setdefault("selector", "value")
        return self.store.bind(claim_id, evidence_id, value, source_ref,
                               str(self.artifact), **kwargs)

    def test_register_claim_category_field_map(self):
        with self.assertRaisesRegex(ProtocolError, "category"):
            self.store.register_claim("X", "bad category", "nope")
        self.store.register_claim("O001", "loss 在第 50 epoch 收敛", "observation")
        self.assertEqual("实验结果/指标观测", self.store.registry["claims"]["O001"]["field"])

    def test_bind_then_fix_locally(self):
        self._bind("C001", "E001", "0.9649", "run-1#train.py:120", "results.json")
        self.assertEqual(BOUND, self.store.registry["claims"]["C001"]["status"])
        # M001 仍在 setUp 中登记但未绑定。
        self.assertEqual({"M001"}, {c["claim_id"] for c in self.store.unbound_claims()})
        second = Path(self._tmp.name) / "results-2.json"
        second.write_text('{"seed": 2, "value": 0.9386}', encoding="utf-8")
        self.store.bind("C001", "E002", "0.9386", "run-2#train.py:130",
                        str(second), selector="value")
        self.store.fix_claim("C001", "E001")
        # fix 只保留目标 evidence，不触发全量重算。
        self.assertEqual(["E001"], self.store.registry["claims"]["C001"]["evidence_ids"])
        self.assertEqual("0.9649", self.store.registry["claims"]["C001"]["value"])

    def test_conflicting_evidence_marks_claim_disputed_until_fixed(self):
        self._bind("C001", "E001", "0.9649", "run-1#result")
        second = Path(self._tmp.name) / "conflict.json"
        second.write_text('{"value": 0.9386}', encoding="utf-8")
        self.store.bind("C001", "E002", "0.9386", "run-2#result",
                        str(second), selector="value")
        claim = self.store.registry["claims"]["C001"]
        self.assertEqual("disputed", claim["status"])
        self.assertIsNone(claim["value"])
        self.store.fix_claim("C001", "E001")
        self.assertEqual(BOUND, claim["status"])
        self.assertEqual("0.9649", claim["value"])

    def test_unbound_reflects_only_unbound(self):
        self.assertEqual(2, len(self.store.unbound_claims()))
        self._bind("C001", "E001", "0.9649", "run#x:1", "results.json")
        self.assertEqual({"M001"}, {c["claim_id"] for c in self.store.unbound_claims()})

    def test_bind_requires_real_artifact_hash(self):
        # 无法解析真实文件且未显式给 sha256 → 拒绝，不得用 evidence_id 摘要冒充文件哈希。
        with self.assertRaisesRegex(ProtocolError, "真实文件"):
            self.store.bind("C001", "E001", "0.5", "run#x:1", "no-such-file.json")

    def test_method_claim_also_requires_extractable_selector(self):
        method_log = Path(self._tmp.name) / "method.txt"
        method_log.write_text("optimizer=Adam lr=0.001", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "selector"):
            self.store.bind("M001", "EM1", "optimizer=Adam", "run#config",
                            str(method_log))
        self.store.bind("M001", "EM1", "optimizer=Adam", "run#config",
                        str(method_log), selector="text:optimizer=Adam")

    def test_bind_computes_hash_from_real_file(self):
        self.store.bind("C001", "E001", "0.9649", "run#a:1", str(self.artifact),
                        selector="value")
        self.assertEqual(file_hash(self.artifact),
                         self.store.registry["evidence"]["E001"]["sha256"])

    def test_bind_rejects_wrong_explicit_sha256(self):
        with self.assertRaisesRegex(ProtocolError, "不一致"):
            self.store.bind("C001", "E001", "0.9649", "run#x:1", str(self.artifact),
                            sha256="a" * 64, selector="value")

    def test_bind_recomputes_selected_value(self):
        with self.assertRaisesRegex(ProtocolError, "selector 结果不一致"):
            self.store.bind("C001", "E001", "0.9", "run#x:1", str(self.artifact),
                            selector="value")
        self.store.bind("C001", "E001", "0.9649", "run#x:1", str(self.artifact),
                        selector="value")

    def test_reference_registration(self):
        _register_real(self.store, self._tmp.name, "R1")
        self.store.register_reference("R2", "hallucinated")
        with self.assertRaisesRegex(ProtocolError, "引用状态"):
            self.store.register_reference("R3", "yikes")

    def test_reference_support_level_default_unavailable(self):
        _register_real(self.store, self._tmp.name, "R1")
        self.assertEqual("unavailable", self.store.registry["references"]["R1"]["support_level"])

    def test_fix_claim_rejects_missing_or_foreign_evidence(self):
        with self.assertRaisesRegex(ProtocolError, "evidence 不存在"):
            self.store.fix_claim("C001", "MISSING")

    def test_reference_support_cannot_create_or_invent_quote(self):
        with self.assertRaisesRegex(ProtocolError, "先登记"):
            self.store.set_reference_support("UNKNOWN", "invented", level="full",
                                             artifact_id=self.fulltext)
        _register_real(self.store, self._tmp.name, "R1")
        with self.assertRaisesRegex(ProtocolError, "连续原文"):
            self.store.set_reference_support("R1", "invented passage", level="full",
                                             artifact_id=self.fulltext)

    def test_set_reference_support_requires_explicit_level(self):
        # 按片段长度猜支持等级的通过路径必须删除：未显式给出 level 即拒绝。
        with self.assertRaisesRegex(ProtocolError, "显式"):
            self.store.set_reference_support("R1", "some long snippet text")

    def test_set_reference_support_explicit_level(self):
        _register_real(self.store, self._tmp.name, "R2")
        self.store.set_reference_support("R2", "verified full text supports the claim",
                                         level="partial", artifact_id=self.fulltext)
        self.assertEqual("partial", self.store.registry["references"]["R2"]["support_level"])
        with self.assertRaisesRegex(ProtocolError, "support_level"):
            self.store.set_reference_support("R3", "x", level="bogus")

    def test_check_references_reports_supports(self):
        _register_real(self.store, self._tmp.name, "R1")
        self.store.set_reference_support("R1", "verified full text supports the claim",
                                         level="full", artifact_id=self.fulltext)
        ms = Path(self._tmp.name) / "ms.md"
        ms.write_text("See [[ref:R1]] claim", encoding="utf-8")
        out = check_references(ms, self.store)
        self.assertEqual(0, out["count"])
        self.assertEqual("full", out["supports"][0]["support_level"])

    def test_reference_metadata_or_fulltext_tamper_is_detected(self):
        metadata_result = _register_real(self.store, self._tmp.name, "R1")
        self.store.set_reference_support("R1", "verified full text supports the claim",
                                         level="full", artifact_id=self.fulltext)
        ms = Path(self._tmp.name) / "ms.md"
        ms.write_text("See [[ref:R1]].", encoding="utf-8")
        metadata = Path(self._tmp.name) / "R1-metadata.json"
        metadata.write_text('{"reference_id": "OTHER"}', encoding="utf-8")
        self.assertEqual(["R1"], check_references(ms, self.store)["unresolved"])

        # 恢复元数据文件和登记摘要后，再验证全文篡改也会被拒绝。
        metadata.write_text('{"reference_id": "R1"}', encoding="utf-8")
        self.store.registry["references"]["R1"]["metadata_sha256"] = file_hash(metadata)
        self.fulltext.write_text("changed", encoding="utf-8")
        self.assertEqual(["R1"], check_references(ms, self.store)["unresolved"])


class ManuscriptCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store_dir = self.dir / "ev"
        self.store = EvidenceStore(self.store_dir)
        self.manuscript = self.dir / "paper.md"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text):
        self.manuscript.write_text(text, encoding="utf-8")
        return self.manuscript

    def test_parse_extracts_channels_and_bare_numbers(self):
        text = "我们的准确率提到 95.3%（见 [[claim:C001]] [[ref:R1]] 与 [[ev:E001]]）。"
        parsed = parse_manuscript(text)
        self.assertEqual(["C001"], [c["claim_id"] for c in parsed["claims"]])
        self.assertEqual(["R1"], [r["reference_id"] for r in parsed["refs"]])
        self.assertEqual(["E001"], [e["evidence_id"] for e in parsed["evs"]])
        self.assertEqual(1, len(parsed["bare_numbers"]))

    def test_lint_flags_bare_number_and_unbound_claim(self):
        self.store.register_claim("C001", "认证准确率", "claim")
        text = "认证准确率为 [[claim:C001]] 95.3%。引用 [[ref:R9]]。"
        result = lint_manuscript(self._write(text), self.store)
        self.assertFalse(result["passed"])
        self.assertTrue(any(p["type"] == "裸数字" for p in result["problems"]))
        self.assertTrue(any(p["type"] == "未绑定 claim" for p in result["problems"]))
        self.assertTrue(any(p["type"] == "引用不可解析" for p in result["problems"]))

    def test_channel_number_not_flagged_but_unbound_still_fails(self):
        self.store.register_claim("C001", "认证准确率 95.3%", "claim")
        text = "准确率达到 [[claim:C001]]95.3%[[/claim]] [[ref:R1]]。"
        result = lint_manuscript(self._write(text), self.store)
        # 通道内数字被豁免，但仍因 claim 未绑定而 fail。
        self.assertFalse(any(p["type"] == "裸数字" for p in result["problems"]))
        self.assertTrue(any(p["type"] == "未绑定 claim" for p in result["problems"]))

    def test_full_pass_when_claim_bound_no_bare_number(self):
        self.store.register_claim("C001", "认证准确率 0.9649", "claim")
        _register_real(self.store, self._tmp.name, "R1")
        artifact = Path(self._tmp.name) / "results.json"
        artifact.write_text('{"value": 0.9649}', encoding="utf-8")
        self.store.bind("C001", "E001", "0.9649", "run#a:1", str(artifact),
                        sha256=file_hash(artifact), selector="value")
        text = "认证准确率为 [[claim:C001]]0.9649[[/claim]]（[[ref:R1]]）。"
        result = lint_manuscript(self._write(text), self.store)
        self.assertTrue(result["passed"])

    def test_run_checks_aggregates(self):
        self.store.register_claim("C001", "x", "claim")
        _register_real(self.store, self._tmp.name, "R1")
        self._write("数字 42 且 [[ref:R1]]。")
        checks = run_checks(self.manuscript, self.store)
        self.assertEqual(["audit", "consistency", "references", "lint"], list(checks.keys()))
        self.assertEqual([], checks["references"]["unresolved"])
        self.assertEqual(0, checks["references"]["count"])


if __name__ == "__main__":
    unittest.main()
