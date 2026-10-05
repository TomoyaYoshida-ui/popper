import runpy
import shutil
import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from popper.core import ProtocolError, digest, file_hash, initialize, read_json, write_json
from popper.research.revisions import CodeEdit, RevisionDiffArchive, RevisionStore
from popper.cli import main


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class RevisionDiffArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.project / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        self.store = RevisionStore(root / "revisions")

    def tearDown(self):
        self.tmp.cleanup()

    def _revision(self, suffix, parent=None):
        source = ((self.store.path(parent) / "code/model.py") if parent
                  else self.project / "model.py")
        return self.store.create(
            self.project, "H1", "D1",
            [CodeEdit("model.py", source.read_text(encoding="utf-8") + suffix,
                      file_hash(source))], "test", parent_revision_id=parent)

    def _make_legacy(self, revision):
        old_dir = self.store.path(revision["revision_id"])
        manifest = read_json(old_dir / "revision.json")
        manifest["identity"].pop("diff_sha256", None)
        manifest.pop("diff", None)
        legacy_id = "REV-" + digest(manifest["identity"])[:24]
        manifest["revision_id"] = legacy_id
        write_json(old_dir / "revision.json", manifest)
        old_dir.rename(self.store.root / legacy_id)
        return legacy_id

    def test_child_diff_migration_is_sidecar_and_idempotent(self):
        parent = self._revision("\n# parent\n")
        child = self._revision("\n# child\n", parent["revision_id"])
        legacy_id = self._make_legacy(child)
        manifest_path = self.store.path(legacy_id) / "revision.json"
        before = file_hash(manifest_path)

        archive = RevisionDiffArchive(self.store)
        migrated = archive.migrate(legacy_id)
        self.assertEqual("migrated_attestation", migrated["source"])
        self.assertIn("+# child", migrated["diff"])
        self.assertEqual(before, file_hash(manifest_path))
        self.assertEqual(migrated["attestation_id"],
                         archive.migrate(legacy_id)["attestation_id"])
        self.assertEqual("migrated_attestation", archive.resolve(legacy_id)["source"])

        sidecar = archive._path(legacy_id)
        tampered = read_json(sidecar)
        tampered["diff"] += "forged"
        write_json(sidecar, tampered)
        with self.assertRaisesRegex(ProtocolError, "身份摘要"):
            archive.resolve(legacy_id)

    def test_root_legacy_revision_is_not_guessed_from_project(self):
        root = self._revision("\n# root\n")
        legacy_id = self._make_legacy(root)
        with self.assertRaisesRegex(ProtocolError, "不能从当前工作目录推测"):
            RevisionDiffArchive(self.store).migrate(legacy_id)
        self.assertFalse((self.store.root / ".diff-attestations").exists())

    def test_native_diff_is_reported_without_sidecar(self):
        revision = self._revision("\n# native\n")
        archive = RevisionDiffArchive(self.store)
        result = archive.migrate(revision["revision_id"])
        self.assertEqual("frozen_identity", result["source"])
        self.assertIsNone(result["attestation_id"])
        self.assertFalse(archive.root.exists())

    def test_cli_migrates_one_explicit_revision(self):
        parent = self._revision("\n# cli parent\n")
        child = self._revision("\n# cli child\n", parent["revision_id"])
        legacy_id = self._make_legacy(child)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["research", "migrate-diffs", str(self.store.root.parent),
                         "--revision", legacy_id])
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("migrated_attestation", payload["source"])
        self.assertEqual(legacy_id, payload["revision_id"])


if __name__ == "__main__":
    unittest.main()
