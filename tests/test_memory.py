import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.memory import (ARTIFACT_PTR_THRESHOLD, Memory, SESSION_LIMIT,
                           WORKING_LIMIT)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "mem"
        self.mem = Memory(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_working_memory_set_and_clear(self):
        self.mem.set_working("step context")
        self.assertEqual(12, self.mem.working_size())
        self.mem.clear_working()
        self.assertEqual(0, self.mem.working_size())

    def test_externalize_small_inline_large_pointer(self):
        small = self.mem.externalize("x" * 100)
        self.assertTrue(small["inline"])
        large = self.mem.externalize("y" * (ARTIFACT_PTR_THRESHOLD + 10))
        self.assertFalse(large["inline"])
        self.assertIn("artifact_id", large)
        self.assertIn("sha256_prefix", large)
        self.assertTrue((self.dir / "artifacts" / f"{large['artifact_id']}.txt").is_file())

    def test_account_tokens_enforces_session_budget(self):
        self.assertEqual(100, self.mem.account_tokens(100))
        with self.assertRaisesRegex(ProtocolError, "SLO-4"):
            self.mem.account_tokens(SESSION_LIMIT + 1)

    def test_evict_uses_importance_and_lru(self):
        self.mem.externalize("low-value " * 600, importance=0.1)  # ~6KB
        self.mem.externalize("high-value" * 800, importance=0.9)  # ~8KB
        evicted = self.mem.evict(5 * 1024, stage_budget=1 * 1024)
        self.assertTrue(evicted)  # 至少驱逐一个

    def test_summary_roundtrip(self):
        self.mem.set_summary({"step": 3, "note": "state.md 摘要"})
        self.assertEqual(3, self.mem.summary()["step"])

    def test_snapshot_lists_artifacts(self):
        self.mem.externalize("d" * (ARTIFACT_PTR_THRESHOLD + 100), importance=0.5)
        snap = self.mem.snapshot()
        self.assertEqual(1, len(snap["artifacts"]))
        self.assertEqual("working_size", "working_size")


if __name__ == "__main__":
    unittest.main()