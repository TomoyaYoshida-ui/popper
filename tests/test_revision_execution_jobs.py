import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from popper.research.execution_jobs import RevisionExecutionJobStore


class RevisionExecutionJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "jobs.sqlite"
        self.store = RevisionExecutionJobStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_persistent_progress_and_audit_chain(self):
        job = self.store.enqueue("wkt_" + "1" * 24, "REV-" + "2" * 24)
        self.assertEqual("queued", job["status"])
        self.assertEqual(job["job_id"], self.store.enqueue(
            "wkt_" + "1" * 24, "REV-" + "2" * 24)["job_id"])
        self.store.claim(job["job_id"])
        self.store.progress(job["job_id"], "executing_revision", 35)
        finished = self.store.finish(job["job_id"], run_id="RUN-" + "3" * 24)
        self.assertEqual("succeeded", finished["status"])
        self.assertEqual(100, finished["progress"])
        self.assertEqual(["queued", "started", "progress", "succeeded"],
                         [event["event_type"] for event in
                          self.store.history(job["job_id"])])
        self.assertTrue(self.store.verify()["ok"])

    def test_cancel_queued_and_recover_running(self):
        cancelled = self.store.enqueue("wkt_" + "4" * 24, "REV-" + "5" * 24)
        cancelled = self.store.cancel(cancelled["job_id"], "user request")
        self.assertEqual("cancelled", cancelled["status"])
        self.assertTrue(cancelled["cancel_requested"])

        running = self.store.enqueue("wkt_" + "6" * 24, "REV-" + "7" * 24)
        self.store.claim(running["job_id"])
        requested = self.store.cancel(running["job_id"], "stop running")
        self.assertEqual("running", requested["status"])
        self.assertTrue(requested["cancel_requested"])
        stopped = self.store.finish_cancelled(running["job_id"], "stop running")
        self.assertEqual("cancelled", stopped["status"])

        running = self.store.enqueue("wkt_" + "a" * 24, "REV-" + "b" * 24)
        self.store.claim(running["job_id"])
        self.assertEqual(1, self.store.interrupt_running())
        recovered = self.store.get(running["job_id"])
        self.assertEqual("interrupted", recovered["status"])
        self.assertIn("restarted", recovered["error"])

        self.store.close()
        self.store = RevisionExecutionJobStore(self.path)
        self.assertEqual("interrupted", self.store.get(running["job_id"])["status"])
        self.assertTrue(self.store.verify()["ok"])

    def test_tamper_is_detected(self):
        job = self.store.enqueue("wkt_" + "8" * 24, "REV-" + "9" * 24)
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE revision_execution_job_events SET payload='{}' WHERE job_id=?",
                (job["job_id"],))
        self.assertFalse(self.store.verify()["ok"])

    def test_concurrent_enqueue_has_one_durable_identity(self):
        worktree_id = "wkt_" + "c" * 24
        revision_id = "REV-" + "d" * 24

        def enqueue():
            store = RevisionExecutionJobStore(self.path)
            try:
                return store.enqueue(worktree_id, revision_id)["job_id"]
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            job_ids = list(pool.map(lambda _: enqueue(), range(12)))
        self.assertEqual(1, len(set(job_ids)))
        self.assertEqual(1, len(self.store.list()))
        self.assertTrue(self.store.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
