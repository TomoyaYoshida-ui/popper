import contextlib
import io
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path

from popper.core import Experiment, ProtocolError, initialize, read_json
from popper.cli import main
from popper.evidence import EvidenceStore
from popper.manuscript_flow import ManuscriptFlow


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class ManuscriptFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.root / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.root)
        self.ms = self.root / "manuscript.md"
        # 稿件正文：引用 r1 与 claim c1（供 gap 检查使用）。
        self.ms.write_text(
            "实验改进目标结果见 [[claim:c1]][[ev:e1]]。依据 [[ref:r1]] 的方法设计对比。",
            encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def start(self):
        initialize(self.root)
        return Experiment(self.root)

    def _write_ms(self, text):
        p = self.root / "m2.md"
        p.write_text(text, encoding="utf-8")
        return str(p)

    def _paper(self, *argv):
        """运行 CLI 的 paper 子命令，返回 stdout 解析出的 JSON。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(list(argv))
        self.assertEqual(0, code)
        return json.loads(buf.getvalue())

    def test_state_machine_migration_and_rollback(self):
        exp = self.start()
        flow = ManuscriptFlow(exp)
        ms = str(self.ms)
        self.assertEqual("draft", flow.current(ms))

        # 合法前向迁移：draft -> reviewed -> submitted -> published
        flow.transition(ms, "reviewed")
        self.assertEqual("reviewed", flow.current(ms))
        flow.transition(ms, "submitted")
        self.assertEqual("submitted", flow.current(ms))
        flow.transition(ms, "published")
        self.assertEqual("published", flow.current(ms))

        # 回退到更早状态并记原因
        flow.revert(ms, "submitted", "期刊退稿需修改后重投")
        self.assertEqual("submitted", flow.current(ms))
        # 回退必须携带原因
        with self.assertRaisesRegex(ProtocolError, "原因"):
            flow.revert(ms, "reviewed", "   ")

        # 状态文件已落盘
        data = read_json(flow.path)
        entry = data["manuscripts"][ms]
        self.assertEqual("submitted", entry["state"])
        self.assertEqual("期刊退稿需修改后重投", entry["transitions"][-1]["reason"])

    def test_illegal_migration_rejected(self):
        exp = self.start()
        flow = ManuscriptFlow(exp)
        ms = self._write_ms("新稿件")
        # draft -> published 直接跳跃：非法迁移
        with self.assertRaisesRegex(ProtocolError, "非法迁移"):
            flow.transition(ms, "published")
        # 回退到同状态或更后状态同样非法
        flow.transition(ms, "reviewed")
        with self.assertRaisesRegex(ProtocolError, "非法回退"):
            flow.revert(ms, "reviewed", "原地踏步")
        with self.assertRaisesRegex(ProtocolError, "非法回退"):
            flow.revert(ms, "submitted", "不能前进")
        # 未知目标状态
        with self.assertRaisesRegex(ProtocolError, "未知目标状态"):
            flow.transition(ms, "archived")

    def test_gap_report_missing_store_and_unresolved(self):
        exp = self.start()
        flow = ManuscriptFlow(exp)
        ms = str(self.ms)
        flow.transition(ms, "reviewed")

        # 无 evidence store：gaps 含 missing_store，迁移仍完成
        res = flow.submit(ms, evidence_dir=None)
        self.assertEqual("submitted", res["status"])
        self.assertIn("missing_store", res["gap_report"]["gaps"])
        self.assertTrue(res["gap_report"]["gaps_present"])

        # 有 store 但引用 unresolved / claim 未绑定：记 unresolved_reference / missing_evidence
        ms2 = self._write_ms("见 [[ref:r1]] 与 [[claim:c1]] 的对比分析")
        flow.transition(ms2, "reviewed")
        report = flow.gap_report(ms2, str(self.root / "evidence"))
        self.assertIn("unresolved_reference:r1", report["gaps"])
        self.assertIn("missing_evidence:c1", report["gaps"])
        self.assertTrue(report["gaps_present"])

        res2 = flow.submit(ms2, str(self.root / "evidence"))
        self.assertEqual("submitted", res2["status"])  # 迁移仍完成
        self.assertTrue(res2["gap_report"]["gaps_present"])

    def test_share_registers_destination_without_fabrication(self):
        exp = self.start()
        flow = ManuscriptFlow(exp)
        ms = str(self.ms)
        community = str(self.root / "community")
        res = flow.share(ms, community_dir=community)
        self.assertEqual("registered", res["status"])
        self.assertEqual(community, res["destination"])

        # 落盘到 manuscript-flow.json，含 destination / timestamp / status
        entry = read_json(flow.path)["manuscripts"][ms]
        self.assertTrue(any(s["destination"] == community and s["status"] == "shared"
                            for s in entry["shares"]))
        # 未指定去向时拒绝
        with self.assertRaisesRegex(ProtocolError, "destination"):
            flow.share(ms)

    def test_manuscript_flow_events_and_replay_unchanged(self):
        exp = self.start()
        ms = self._write_ms("全文见 [[ref:r1]]。")
        flow = ManuscriptFlow(exp)
        flow.transition(ms, "reviewed")
        flow.submit(ms)          # reviewed -> submitted（gap 报告，缺 store）
        flow.publish(ms)         # submitted -> published

        # 每次迁移写入事件链 kind=manuscript_flow
        rows = exp.db.execute(
            "SELECT payload FROM events WHERE kind='manuscript_flow' ORDER BY seq"
        ).fetchall()
        to_states = [json.loads(r["payload"])["to"] for r in rows]
        self.assertEqual(["reviewed", "submitted", "published"], to_states)

        # replay 会校验事件链完整性；manuscript_flow 事件不破坏之
        verified = exp.replay()
        self.assertEqual("verified", verified["status"])

    def test_paper_cli_submit_publish_revert_status_share(self):
        initialize(self.root)
        ms = str(self.ms)
        # 用 API 前置到 reviewed 后走 CLI submit（CLI 未直接暴露 reviewed 迁移）
        exp = Experiment(self.root)
        ManuscriptFlow(exp).transition(ms, "reviewed")
        exp.close()

        out = self._paper("research", "paper", "submit", str(self.root), "--manuscript", ms)
        self.assertEqual("submitted", out["status"])
        self.assertIn("missing_store", out["gap_report"]["gaps"])

        out = self._paper("research", "paper", "publish", str(self.root), "--manuscript", ms)
        self.assertEqual("published", out["status"])

        out = self._paper("research", "paper", "revert", str(self.root), "submitted",
                          "--reason", "退稿修改", "--manuscript", ms)
        self.assertEqual("submitted", out["status"])

        out = self._paper("research", "paper", "share", str(self.root),
                          "--manuscript", ms, "--community-dir", str(self.root / "community"))
        self.assertEqual("registered", out["status"])

        out = self._paper("research", "paper", "status", str(self.root))
        self.assertEqual("submitted", out["manuscripts"][ms]["state"])


if __name__ == "__main__":
    unittest.main()