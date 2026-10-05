import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.workstation_lease import WorkstationLease


class WorkstationLeaseTests(unittest.TestCase):
    def test_same_process_and_other_process_cannot_take_live_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = WorkstationLease(root)
            try:
                with self.assertRaisesRegex(ProtocolError, "已有运行中"):
                    WorkstationLease(root)
                probe = subprocess.run(
                    [sys.executable, "-c",
                     "import sys; from popper.workstation_lease import WorkstationLease; "
                     "WorkstationLease(sys.argv[1])", str(root)],
                    capture_output=True, text=True, encoding="utf-8", timeout=10,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                self.assertNotEqual(0, probe.returncode)
                self.assertIn("已有运行中", probe.stderr)
            finally:
                lease.close()
            reopened = WorkstationLease(root)
            reopened.close()
