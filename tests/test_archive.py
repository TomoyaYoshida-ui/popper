"""审计日志压缩归档测试：gzip JSONL 导出 + manifest + 冷存储分层。"""
import gzip
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path

from popper.core import Experiment, initialize

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


def _build_project(root):
    root = Path(root)
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, root / name)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](root)


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        _build_project(cls.root)
        initialize(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_archive_writes_gzip_jsonl_and_manifest(self):
        exp = Experiment(self.root)
        try:
            expected = exp.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            target = self.root / ".popper" / "archive"
            result = exp.archive_events(target)
        finally:
            exp.close()
        self.assertEqual("archived", result["status"])
        self.assertEqual(expected, result["count"])
        archive_path = Path(result["path"])
        self.assertTrue(archive_path.is_file())
        self.assertTrue(archive_path.name.endswith(".jsonl.gz"))
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(expected, manifest["count"])
        self.assertEqual("1.0", manifest["schema_version"])
        self.assertIn("归档不删除原库", manifest["note"])
        self.assertTrue(manifest["source_db"].endswith(str(Path(".popper") / "state.db")))
        with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(expected, len(lines))
        self.assertEqual({"seq", "kind", "payload", "previous", "hash"}, set(lines[0]))
        self.assertEqual(manifest["first_hash"], lines[0]["hash"])
        self.assertEqual(manifest["last_hash"], lines[-1]["hash"])
        self.assertEqual(list(range(1, expected + 1)), [row["seq"] for row in lines])

    def test_cold_storage_moves_archives_older_than_thirty_days(self):
        exp = Experiment(self.root)
        target = self.root / ".popper" / "archive"
        target.mkdir(parents=True, exist_ok=True)
        old = "events-20200101T000000Z.jsonl.gz"
        with gzip.open(target / old, "wt", encoding="utf-8") as handle:
            handle.write("{}\n")
        try:
            result = exp.archive_events(target)
        finally:
            exp.close()
        self.assertIn(old, result["moved_cold"])
        self.assertTrue((target / "cold" / old).is_file())
        self.assertFalse((target / old).exists())


if __name__ == "__main__":
    unittest.main()