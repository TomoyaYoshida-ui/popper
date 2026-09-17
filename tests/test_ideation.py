import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.corpus import Corpus
from popper.ideation import IdeationRun


class IdeationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.corpus = Corpus(self.dir / "corpus")
        self.corpus.initialize()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, type_="gap", desc="输入参数默认配置不合理", lit="arXiv:2509.23189"):
        return self.corpus.add(type_, lit, desc)["record_id"]

    def _run(self, name="run"):
        return IdeationRun(self.dir / name)

    def test_ideate_builtin_gap_operator(self):
        rid = self._seed(desc="AutoEP 输入参数默认配置不合理")
        run = self._run()
        card = run.ideate(self.corpus, operator="gap-to-problem", question="超参自适应")
        self.assertIn("超参自适应", card["title"])
        self.assertEqual("选题", card["kind"])
        self.assertIn(rid, card["motivation"])
        self.assertTrue(card["falsifiable_prediction"])
        self.assertTrue(card["minimal_discriminating_experiment"])
        # novelty 分级永不二值，且锚定语料近邻文献
        self.assertIn(card["novelty"]["level"], ("major", "incremental", "trivial"))
        self.assertEqual(card["novelty"]["anchors"], ["arXiv:2509.23189"])

    def test_compose_requires_registration(self):
        run = self._run()
        with self.assertRaisesRegex(ProtocolError, "未登记"):
            run.ideate(self.corpus, operator="compose(replace-module, negative-result-pivot)")

    def test_compose_after_registration_produces_card(self):
        self._seed(desc="模型换模块后机制不明")
        run = self._run()
        reg = run.register_operator("replace-then-pivot", "replace-module", "negative-result-pivot")
        self.assertEqual("registered", reg["status"])
        # 重复登记幂等
        again = run.register_operator("replace-then-pivot", "replace-module", "negative-result-pivot")
        self.assertEqual("already_registered", again["status"])
        card = run.ideate(self.corpus, operator="replace-then-pivot", question="换损失函数")
        self.assertTrue(card["kind"].startswith("组合"))
        self.assertIn("换损失函数", card["title"])

    def test_commit_is_idempotent_and_persisted(self):
        self._seed()
        run = self._run()
        card = run.ideate(self.corpus, operator="gap-to-problem")
        first = run.commit(card)
        self.assertEqual("created", first["status"])
        second = run.commit(card)
        self.assertEqual("already_exists", second["status"])
        data = run._load_output()
        self.assertEqual(1, len(data))

    def test_invalid_kind_operator_rejected(self):
        run = self._run()
        # 直接构造一个非法 kind 的已登记组合算子非法输入，应被拒绝。
        with self.assertRaisesRegex(ProtocolError, "组合算子必须"):
            run.register_operator("bad", 1, None)


if __name__ == "__main__":
    unittest.main()