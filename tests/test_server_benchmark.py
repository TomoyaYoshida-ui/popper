"""Workbench benchmark jobs are durable, idempotent and evidence-checked."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from popper.demo import initialize_demo
from popper.research.benchmark import run_cross_domain_benchmark
from popper.research.benchmark_jobs import BenchmarkJobStore
from popper.server import Workstation


class BenchmarkJobStoreTests(unittest.TestCase):
    def test_event_projection_and_duplicate_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "jobs.sqlite"
            output = Path(temporary) / "new-benchmark"
            store = BenchmarkJobStore(db)
            try:
                first, created = store.enqueue("request-abcdefgh", output)
                self.assertTrue(created)
                replayed, created = store.enqueue("request-abcdefgh", output)
                self.assertFalse(created)
                self.assertEqual(first["job_id"], replayed["job_id"])
                self.assertEqual("running", store.claim(first["job_id"])["status"])
                self.assertEqual("succeeded", store.finish(first["job_id"])["status"])
                self.assertTrue(store.verify()["ok"])
                with store._conn.transaction(write=True):
                    store._conn.execute(
                        "UPDATE benchmark_jobs SET status='failed' WHERE job_id=?",
                        (first["job_id"],))
                self.assertFalse(store.verify()["ok"])
            finally:
                store.close()


class BenchmarkHttpTests(unittest.TestCase):
    def _start(self, project, trusted):
        server = Workstation(("127.0.0.1", 0), project, trusted_local=trusted)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/session", timeout=5) as response:
            token = json.load(response)["token"]
        return server, thread, base, token

    @staticmethod
    def _request(url, token=None, payload=None):
        headers = {"X-Popper-Token": token} if token else {}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        with urllib.request.urlopen(urllib.request.Request(
                url, data=data, headers=headers), timeout=30) as response:
            return response.status, json.load(response)

    def test_create_verify_and_read_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "workspace"
            initialize_demo(project, "ode-convergence")
            server, thread, base, token = self._start(project, True)
            try:
                url = base + "/api/v1/benchmarks"
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    self._request(url)
                self.assertEqual(403, denied.exception.code)
                payload = {"schema_version": "1.0", "run_name": "comparison-a",
                           "idempotency_key": "benchmark-request-001"}
                code, body = self._request(url, token, payload)
                self.assertEqual(202, code)
                job = body["data"]
                self.assertIn(job["status"], {"queued", "running"})
                self.assertEqual(0, server.wait_for_jobs(timeout=90))
                _, detail = self._request(url + "/" + job["job_id"], token)
                self.assertEqual("succeeded", detail["data"]["job"]["status"])
                self.assertTrue(detail["data"]["verification"]["ok"])
                self.assertEqual(2, len(detail["data"]["report"]["cases"]))
                _, replayed = self._request(url, token, payload)
                self.assertEqual(job["job_id"], replayed["data"]["job_id"])
                _, listed = self._request(url, token)
                self.assertTrue(listed["data"]["integrity"]["ok"])
                self.assertEqual(1, len(listed["data"]["jobs"]))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
            server, thread, base, token = self._start(project, False)
            try:
                _, listed = self._request(base + "/api/v1/benchmarks", token)
                self.assertEqual("succeeded", listed["data"]["jobs"][0]["status"])
                with self.assertRaises(urllib.error.HTTPError) as forbidden:
                    self._request(base + "/api/v1/benchmarks", token, payload)
                self.assertEqual(403, forbidden.exception.code)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_untrusted_post_returns_complete_403_with_request_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "workspace"
            initialize_demo(project, "ode-convergence")
            server, thread, base, token = self._start(project, False)
            try:
                payload = json.dumps({
                    "schema_version": "1.0", "run_name": "denied",
                    "idempotency_key": "benchmark-denied-001",
                }).encode()
                for _ in range(20):
                    request = urllib.request.Request(
                        base + "/api/v1/benchmarks", data=payload,
                        headers={"X-Popper-Token": token,
                                 "Content-Type": "application/json"},
                        method="POST")
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(403, denied.exception.code)
                    body = json.load(denied.exception)
                    self.assertEqual("permission_denied", body["error"]["code"])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_restart_interrupts_unproven_job_but_reconciles_verified_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "workspace"
            initialize_demo(project, "ode-convergence")
            db = project / ".popper" / "benchmark-jobs.sqlite"
            output = project / ".popper" / "benchmarks" / "unfinished"
            jobs = BenchmarkJobStore(db)
            try:
                unfinished, _ = jobs.enqueue("restart-unfinished", output)
                jobs.claim(unfinished["job_id"])
            finally:
                jobs.close()
            server = Workstation(("127.0.0.1", 0), project, trusted_local=False)
            server.server_close()
            jobs = BenchmarkJobStore(db)
            try:
                self.assertEqual("interrupted", jobs.get(unfinished["job_id"])["status"])
                self.assertTrue(jobs.verify()["ok"])
                completed_output = project / ".popper" / "benchmarks" / "completed"
                completed, _ = jobs.enqueue("restart-completed", completed_output)
                jobs.claim(completed["job_id"])
            finally:
                jobs.close()
            self.assertEqual("passed", run_cross_domain_benchmark(
                completed_output, trusted_local=True)["status"])
            server = Workstation(("127.0.0.1", 0), project, trusted_local=False)
            server.server_close()
            jobs = BenchmarkJobStore(db)
            try:
                self.assertEqual("succeeded", jobs.get(completed["job_id"])["status"])
                self.assertTrue(jobs.verify()["ok"])
                queued_output = project / ".popper" / "benchmarks" / "queued"
                queued, _ = jobs.enqueue("restart-queued-job", queued_output)
            finally:
                jobs.close()
            server = Workstation(("127.0.0.1", 0), project, trusted_local=True)
            try:
                self.assertEqual(0, server.wait_for_jobs(timeout=90))
            finally:
                server.server_close()
            jobs = BenchmarkJobStore(db)
            try:
                self.assertEqual("succeeded", jobs.get(queued["job_id"])["status"])
                self.assertTrue(jobs.verify()["ok"])
            finally:
                jobs.close()


if __name__ == "__main__":
    unittest.main()
