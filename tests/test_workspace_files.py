import os
import json
import runpy
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, initialize
from popper.identity import EntityKind, new_id
from popper.workspace_files import ProjectFileConflict, ProjectFiles


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class ProjectFilesTests(unittest.TestCase):
    def test_save_preserves_utf8_bom_and_file_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "script.py"
            source.write_bytes(b"\xef\xbb\xbfprint('old')\n")
            source.chmod(0o640)
            files = ProjectFiles(root)
            original = files.read("script.py")
            self.assertEqual("print('old')\n", original["content"])
            saved = files.write("script.py", "print('new')\n", original["sha256"])
            self.assertEqual(b"\xef\xbb\xbfprint('new')\n", source.read_bytes())
            self.assertEqual(saved["sha256"], files.read("script.py")["sha256"])
            if os.name != "nt":
                self.assertEqual(0o640, stat.S_IMODE(source.stat().st_mode))
            else:
                self.assertTrue(source.stat().st_mode & stat.S_IWUSR)
            source.chmod(stat.S_IREAD)
            try:
                with self.assertRaisesRegex(ProtocolError, "只读"):
                    files.write("script.py", "print('forged')\n", saved["sha256"])
                self.assertEqual(b"\xef\xbb\xbfprint('new')\n", source.read_bytes())
            finally:
                source.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_preview_marks_missing_final_newline_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "note.txt").write_bytes(b"old")
            files = ProjectFiles(root)
            digest = files.read("note.txt")["sha256"]
            preview = files.preview_write("note.txt", "new", digest)
            self.assertIn("-old\n\\ No newline at end of file\n", preview["diff"])
            self.assertIn("+new\n\\ No newline at end of file\n", preview["diff"])
            self.assertEqual(b"old", (root / "note.txt").read_bytes())

    def test_agent_replace_receipt_is_idempotent_and_reconciles_prepared_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "analysis.py"
            source.write_text("value = 1\n", encoding="utf-8")
            files = ProjectFiles(root)
            args = {"path": "analysis.py", "expected_sha256": files.read("analysis.py")["sha256"],
                    "old_text": "value = 1", "new_text": "value = 2"}
            call_id = new_id(EntityKind.TOOL_CALL)
            result = files.apply_replace(call_id, args)
            self.assertEqual("value = 2\n", source.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ProjectFileConflict, "恰好出现一次"):
                files.preview_replace("analysis.py", "value = 1", "value = 9",
                                      result["sha256"])
            self.assertEqual(result, files.apply_replace(call_id, args))
            receipt_path = root / ".popper" / "file-edit-receipts" / (call_id + ".json")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_path.write_text(json.dumps({**receipt, "status": "prepared"}),
                                    encoding="utf-8")
            self.assertEqual(result, files.edit_receipt(call_id, args))
            self.assertEqual("committed", json.loads(receipt_path.read_text(
                encoding="utf-8"))["status"])
            with self.assertRaisesRegex(ProjectFileConflict, "参数不匹配"):
                files.edit_receipt(call_id, {**args, "new_text": "value = 3"})
            with self.assertRaisesRegex(ProjectFileConflict, "别处修改"):
                files.apply_replace(new_id(EntityKind.TOOL_CALL),
                                    {**args, "new_text": "value = 4"})
            self.assertEqual("value = 2\n", source.read_text(encoding="utf-8"))

    def test_bounded_listing_and_optimistic_text_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "analysis.py").write_bytes(b"print('first')\n")
            (root / ".env").write_text("secret", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "hidden.js").write_text("ignored", encoding="utf-8")
            (root / "image.png").write_bytes(b"\x89PNG")
            files = ProjectFiles(root)
            listed = files.list_files()
            self.assertEqual(["src/analysis.py"],
                             [item["path"] for item in listed["files"]])
            original = files.read("src/analysis.py")
            self.assertEqual("print('first')\n", original["content"])
            preview = files.preview_write("src/analysis.py", "print('second')\n",
                                          original["sha256"])
            self.assertIn("-print('first')", preview["diff"])
            self.assertIn("+print('second')", preview["diff"])
            self.assertTrue(preview["changed"])
            self.assertEqual(b"print('first')\n", (root / "src" / "analysis.py").read_bytes())
            saved = files.write("src/analysis.py", "print('second')\n",
                                original["sha256"])
            self.assertEqual(preview["after_sha256"], saved["sha256"])
            self.assertEqual("print('second')\n",
                             (root / "src" / "analysis.py").read_text(encoding="utf-8"))
            self.assertEqual(saved["sha256"], files.read("src/analysis.py")["sha256"])
            with self.assertRaisesRegex(ProjectFileConflict, "别处修改"):
                files.write("src/analysis.py", "stale", original["sha256"])
            self.assertEqual([], list((root / "src").glob(".popper-edit-*")))

    def test_rejects_traversal_hidden_paths_symlinks_and_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            (root / "safe.py").write_text("pass\n", encoding="utf-8")
            files = ProjectFiles(root)
            for path in ("../outside.py", "/outside.py", "src/../../outside.py",
                         ".popper/state.db", "src\\file.py", "missing.py"):
                with self.subTest(path=path), self.assertRaises(ProtocolError):
                    files.read(path)
            outside = Path(temporary) / "outside.py"
            outside.write_text("outside", encoding="utf-8")
            link = root / "linked.py"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaisesRegex(ProtocolError, "符号链接"):
                    files.read("linked.py")
                self.assertNotIn("linked.py", [item["path"] for item in
                                               files.list_files()["files"]])
            outside_dir = Path(temporary) / "outside-dir"
            outside_dir.mkdir()
            (outside_dir / "external.py").write_text("secret", encoding="utf-8")
            linked_dir = root / "linked-dir"
            try:
                linked_dir.symlink_to(outside_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaises(ProtocolError):
                    files.read("linked-dir/external.py")
                self.assertNotIn("linked-dir/external.py", [item["path"] for item in
                                                        files.list_files()["files"]])

    def test_frozen_experiment_inputs_are_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(EXAMPLE / name, root / name)
            runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](root)
            initialize(root)
            (root / "notes.md").write_text("draft", encoding="utf-8")
            files = ProjectFiles(root)
            listed = {row["path"]: row for row in files.list_files()["files"]}
            self.assertFalse(listed["model.py"]["editable"])
            self.assertFalse(listed["experiment.json"]["editable"])
            self.assertTrue(listed["notes.md"]["editable"])
            model = files.read("model.py")
            self.assertFalse(model["editable"])
            with self.assertRaisesRegex(ProtocolError, "冻结"):
                files.write("model.py", "print('forged')", model["sha256"])
            note = files.read("notes.md")
            files.write("notes.md", "revised", note["sha256"])
            self.assertEqual("revised", (root / "notes.md").read_text(encoding="utf-8"))
