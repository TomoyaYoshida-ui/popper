import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from popper.core import ProtocolError, read_json, write_json
from popper.research import agent_revisions
from popper.research.readout import read_research_state
from popper.research.revision_drafts import preview_initial_revision, submit_initial_revision
from popper.research.revisions import RevisionStore
from popper.research.execution_jobs import RevisionExecutionJobStore
from popper.sessions import ToolCallConflict
from popper.workspace_files import ProjectFiles
from tests import test_agent_revision_tools as _fixtures


class RevisionReferenceTests(unittest.TestCase):
    def setUp(self):
        self.f = _fixtures.AgentRevisionToolsTests()
        self.f.setUp()
        self.ws, self.project, self.run, self.args = self.f.ws, self.f.project, self.f.run, self.f.args

    def tearDown(self):
        self.f.tearDown()

    def commit(self):
        preview = self.f.approve_execute(self.f.propose("research.revision_preview", self.args))
        call = self.f.propose("research.revision_commit", {"study_id": self.args["study_id"], "preview_call_id": preview["call_id"]})
        self.assertTrue(self.ws._agent_tool_public(call)["preview"]["ok"])
        with self.assertRaises(ToolCallConflict):
            self.ws.execute_agent_tool(call["call_id"])
        created = self.f.approve_execute(call)
        self.assertEqual("completed", created["status"], created.get("error"))
        return preview, call, created

    def test_reference_chain_has_three_independent_approvals_and_no_frozen_writes(self):
        original = ProjectFiles(self.project).read("model.py")["content"]
        preview, call, created = self.commit()
        self.assertEqual(preview["call_id"], created["result"]["preview_call_id"])
        self.assertEqual(call["call_id"], created["result"]["create_call_id"])
        self.assertEqual(read_research_state(self.run)["state_version"], created["result"]["state_version"])
        execution = self.f.propose("research.revision_run", {
            "study_id": self.args["study_id"], "create_call_id": call["call_id"]})
        self.assertTrue(self.ws._agent_tool_public(execution)["preview"]["ok"])
        with self.assertRaises(ToolCallConflict):
            self.ws.execute_agent_tool(execution["call_id"])
        with patch.object(self.ws, "_start_revision_job"):
            queued = self.f.approve_execute(execution)
        self.assertEqual("completed", queued["status"], queued.get("error"))
        self.assertEqual("enqueued_not_completed", queued["result"]["status"])
        self.assertEqual("queued", queued["result"]["job"]["status"])
        self.assertEqual(original, ProjectFiles(self.project).read("model.py")["content"])
        self.assertEqual([], read_research_state(self.run)["observations"])

    def test_same_reference_across_calls_is_persistently_idempotent(self):
        preview, first, created = self.commit()
        duplicate = self.f.propose("research.revision_commit", first["arguments"])
        self.assertTrue(self.ws._agent_tool_public(duplicate)["preview"]["replayed"])
        second = self.f.approve_execute(duplicate)
        self.assertEqual(created["result"], second["result"])
        self.assertEqual(1, len(list((self.run / "revisions").glob("REV-*"))))
        execution = self.f.propose("research.revision_run", {
            "study_id": self.args["study_id"], "create_call_id": first["call_id"]})
        with patch.object(self.ws, "_start_revision_job"):
            queued = self.f.approve_execute(execution)
        duplicate_run = self.f.propose("research.revision_run", execution["arguments"])
        with patch.object(self.ws, "execute_agent_revision") as no_run:
            repeated = self.f.approve_execute(duplicate_run)
        no_run.assert_not_called()
        self.assertEqual(queued["result"], repeated["result"])
        self.assertEqual(queued["result"], agent_revisions.reconcile(self.ws, execution, self.project))

    def test_incomplete_or_cross_session_source_cannot_be_referenced(self):
        unfinished = self.f.propose("research.revision_preview", self.args)
        reference = self.f.propose("research.revision_commit", {
            "study_id": self.args["study_id"], "preview_call_id": unfinished["call_id"]})
        self.assertFalse(self.ws._agent_tool_public(reference)["preview"]["ok"])
        completed = self.f.approve_execute(unfinished)
        old_session = self.f.session
        self.f.session = self.ws.create_agent_session("unrelated session")
        try:
            cross = self.f.propose("research.revision_commit", {
                "study_id": self.args["study_id"], "preview_call_id": completed["call_id"]})
            failed = self.f.approve_execute(cross)
            self.assertEqual("failed", failed["status"])
            self.assertIn("same project/session/Study", failed["error"]["message"])
        finally:
            self.f.session = old_session
        self.assertEqual([], list((self.run / "revisions").glob("REV-*")))

    def test_cross_study_and_missing_native_context_are_rejected(self):
        preview = self.f.approve_execute(self.f.propose("research.revision_preview", self.args))
        reference = self.f.propose("research.revision_commit", {
            "study_id": "S-other", "preview_call_id": preview["call_id"]})
        self.assertFalse(self.ws._agent_tool_public(reference)["preview"]["ok"])
        with self.assertRaisesRegex(ProtocolError, "native call/session"):
            agent_revisions.preview(self.ws, "research.revision_commit", {
                "study_id": self.args["study_id"], "preview_call_id": preview["call_id"]}, self.project)

    def test_precise_edits_preserve_original_and_conversion_receipts(self):
        original = ProjectFiles(self.project).read("model.py")["content"]
        spec = {key: value for key, value in self.args.items() if key != "replacement"}
        spec["edits"] = [{"search": "def main", "replace": "# Exact patch\ndef main"}]
        self.args = self.f.args = spec
        preview, _, created = self.commit()
        receipt = read_json(agent_revisions._receipt_path(self.run, preview["call_id"]))
        self.assertEqual(spec["edits"], receipt["specification"]["edits"])
        self.assertNotIn("replacement", receipt["specification"])
        self.assertEqual(original.replace("def main", "# Exact patch\ndef main"), receipt["resolved_specification"]["replacement"])
        self.assertEqual("search_replace", preview["result"]["edit_mode"])
        revision = RevisionStore(self.run / "revisions").verify(created["result"]["revision_id"])
        self.assertEqual("agent", revision["identity"]["actor"])
        self.assertEqual(original, ProjectFiles(self.project).read("model.py")["content"])

    def test_replacement_and_edits_are_mutually_exclusive(self):
        with self.assertRaises(ProtocolError):
            preview_initial_revision(self.run, self.project, {**self.args, "edits": [{"search": "x", "replace": "y"}]})
        with self.assertRaises(ProtocolError):
            preview_initial_revision(self.run, self.project, {key: value for key, value in self.args.items() if key != "replacement"})

    def test_changed_version_or_preview_digest_cannot_be_committed(self):
        preview = self.f.approve_execute(self.f.propose("research.revision_preview", self.args))
        reference = self.f.propose("research.revision_commit", {
            "study_id": self.args["study_id"], "preview_call_id": preview["call_id"]})
        changed = {**read_research_state(self.run), "state_version": self.args["expected_state_version"] + 1}
        with patch("popper.research.revision_drafts.read_research_state", return_value=changed):
            self.assertFalse(self.ws._agent_tool_public(reference)["preview"]["ok"])
            failed = self.f.approve_execute(reference)
        self.assertEqual("failed", failed["status"])
        self.assertIn("版本已变化", failed["error"]["message"])
        self.assertEqual([], list((self.run / "revisions").glob("REV-*")))

    def test_run_does_not_adopt_new_study_version(self):
        _, created_call, _ = self.commit()
        execution = self.f.propose("research.revision_run", {
            "study_id": self.args["study_id"], "create_call_id": created_call["call_id"]})
        changed = {**read_research_state(self.run), "state_version": self.args["expected_state_version"] + 1}
        with patch("popper.research.agent_revisions.read_research_state", return_value=changed):
            self.assertFalse(self.ws._agent_tool_public(execution)["preview"]["ok"])
            with patch.object(self.ws, "execute_agent_revision") as no_run:
                failed = self.f.approve_execute(execution)
        no_run.assert_not_called()
        self.assertEqual("failed", failed["status"])
        self.assertIn("version", failed["error"]["message"])

    def test_prepared_create_reconciles_immutable_revision_after_restart_without_create(self):
        preview, call, created = self.commit()
        path = agent_revisions._reference_receipt_path(self.run, call)
        receipt = read_json(path)
        write_json(path, {key: value for key, value in {**receipt, "status": "prepared"}.items() if key != "result"})
        with patch("popper.research.agent_revisions.submit_initial_revision") as no_create:
            recovered = agent_revisions.reconcile(self.ws, call, self.project)
        no_create.assert_not_called()
        self.assertEqual(created["result"], recovered)
        self.assertEqual("completed", read_json(path)["status"])
        self.assertEqual(1, len(list((self.run / "revisions").glob("REV-*"))))

    def test_reference_atomic_write_fits_real_windows_path_without_shortening_digest(self):
        call = {"tool_name": "research.revision_commit", "session_id": "session",
                "arguments": {"study_id": "study", "preview_call_id": "preview"}}
        with TemporaryDirectory() as directory:
            base = Path(directory)
            # The production receipt parent is 167 characters; the old name's
            # atomic temporary suffix made a 260-character path.
            run = base / ("x" * (142 - len(str(base))))
            path = agent_revisions._reference_receipt_path(run, call)
            self.assertEqual(64, len(path.stem))
            self.assertEqual(250, len(str(path)) + 13)
            write_json(path, {"identity": call})
            self.assertEqual(call, read_json(path)["identity"])

    def test_legacy_prepared_reference_is_read_and_reconciled_to_compact_path(self):
        _, call, created = self.commit()
        path = agent_revisions._reference_receipt_path(self.run, call)
        receipt = read_json(path)
        legacy = path.with_name("reference-" + path.name)
        path.rename(legacy)
        write_json(legacy, {key: value for key, value in
                           {**receipt, "status": "prepared"}.items() if key != "result"})
        original = legacy.read_bytes()
        with patch("popper.research.agent_revisions.submit_initial_revision") as no_create:
            recovered = agent_revisions.reconcile(self.ws, call, self.project)
        no_create.assert_not_called()
        self.assertEqual(created["result"], recovered)
        self.assertEqual(original, legacy.read_bytes())
        self.assertEqual("completed", read_json(path)["status"])

    def test_reference_receipt_identity_conflict_cannot_be_replayed(self):
        _, call, _ = self.commit()
        path = agent_revisions._reference_receipt_path(self.run, call)
        receipt = read_json(path)
        write_json(path, {**receipt, "session_id": "another-session"})
        with self.assertRaisesRegex(ProtocolError, "receipt conflict"):
            agent_revisions.reconcile(self.ws, call, self.project)

    def test_prepared_run_reconciles_actual_queue_and_does_not_repeat_execution(self):
        _, created_call, _ = self.commit()
        execution = self.f.propose("research.revision_run", {
            "study_id": self.args["study_id"], "create_call_id": created_call["call_id"]})
        with patch.object(self.ws, "_start_revision_job"):
            queued = self.f.approve_execute(execution)
        path = agent_revisions._reference_receipt_path(self.run, execution)
        receipt = read_json(path)
        write_json(path, {key: value for key, value in {**receipt, "status": "prepared"}.items() if key != "result"})
        # Preview checks the actual existing queue but must not rewrite an intent,
        # create a database, dispatch a Job, or silently adopt a current version.
        before = path.read_bytes()
        with patch.object(self.ws, "execute_agent_revision") as no_run:
            projected = agent_revisions.preview(self.ws, execution["tool_name"], execution["arguments"], self.project, call=execution)
            self.assertTrue(projected["replayed"])
            self.assertEqual(before, path.read_bytes())
            recovered = agent_revisions.reconcile(self.ws, execution, self.project)
        no_run.assert_not_called()
        self.assertEqual(queued["result"], recovered)
        self.assertEqual("completed", read_json(path)["status"])
        jobs = RevisionExecutionJobStore.readonly(self.run / "revision-execution-jobs.sqlite")
        try:
            self.assertEqual(1, len(jobs.list()))
            self.assertTrue(jobs.verify()["ok"])
        finally:
            jobs.close()

    def test_readonly_queue_missing_does_not_create_file_and_forbids_writes(self):
        path = self.run / "not-created.sqlite"
        with self.assertRaises(FileNotFoundError):
            RevisionExecutionJobStore.readonly(path)
        self.assertFalse(path.exists())
        mutable = RevisionExecutionJobStore(path)
        try:
            mutable.enqueue("synthetic-preview", "REV-synthetic")
        finally:
            mutable.close()
        readonly = RevisionExecutionJobStore.readonly(path)
        try:
            self.assertTrue(readonly.verify()["ok"])
            self.assertEqual("REV-synthetic", readonly.for_worktree("synthetic-preview")["revision_id"])
            with self.assertRaises(Exception):
                readonly.enqueue("cannot-create", "REV-other")
        finally:
            readonly.close()


if __name__ == "__main__":
    unittest.main()
