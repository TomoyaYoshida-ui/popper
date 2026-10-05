import tempfile
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from popper.core import ProtocolError
from popper.research.design_jobs import DesignExecutionJobStore


class DesignExecutionJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "design-jobs.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def test_enqueue_claim_finish_and_restart_are_idempotent(self):
        jobs = DesignExecutionJobStore(self.path)
        first = jobs.enqueue("D-1", "a" * 64)
        self.assertEqual(first, jobs.enqueue("D-1", "a" * 64))
        with self.assertRaisesRegex(ProtocolError, "不同冻结快照"):
            jobs.enqueue("D-1", "b" * 64)
        self.assertEqual("running", jobs.claim(first["job_id"])["status"])
        with self.assertRaisesRegex(ProtocolError, "已被领取"):
            jobs.claim(first["job_id"])
        self.assertEqual("succeeded", jobs.finish(
            first["job_id"], observation_id="OBS-1")["status"])
        self.assertTrue(jobs.verify()["ok"])
        jobs.close()
        reopened = DesignExecutionJobStore(self.path)
        try:
            self.assertEqual("OBS-1", reopened.for_design("D-1")["observation_id"])
            self.assertEqual(0, reopened.interrupt_running())
            self.assertTrue(reopened.verify()["ok"])
        finally:
            reopened.close()

    def test_restart_marks_running_interrupted_without_replay(self):
        jobs = DesignExecutionJobStore(self.path)
        running = jobs.enqueue("D-running", "c" * 64)
        queued = jobs.enqueue("D-queued", "d" * 64)
        jobs.claim(running["job_id"])
        jobs.close()
        reopened = DesignExecutionJobStore(self.path)
        try:
            self.assertEqual(1, reopened.interrupt_running())
            self.assertEqual("interrupted", reopened.get(running["job_id"])["status"])
            self.assertEqual("queued", reopened.get(queued["job_id"])["status"])
            self.assertTrue(reopened.verify()["ok"])
        finally:
            reopened.close()

    def test_snapshot_tamper_fails_integrity(self):
        jobs = DesignExecutionJobStore(self.path)
        job = jobs.enqueue("D-tamper", "e" * 64)
        jobs.close()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE design_execution_jobs SET status='succeeded' WHERE job_id=?",
                         (job["job_id"],))
        reopened = DesignExecutionJobStore(self.path)
        try:
            self.assertFalse(reopened.verify()["ok"])
        finally:
            reopened.close()

    def test_cancel_queued_prevents_claim_and_survives_restart(self):
        jobs = DesignExecutionJobStore(self.path)
        job = jobs.enqueue("D-cancel-queued", "f" * 64)
        cancelled = jobs.cancel(job["job_id"], "user requested stop")
        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual(1, cancelled["cancel_requested"])
        self.assertEqual(cancelled, jobs.cancel(job["job_id"], "user requested stop"))
        with self.assertRaises(ProtocolError):
            jobs.claim(job["job_id"])
        self.assertTrue(jobs.verify()["ok"])
        jobs.close()
        reopened = DesignExecutionJobStore(self.path)
        try:
            self.assertEqual("cancelled", reopened.get(job["job_id"])["status"])
            self.assertTrue(reopened.verify()["ok"])
        finally:
            reopened.close()

    def test_running_cancel_request_can_finish_cancelled_or_success(self):
        jobs = DesignExecutionJobStore(self.path)
        first = jobs.enqueue("D-cancel-running", "a" * 64)
        jobs.claim(first["job_id"])
        requested = jobs.cancel(first["job_id"], "stop active work")
        self.assertEqual("running", requested["status"])
        self.assertEqual(1, requested["cancel_requested"])
        self.assertEqual("cancelled", jobs.finish_cancelled(first["job_id"])["status"])
        second = jobs.enqueue("D-already-committed", "b" * 64)
        jobs.claim(second["job_id"])
        jobs.cancel(second["job_id"], "too late")
        self.assertEqual("succeeded", jobs.finish(
            second["job_id"], observation_id="OBS-committed")["status"])
        self.assertTrue(jobs.verify()["ok"])
        jobs.close()

    def test_cancelled_job_retry_has_explicit_attempt_and_idempotent_replay(self):
        jobs = DesignExecutionJobStore(self.path)
        first = jobs.enqueue("D-retry", "c" * 64)
        jobs.cancel(first["job_id"], "stop")
        retried, applied = jobs.retry_cancelled(first["job_id"], 1)
        self.assertTrue(applied)
        self.assertEqual("queued", retried["status"])
        self.assertEqual(2, retried["attempt"])
        self.assertEqual(0, retried["cancel_requested"])
        self.assertEqual((retried, False), jobs.retry_cancelled(first["job_id"], 1))
        with self.assertRaises(ProtocolError):
            jobs.retry_cancelled(first["job_id"], 2)
        self.assertEqual("running", jobs.claim(first["job_id"])["status"])
        jobs.cancel(first["job_id"], "stop again")
        jobs.finish_cancelled(first["job_id"])
        self.assertEqual(3, jobs.retry_cancelled(first["job_id"], 2)[0]["attempt"])
        self.assertTrue(jobs.verify()["ok"])
        jobs.close()

    def test_legacy_queue_schema_migrates_without_losing_event_history(self):
        jobs = DesignExecutionJobStore(self.path)
        original = jobs.enqueue("D-legacy", "d" * 64)
        jobs.close()
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE legacy_jobs(
                job_id TEXT PRIMARY KEY, design_id TEXT NOT NULL UNIQUE,
                design_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                observation_id TEXT, error TEXT, created TEXT NOT NULL,
                updated TEXT NOT NULL)""")
            conn.execute("""INSERT INTO legacy_jobs
                SELECT job_id,design_id,design_sha256,status,observation_id,
                       error,created,updated FROM design_execution_jobs""")
            conn.execute("DROP TABLE design_execution_jobs")
            conn.execute("ALTER TABLE legacy_jobs RENAME TO design_execution_jobs")
        migrated = DesignExecutionJobStore(self.path)
        try:
            self.assertEqual(1, migrated.get(original["job_id"])["attempt"])
            self.assertEqual(0, migrated.get(original["job_id"])["cancel_requested"])
            self.assertTrue(migrated.verify()["ok"])
            migrated.cancel(original["job_id"], "cancel legacy")
            self.assertEqual(2, migrated.retry_cancelled(original["job_id"], 1)[0]["attempt"])
            self.assertTrue(migrated.verify()["ok"])
        finally:
            migrated.close()

    def test_concurrent_retry_has_one_applied_transition(self):
        jobs = DesignExecutionJobStore(self.path)
        original = jobs.enqueue("D-race", "e" * 64)
        jobs.cancel(original["job_id"], "stop")
        jobs.close()
        def retry():
            contender = DesignExecutionJobStore(self.path)
            try:
                return contender.retry_cancelled(original["job_id"], 1)
            finally:
                contender.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: retry(), range(2)))
        self.assertEqual([False, True], sorted(applied for _, applied in outcomes))
        reopened = DesignExecutionJobStore(self.path)
        try:
            self.assertEqual(2, reopened.get(original["job_id"])["attempt"])
            self.assertTrue(reopened.verify()["ok"])
        finally:
            reopened.close()
