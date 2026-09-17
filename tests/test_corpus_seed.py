import tempfile
import unittest
from pathlib import Path

from popper.corpus import VALID_TYPES, _LITERATURE_ID_PATTERN, Corpus
from popper.corpus_seed import _builtin_seeds, seed


class CorpusSeedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_seed_batch_adds_200(self):
        result = seed(self.dir, count=200)
        self.assertEqual("seeded", result["status"])
        self.assertEqual(200, result["inserted"])
        self.assertGreaterEqual(result["count"], 200)

    def test_seed_is_idempotent(self):
        first = seed(self.dir, count=200)
        second = seed(self.dir, count=200)
        self.assertEqual(200, first["count"])
        self.assertEqual(0, second["inserted"])  # 重复 seed 不重复插入
        self.assertEqual(200, second["count"])

    def test_builtin_seeds_valid_types_and_identifiers(self):
        seeds = _builtin_seeds(200)
        self.assertEqual(200, len(seeds))
        for item in seeds:
            self.assertIn(item["type"], VALID_TYPES)
            self.assertTrue(item["literature_id"])
            self.assertTrue(_LITERATURE_ID_PATTERN.match(item["literature_id"]))  # 可解析
            self.assertIsNone(item["novelty_tag"])

    def test_seed_type_distribution_within_four(self):
        result = seed(self.dir, count=200)
        self.assertTrue(set(result["by_type"]) <= set(VALID_TYPES))
        corpus = Corpus(self.dir)
        self.assertEqual(200, corpus.stats()["count"])


if __name__ == "__main__":
    unittest.main()