import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import read_json, write_json
from popper.research.workers import LocalWorker
from popper.research.workers import json_io


class WorkerMetadataReadTests(unittest.TestCase):
    def test_transient_windows_denial_rereads_without_launching_or_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = LocalWorker(root / "jobs", root / "revisions")
            path = worker.root / "JOB-test" / "job.json"
            write_json(path, {"job_id": "JOB-test", "status": "running"})
            before = path.read_bytes()
            attempts = []
            def occasionally_denied(target):
                attempts.append(target)
                if len(attempts) < 3:
                    raise PermissionError(13, "synthetic sharing violation")
                return read_json(target)
            with patch.object(json_io, "_WINDOWS", True), patch.object(json_io, "_read_json", side_effect=occasionally_denied), patch.object(json_io.time, "sleep") as sleep, patch.object(worker, "_spawn_supervisor") as launch:
                self.assertEqual("running", worker.poll("JOB-test")["status"])
                self.assertEqual(3, len(attempts))
                self.assertEqual(2, sleep.call_count)
                launch.assert_not_called()
            self.assertEqual(before, path.read_bytes())

    def test_persistent_denial_is_bounded_and_propagates_original_error(self):
        denied = PermissionError(13, "persistent synthetic denial")
        with patch.object(json_io, "_WINDOWS", True), patch.object(json_io, "_read_json", side_effect=denied) as read, patch.object(json_io.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as error:
                json_io.read_job_json(Path("unused"))
            self.assertIs(denied, error.exception)
            self.assertEqual(len(json_io._SHARING_DELAYS), read.call_count)
            self.assertEqual(len(json_io._SHARING_DELAYS) - 1, sleep.call_count)

    def test_nonwindows_permissions_and_malformed_json_are_not_hidden(self):
        for windows, reason in ((False, PermissionError(13, "denied")), (True, json.JSONDecodeError("invalid", "x", 0)), (True, FileNotFoundError("missing"))):
            with self.subTest(windows=windows, kind=type(reason).__name__), patch.object(json_io, "_WINDOWS", windows), patch.object(json_io, "_read_json", side_effect=reason) as read, patch.object(json_io.time, "sleep") as sleep:
                with self.assertRaises(type(reason)):
                    json_io.read_job_json(Path("unused"))
                self.assertEqual(1, read.call_count)
                sleep.assert_not_called()
