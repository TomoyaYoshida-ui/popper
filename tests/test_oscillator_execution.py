import os
import tempfile
import unittest
from pathlib import Path

from popper import sandbox
from popper.core import ProtocolError, read_json
from popper.research.oscillator_execution import execute_metered, probe_read_isolation
from tests.test_oscillator_broker import row


@unittest.skipUnless(os.name == "nt", "Windows metered execution")
class MeteredExecutionTests(unittest.TestCase):
    def setUp(self):
        if not sandbox.available() or not sandbox.seal_read_available():
            self.skipTest("OS sandbox/read sealing unavailable")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reference = self.root / "private-reference.json"
        self.reference.write_text('{"answer":"not-for-candidate"}', encoding="utf-8")
        self.scorer = Path(__file__).resolve().parents[1] / "popper" / "domains" / "oscillator_trajectory.py"
        self.candidate = self.root / "model.py"

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_reference_and_scoring_source_denied(self):
        result = probe_read_isolation([self.reference, self.scorer])
        self.assertTrue(result["passed"])
        self.assertTrue(result["host_read_verified"])
        self.assertEqual(2, result["protected_count"])
        self.assertIn("not-for-candidate", self.reference.read_text())

    def test_real_solver_receipt_owned_by_host(self):
        self.candidate.write_text(
            "def solve(rhs,y0,times,budget,config):\n"
            " y=list(y0); states=[list(y)]\n"
            " for a,b in zip(times,times[1:]):\n"
            "  dy=rhs(a,y); y=[x+(b-a)*v for x,v in zip(y,dy)]; states.append(list(y))\n"
            " return states\n", encoding="utf-8")
        receipt = execute_metered(self.candidate, [row()], {}, self.root / "execution",
                                  job_id="JOB-test", protected_files=[self.reference, self.scorer], timeout=10)
        self.assertEqual(2, receipt["cases"][0]["query_count"])
        self.assertIsNone(receipt["error"])
        self.assertEqual("succeeded", receipt["cases"][0]["status"])
        self.assertEqual(receipt, read_json(self.root / "execution" / "receipt.json"))
        self.assertFalse(receipt["network_block"] and not sandbox.netblock_available())

    def test_over_budget_cannot_submit_a_forged_success(self):
        self.candidate.write_text(
            "def solve(rhs,y0,times,budget,config):\n"
            " for i in range(budget+1): rhs(0,y0)\n"
            " return [y0]*len(times)\n", encoding="utf-8")
        receipt = execute_metered(self.candidate, [row()], {}, self.root / "execution",
                                  job_id="JOB-test", protected_files=[self.reference, self.scorer], timeout=10)
        self.assertEqual(256, receipt["cases"][0]["query_count"])
        self.assertIsNone(receipt["payload"]["trajectories"][0]["states"])
        self.assertIsNotNone(receipt["error"])

    def test_existing_output_not_overwritten(self):
        self.candidate.write_text("def solve(*a): return []", encoding="utf-8")
        with self.assertRaises(ProtocolError):
            execute_metered(self.candidate, [row()], {}, self.root,
                            job_id="job", protected_files=[self.reference], timeout=10)

    def test_cross_platform_absolute_and_traversal_code_names_rejected(self):
        self.candidate.write_text("def solve(*a): return []", encoding="utf-8")
        for index, name in enumerate(("../escape.py", "C:escape.py", "C:/escape.py", "/escape.py", "pkg\\escape.py")):
            with self.subTest(name=name), self.assertRaises(ProtocolError):
                execute_metered(self.candidate, [row()], {}, self.root / f"execution-{index}",
                    job_id="job", protected_files=[self.reference], timeout=10,
                    code_files={"model.py": self.candidate, name: self.candidate})


if __name__ == "__main__":
    unittest.main()
