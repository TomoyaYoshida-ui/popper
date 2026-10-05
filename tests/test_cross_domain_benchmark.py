"""Offline benchmark reports valid and null results without inventing Claims."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, read_json, write_json
from popper.research.benchmark import (run_cross_domain_benchmark,
                                       verify_cross_domain_benchmark)
from popper.research.controller import ResearchController


class CrossDomainBenchmarkTests(unittest.TestCase):
    def test_both_non_ml_cases_retain_verifiable_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "benchmark"
            result = run_cross_domain_benchmark(root, trusted_local=True)
            self.assertEqual("passed", result["status"])
            self.assertEqual(2, result["passed"])
            report = read_json(root / "benchmark.json")
            self.assertFalse(report["model_used"])
            self.assertEqual(["ode-convergence", "throughput-sort"],
                             report["requested_cases"])
            self.assertTrue(verify_cross_domain_benchmark(root)["ok"])
            for row in report["cases"]:
                summary = row["summary"]
                self.assertIn(summary["study_status"], {"concluded", "budget_exhausted"})
                self.assertEqual(len(summary["claims"]), summary["evidence_claim_count"])
                self.assertEqual(bool(summary["claims"]), summary["evidence_available"])
            with self.assertRaisesRegex(ProtocolError, "已存在"):
                run_cross_domain_benchmark(root, trusted_local=True)
            project_spec = root / "ode-convergence" / "project" / "experiment.json"
            project_spec.write_text(project_spec.read_text(encoding="utf-8") + " ",
                                    encoding="utf-8")
            with self.assertRaises(ProtocolError):
                verify_cross_domain_benchmark(root)

    def test_failure_is_reported_and_not_relabelled_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "benchmark"
            with self.assertRaisesRegex(ProtocolError, "trusted-local"):
                run_cross_domain_benchmark(root)
            self.assertFalse(root.exists())
            with patch.object(ResearchController, "run", side_effect=RuntimeError("injected failure")):
                result = run_cross_domain_benchmark(
                    root, ["ode-convergence"], trusted_local=True)
            self.assertEqual("failed", result["status"])
            report = read_json(root / "benchmark.json")
            self.assertEqual("RuntimeError", report["cases"][0]["error_type"])
            with self.assertRaisesRegex(ProtocolError, "结构或状态"):
                verify_cross_domain_benchmark(root)
            report["status"] = "passed"
            report["cases"] = []
            write_json(root / "benchmark.json", report)
            with self.assertRaisesRegex(ProtocolError, "结构或状态"):
                verify_cross_domain_benchmark(root)


if __name__ == "__main__":
    unittest.main()
