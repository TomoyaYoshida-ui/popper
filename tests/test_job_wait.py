import threading
import unittest
from types import SimpleNamespace
import popper.server
from popper.services.research import ResearchService


class JobWaitTests(unittest.TestCase):
    def test_wait_includes_jobs_spawned_during_join(self):
        host=SimpleNamespace(job_threads_lock=threading.Lock(),_job_threads=[])
        service=ResearchService(host)
        class Job:
            def __init__(self,on_join=lambda: None): self.alive=True; self.on_join=on_join
            def join(self,timeout): self.on_join(); self.alive=False
            def is_alive(self): return self.alive
        child=Job()
        parent=Job(lambda: host._job_threads.append(child))
        host._job_threads.append(parent)
        self.assertEqual(0,service.wait_for_jobs(timeout=2))
        self.assertFalse(child.is_alive())
        self.assertEqual([],host._job_threads)

    def test_timeout_retains_live_job_for_next_wait(self):
        host=SimpleNamespace(job_threads_lock=threading.Lock(),_job_threads=[])
        service=ResearchService(host); release=threading.Event()
        service._start_job(lambda: release.wait(2))
        try: self.assertEqual(1,service.wait_for_jobs(timeout=0))
        finally: release.set()
        self.assertEqual(0,service.wait_for_jobs(timeout=2))
