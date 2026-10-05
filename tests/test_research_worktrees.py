import runpy
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, file_hash, initialize
from popper.research.revisions import CodeEdit, RevisionStore
from popper.research.worktrees import RevisionWorktreeManager
from popper.server import Workstation


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


@unittest.skipUnless(shutil.which("git"), "git is required")
class RevisionWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.project / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.email", "popper@example.invalid"],
                       cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.name", "Popper Test"],
                       cwd=self.project, check=True)
        subprocess.run(["git", "add", "model.py"], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-qm", "source"], cwd=self.project, check=True)
        self.revisions = RevisionStore(self.root / "revisions")
        source = self.project / "model.py"
        self.revision = self.revisions.create(
            self.project, "H1", "D1",
            [CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# candidate\n",
                      file_hash(source))], "test-policy")
        self.registry = self.root / "worktrees.sqlite"
        self.worktree_root = self.root / "isolated-worktrees"
        self.manager = RevisionWorktreeManager(
            self.registry, self.worktree_root, self.project, self.revisions)

    def tearDown(self):
        if hasattr(self, "manager"):
            for record in self.manager.list():
                if record["status"] == "active":
                    self.manager.remove(record["worktree_id"])
            self.manager.close()
        self.temp.cleanup()

    def test_create_persist_and_remove_isolated_revision_worktree(self):
        record = self.manager.create(self.revision["revision_id"], "worktree-test-0001")
        self.assertEqual("active", record["status"])
        self.assertEqual(record, self.manager.create(
            self.revision["revision_id"], "worktree-test-0001"))
        checkout = Path(record["path"])
        self.assertTrue((checkout / ".git").is_file())
        candidate = checkout / "model.py"
        self.assertEqual(self.revision["files"]["model.py"], file_hash(candidate))
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--", "model.py"],
            cwd=checkout, check=True, capture_output=True, text=True).stdout
        self.assertTrue(status.strip(), "revision overlay must be visible against anchored commit")

        candidate.write_text("# mutable review copy\n", encoding="utf-8")
        immutable = self.revisions.path(self.revision["revision_id"]) / "code/model.py"
        self.assertEqual(self.revision["files"]["model.py"], file_hash(immutable))

        self.manager.close()
        self.manager = RevisionWorktreeManager(
            self.registry, self.worktree_root, self.project, self.revisions)
        self.assertEqual(record["worktree_id"], self.manager.list()[0]["worktree_id"])
        removed = self.manager.remove(record["worktree_id"])
        self.assertEqual("removed", removed["status"])
        self.assertFalse(checkout.exists())
        self.assertEqual(removed, self.manager.remove(record["worktree_id"]))
        self.assertEqual(["provisioning", "activated", "removed"],
                         [event["event_type"] for event in
                          self.manager.history(record["worktree_id"])])
        self.assertTrue(self.manager.verify()["ok"])

    def test_remove_retries_after_git_succeeded_but_event_commit_interrupted(self):
        record = self.manager.create(self.revision["revision_id"], "remove-crash-0001")
        target = Path(record["path"])
        original_append = self.manager._append_event

        def interrupted_remove(worktree_id, event_type, payload):
            if event_type == "removed":
                raise SystemExit("simulated process exit")
            return original_append(worktree_id, event_type, payload)

        with patch.object(self.manager, "_append_event", side_effect=interrupted_remove):
            with self.assertRaises(SystemExit):
                self.manager.remove(record["worktree_id"])
        self.assertFalse(target.exists())
        self.assertEqual("active", self.manager.get(record["worktree_id"])["status"])
        removed = self.manager.remove(record["worktree_id"])
        self.assertEqual("removed", removed["status"])
        self.assertEqual(removed, self.manager.remove(record["worktree_id"]))
        self.assertEqual(["provisioning", "activated", "removed"],
                         [event["event_type"] for event in
                          self.manager.history(record["worktree_id"])])
        self.assertTrue(self.manager.verify()["ok"])

    def test_remove_does_not_claim_success_with_stale_git_registration(self):
        record = self.manager.create(self.revision["revision_id"], "remove-stale-git-0001")
        target = Path(record["path"])
        shutil.rmtree(target)
        with self.assertRaisesRegex(ProtocolError, "Git 仍登记"):
            self.manager.remove(record["worktree_id"])
        self.assertEqual("active", self.manager.get(record["worktree_id"])["status"])
        subprocess.run(["git", "worktree", "prune", "--expire=now"],
                       cwd=self.project, check=True)
        self.assertEqual("removed", self.manager.remove(record["worktree_id"])["status"])
        self.assertTrue(self.manager.verify()["ok"])

    def test_recover_interrupted_provisioning_without_checkout(self):
        original_git = self.manager._git

        def interrupted_git(project, *args):
            if args[:2] == ("worktree", "add"):
                raise SystemExit("simulated process exit")
            return original_git(project, *args)

        with patch.object(self.manager, "_git", side_effect=interrupted_git):
            with self.assertRaises(SystemExit):
                self.manager.create(self.revision["revision_id"], "crash-before-git-0001")
        record = self.manager.list()[0]
        self.assertEqual("provisioning", record["status"])
        self.assertEqual([{"worktree_id": record["worktree_id"], "status": "failed"}],
                         self.manager.recover_provisioning())
        self.assertEqual([], self.manager.recover_provisioning())
        self.assertEqual("failed", self.manager.create(
            self.revision["revision_id"], "crash-before-git-0001")["status"])
        self.assertEqual(["provisioning", "failed"],
                         [event["event_type"] for event in
                          self.manager.history(record["worktree_id"])])
        removed = self.manager.remove(record["worktree_id"])
        self.assertEqual("removed", removed["status"])
        self.assertEqual(["provisioning", "failed", "removed"],
                         [event["event_type"] for event in
                          self.manager.history(record["worktree_id"])])
        self.assertTrue(self.manager.verify()["ok"])

    def test_recover_completed_checkout_before_activation_commit(self):
        original_append = self.manager._append_event

        def interrupted_activation(worktree_id, event_type, payload):
            if event_type == "activated":
                raise SystemExit("simulated process exit")
            return original_append(worktree_id, event_type, payload)

        with patch.object(self.manager, "_append_event", side_effect=interrupted_activation):
            with self.assertRaises(SystemExit):
                self.manager.create(self.revision["revision_id"], "crash-after-git-0001")
        record = self.manager.list()[0]
        self.assertEqual("provisioning", record["status"])
        self.assertEqual(self.revision["files"]["model.py"],
                         file_hash(Path(record["path"]) / "model.py"))
        self.assertEqual([{"worktree_id": record["worktree_id"], "status": "active"}],
                         self.manager.recover_provisioning())
        self.assertEqual([], self.manager.recover_provisioning())
        self.assertEqual("active", self.manager.get(record["worktree_id"])["status"])
        self.assertEqual(["provisioning", "activated"],
                         [event["event_type"] for event in
                          self.manager.history(record["worktree_id"])])
        self.assertTrue(self.manager.verify()["ok"])

    def test_recover_partial_checkout_preserves_evidence(self):
        original_git = self.manager._git

        def interrupted_after_git(project, *args):
            result = original_git(project, *args)
            if args[:2] == ("worktree", "add"):
                raise SystemExit("simulated process exit")
            return result

        with patch.object(self.manager, "_git", side_effect=interrupted_after_git):
            with self.assertRaises(SystemExit):
                self.manager.create(self.revision["revision_id"], "crash-before-overlay-0001")
        record = self.manager.list()[0]
        target = Path(record["path"])
        self.assertTrue(target.is_dir())
        self.assertNotEqual(self.revision["files"]["model.py"],
                            file_hash(target / "model.py"))
        try:
            self.assertEqual("failed", self.manager.recover_provisioning()[0]["status"])
            self.assertTrue(target.is_dir(), "startup must preserve partial checkout")
            self.assertEqual("removed", self.manager.remove(record["worktree_id"])["status"])
            self.assertFalse(target.exists())
            self.assertTrue(self.manager.verify()["ok"])
        finally:
            if target.exists():
                subprocess.run(["git", "worktree", "remove", "--force", str(target)],
                               cwd=self.project, check=True)

    def test_failed_worktree_remove_rejects_unrelated_replacement(self):
        original_git = self.manager._git

        def interrupted_git(project, *args):
            if args[:2] == ("worktree", "add"):
                raise SystemExit("simulated process exit")
            return original_git(project, *args)

        with patch.object(self.manager, "_git", side_effect=interrupted_git):
            with self.assertRaises(SystemExit):
                self.manager.create(self.revision["revision_id"], "failed-replaced-0001")
        record = self.manager.list()[0]
        self.manager.recover_provisioning()
        target = Path(record["path"])
        target.mkdir()
        marker = target / "user-data.txt"
        marker.write_text("must remain", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "不是原 Git 副本"):
            self.manager.remove(record["worktree_id"])
        self.assertEqual("must remain", marker.read_text(encoding="utf-8"))
        self.assertEqual("failed", self.manager.get(record["worktree_id"])["status"])

    def test_trusted_workstation_startup_recovers_provisioning(self):
        self.manager.close()
        registry = self.root / "revision-worktrees.sqlite"
        namespace = hashlib.sha256(str(self.root.resolve()).encode("utf-8")).hexdigest()[:20]
        worktree_root = Path(tempfile.gettempdir()) / "popper-revision-worktrees" / namespace
        self.manager = RevisionWorktreeManager(
            registry, worktree_root, self.project, self.revisions)
        original_git = self.manager._git

        def interrupted_git(project, *args):
            if args[:2] == ("worktree", "add"):
                raise SystemExit("simulated process exit")
            return original_git(project, *args)

        with patch.object(self.manager, "_git", side_effect=interrupted_git):
            with self.assertRaises(SystemExit):
                self.manager.create(self.revision["revision_id"], "startup-recovery-0001")
        worktree_id = self.manager.list()[0]["worktree_id"]
        self.manager.close()
        server = Workstation(("127.0.0.1", 0), self.project,
                             trusted_local=True, research_runs=(self.root,))
        try:
            self.manager = RevisionWorktreeManager(
                registry, worktree_root, self.project, self.revisions)
            self.assertEqual("failed", self.manager.get(worktree_id)["status"])
            self.assertTrue(self.manager.verify()["ok"])
        finally:
            server.server_close()

    def test_unversioned_revision_is_rejected_without_lifecycle_record(self):
        other = self.root / "unversioned"
        other.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, other / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](other)
        initialize(other)
        store = RevisionStore(self.root / "unversioned-revisions")
        source = other / "model.py"
        revision = store.create(
            other, "H2", "D2",
            [CodeEdit("model.py", source.read_text(encoding="utf-8") + "\n# edit\n",
                      file_hash(source))], "test-policy")
        manager = RevisionWorktreeManager(
            self.root / "unversioned.sqlite", self.root / "other-worktrees", other, store)
        try:
            with self.assertRaisesRegex(ProtocolError, "Git commit"):
                manager.create(revision["revision_id"])
            self.assertEqual([], manager.list())
        finally:
            manager.close()

    def test_concurrent_idempotent_create_provisions_only_one_worktree(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def create():
            manager = RevisionWorktreeManager(
                self.registry, self.worktree_root, self.project, self.revisions)
            try:
                barrier.wait(timeout=5)
                results.append(manager.create(
                    self.revision["revision_id"], "concurrent-worktree-0001"))
            except Exception as error:
                errors.append(error)
            finally:
                manager.close()

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(2, len(results))
        self.assertEqual(1, len({row["worktree_id"] for row in results}))
        self.assertEqual(1, len(self.manager.list()))

    def test_frozen_proposal_requires_digest_approval_and_creates_successor(self):
        record = self.manager.create(self.revision["revision_id"], "proposal-worktree-0001")
        candidate = Path(record["path"]) / "model.py"
        approved_text = candidate.read_text(encoding="utf-8") + "\n# approved change\n"
        candidate.write_text(approved_text, encoding="utf-8")
        proposed = self.manager.propose(record["worktree_id"], "local-user")
        self.assertEqual("review_required", proposed["status"])
        frozen = json.loads(proposed["proposal"])
        self.assertEqual(self.revision["revision_id"], frozen["parent_revision_id"])
        self.assertEqual(["model.py"], [item["path"] for item in frozen["edits"]])

        # Later worktree edits cannot be smuggled into the already frozen proposal.
        candidate.write_text("raise RuntimeError('not approved')\n", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "审批摘要"):
            self.manager.submit(record["worktree_id"], "0" * 64, "local-user")
        submitted = self.manager.submit(
            record["worktree_id"], proposed["proposal_sha256"], "local-user")
        self.assertEqual("submitted", submitted["status"])
        successor = self.revisions.verify(submitted["successor_revision_id"])
        self.assertEqual(self.revision["revision_id"],
                         successor["identity"]["parent_revision_id"])
        successor_text = (self.revisions.path(successor["revision_id"])
                          / "code/model.py").read_text(encoding="utf-8")
        self.assertEqual(approved_text, successor_text)
        self.assertEqual(submitted, self.manager.submit(
            record["worktree_id"], proposed["proposal_sha256"], "local-user"))
        executing = self.manager.begin_execution(record["worktree_id"])
        self.assertEqual("executing", executing["status"])
        self.assertEqual(executing, self.manager.begin_execution(record["worktree_id"]))
        executed = self.manager.finish_execution(
            record["worktree_id"], run_id="RUN-approved-001")
        self.assertEqual("executed", executed["status"])
        self.assertEqual("RUN-approved-001", executed["execution_run_id"])
        self.assertEqual(
            ["provisioning", "activated", "proposal_frozen", "submitted",
             "execution_started", "executed"],
            [event["event_type"] for event in
             self.manager.history(record["worktree_id"])])
        self.assertTrue(self.manager.verify()["ok"])

    def test_proposal_rejects_non_code_changes(self):
        record = self.manager.create(self.revision["revision_id"], "invalid-worktree-0001")
        (Path(record["path"]) / "notes.txt").write_text("not code", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "只能修改"):
            self.manager.propose(record["worktree_id"], "local-user")
        self.assertEqual("active", self.manager.get(record["worktree_id"])["status"])

    def test_event_tampering_is_detected(self):
        record = self.manager.create(self.revision["revision_id"], "tamper-worktree-0001")
        self.assertTrue(self.manager.verify()["ok"])
        original_payload = self.manager._conn.execute(
            "SELECT payload FROM worktree_events WHERE worktree_id=? AND event_type='activated'",
            (record["worktree_id"],)).fetchone()[0]
        with self.manager._conn:
            self.manager._conn.execute(
                "UPDATE worktree_events SET payload='{}' WHERE worktree_id=? AND event_type='activated'",
                (record["worktree_id"],))
        try:
            check = self.manager.verify()
            self.assertFalse(check["ok"])
            self.assertIn("事件链", check["reason"])
            with self.assertRaisesRegex(ProtocolError, "完整性失败"):
                self.manager.remove(record["worktree_id"])
            with self.assertRaisesRegex(ProtocolError, "完整性失败"):
                self.manager.create(self.revision["revision_id"], "tamper-create-0001")
            self.assertEqual(1, len(self.manager.list()))
        finally:
            with self.manager._conn.transaction(write=True):
                self.manager._conn.execute(
                    "UPDATE worktree_events SET payload=? WHERE worktree_id=? "
                    "AND event_type='activated'",
                    (original_payload, record["worktree_id"]))

    def test_snapshot_identity_tampering_is_detected_without_changing_status(self):
        record = self.manager.create(self.revision["revision_id"], "identity-tamper-0001")
        replacements = {
            "git_commit": "0" * 40,
            "revision_id": "REV-" + "0" * 24,
            "idempotency_key": "identity-tamper-other",
            "path": str(self.root / "wrong-path"),
        }
        for field, replacement in replacements.items():
            with self.manager._conn.transaction(write=True):
                self.manager._conn.execute(
                    f"UPDATE revision_worktrees SET {field}=? WHERE worktree_id=?",
                    (replacement, record["worktree_id"]))
            try:
                with self.subTest(field=field):
                    check = self.manager.verify()
                    self.assertFalse(check["ok"])
                    self.assertIn("快照", check["reason"])
                    with self.assertRaisesRegex(ProtocolError, "完整性失败"):
                        self.manager.remove(record["worktree_id"])
                    self.assertTrue(Path(record["path"]).is_dir())
            finally:
                with self.manager._conn.transaction(write=True):
                    self.manager._conn.execute(
                        f"UPDATE revision_worktrees SET {field}=? WHERE worktree_id=?",
                        (record[field], record["worktree_id"]))

    def test_frozen_proposal_body_tampering_is_detected(self):
        record = self.manager.create(self.revision["revision_id"], "proposal-tamper-0001")
        candidate = Path(record["path"]) / "model.py"
        candidate.write_text(candidate.read_text(encoding="utf-8") + "\n# edit\n",
                             encoding="utf-8")
        proposed = self.manager.propose(record["worktree_id"], "local-user")
        with self.manager._conn.transaction(write=True):
            self.manager._conn.execute(
                "UPDATE revision_worktrees SET proposal=? WHERE worktree_id=?",
                ('{"edits":[]}', record["worktree_id"]))
        try:
            check = self.manager.verify()
            self.assertFalse(check["ok"])
            self.assertIn("摘要", check["reason"])
            with self.assertRaisesRegex(ProtocolError, "完整性失败"):
                self.manager.submit(record["worktree_id"],
                                    proposed["proposal_sha256"], "local-user")
            self.assertEqual("review_required",
                             self.manager.get(record["worktree_id"])["status"])
        finally:
            with self.manager._conn.transaction(write=True):
                self.manager._conn.execute(
                    "UPDATE revision_worktrees SET proposal=? WHERE worktree_id=?",
                    (proposed["proposal"], record["worktree_id"]))
            self.manager.remove(record["worktree_id"])

    def test_legacy_snapshot_is_migrated_to_explicit_replayable_events(self):
        legacy_db = self.root / "legacy-worktrees.sqlite"
        conn = sqlite3.connect(legacy_db)
        with conn:
            conn.execute("""
                CREATE TABLE revision_worktrees(
                    worktree_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    revision_id TEXT NOT NULL,
                    git_commit TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created TEXT NOT NULL,
                    removed TEXT,
                    error TEXT)
            """)
            conn.execute(
                "INSERT INTO revision_worktrees VALUES(?,?,?,?,?,?,?,?,?)",
                ("wkt_" + "1" * 24, "legacy-key-0001", self.revision["revision_id"],
                 self.revision["identity"]["source_control"]["head_commit"],
                 str(self.root / "legacy-path"), "active",
                 "2026-01-01T00:00:00+00:00", None, None))
        conn.close()
        manager = RevisionWorktreeManager(
            legacy_db, self.root / "legacy-isolated", self.project, self.revisions)
        try:
            events = manager.history("wkt_" + "1" * 24)
            self.assertEqual(["provisioning", "activated"],
                             [event["event_type"] for event in events])
            self.assertTrue(all(event["payload"]["migrated"] for event in events))
            self.assertTrue(manager.verify()["ok"])
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
