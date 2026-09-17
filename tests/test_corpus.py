import json
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.corpus import Corpus


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_init_add_stats_incremental(self):
        corpus = Corpus(self.dir)
        self.assertEqual("initialized", corpus.initialize()["status"])
        self.assertEqual("already_initialized", corpus.initialize()["status"])

        added = corpus.add("gap", "arXiv:2509.23189", "AutoEP 缺省静态超参数配置")
        self.assertEqual("added", added["status"])
        self.assertEqual(1, added["library_size"])

        added2 = corpus.add("contradiction", "doi:10.1/abc",
                            "两篇文献对 δ 判定阈值存在矛盾", novelty_tag="incremental")
        self.assertEqual("added", added2["status"])

        dup = corpus.add("gap", "arXiv:2509.23189", "AutoEP 缺省静态超参数配置")
        self.assertEqual("already_exists", dup["status"])

        stats = corpus.stats()
        self.assertEqual(2, stats["count"])
        self.assertEqual({"gap": 1, "contradiction": 1}, stats["by_type"])
        self.assertIsNotNone(stats["latest_update"])

        records = corpus.verified_records()
        self.assertEqual(2, len(records))  # 本地自然记录均可进入 claim 链路
        self.assertEqual({"gap", "contradiction"}, {r["type"] for r in records})

    def test_add_support_level(self):
        corpus = Corpus(self.dir)
        corpus.initialize()
        added = corpus.add("gap", "arXiv:2509.23189", "带支持等级的描述", support_level="full")
        self.assertEqual("added", added["status"])
        records = corpus.verified_records()
        self.assertEqual("full", records[0]["support_level"])
        with self.assertRaisesRegex(ProtocolError, "support_level"):
            corpus.add("gap", "arXiv:2509.23200", "非法支持等级", support_level="bogus")

    def test_invalid_literature_id_and_type_rejected(self):
        corpus = Corpus(self.dir)
        corpus.initialize()
        with self.assertRaisesRegex(ProtocolError, "literature_id 无法解析"):
            corpus.add("gap", "not a valid id", "描述")
        with self.assertRaisesRegex(ProtocolError, "type 必须是"):
            corpus.add("unknown", "arXiv:2509.23189", "描述")
        stats = corpus.stats()
        self.assertEqual(0, stats["count"])

    def test_share_queues_but_community_isolated_until_verified(self):
        corpus = Corpus(self.dir)
        corpus.initialize()
        rid = corpus.add("foresight", "arXiv:2501.00001",
                         "窗口法超参自适应是前瞻信号")["record_id"]
        queued = corpus.share(rid)
        self.assertEqual("queued", queued["status"])
        self.assertTrue(Path(queued["outbox"]).is_file())
        self.assertEqual([], corpus.list_community())  # 社区池尚未收到
        self.assertEqual(1, len(corpus.verified_records()))  # review_pending 不影响本地

        # 填充社区池：直接从 disk 模拟一个社区来源提交。
        community_payload = {
            "record_id": "comm-1", "type": "negative_result",
            "literature_id": "doi:10.2/comm", "description": "社区反馈的负结果",
            "novelty_tag": None, "timestamp": "2026-01-01T00:00:00+00:00",
            "source": "community", "verification": "verified",
        }
        community_path = self.dir / "community" / "comm.json"
        community_path.parent.mkdir(parents=True, exist_ok=True)
        community_path.write_text(json.dumps(community_payload), encoding="utf-8")

        listed = corpus.list_community()
        self.assertEqual(1, len(listed))
        # 二次验证前，社区记录不可被 claim 链路读取（隔离）。
        self.assertNotIn("comm-1", {r["record_id"] for r in corpus.verified_records()})

        imported = corpus.verify_community()
        self.assertEqual(["comm-1"], imported["record_ids"])
        self.assertEqual(2, len(corpus.verified_records()))
        verified = {r["record_id"]: r for r in corpus.verified_records()}
        self.assertEqual("community", verified["comm-1"]["source"])

        # 幂等：再次二次验证不重复导入。
        self.assertEqual(0, corpus.verify_community()["count"])

    def test_share_rejects_community_source_and_missing_record(self):
        corpus = Corpus(self.dir)
        corpus.initialize()
        with self.assertRaisesRegex(ProtocolError, "不存在"):
            corpus.share("nope")
        payload = {"record_id": "comm-2", "type": "gap", "literature_id": "arXiv:2502.00002",
                   "description": "社区版", "novelty_tag": None,
                   "timestamp": "2026-01-01T00:00:00+00:00", "source": "community",
                   "verification": "verified"}
        community_path = self.dir / "community" / "x.json"
        community_path.parent.mkdir(parents=True, exist_ok=True)
        community_path.write_text(json.dumps(payload), encoding="utf-8")
        corpus.verify_community()
        with self.assertRaisesRegex(ProtocolError, "社区来源记录不可再次共享"):
            corpus.share("comm-2")


if __name__ == "__main__":
    unittest.main()