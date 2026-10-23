import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from popper.cli import main
from popper.core import ProtocolError
from popper.demo import CASES, FILES, initialize_demo, run_offline_smoke


ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def test_each_demo_initializes_from_exact_shipped_template(self):
        for case in CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "project"
                result = initialize_demo(target, case)
                self.assertTrue(result["initialized"])
                for name in FILES:
                    self.assertEqual((target / name).read_bytes(),
                                     (ROOT / "examples" / case / name).read_bytes())
                self.assertTrue((target / "experiment.json").is_file())
                self.assertEqual(
                    result["next"],
                    [
                        f"popper research init {target.name} demo-run",
                        "popper research run demo-run --trusted-local",
                        "popper research confirm demo-run --trusted-local",
                    ],
                )

    def test_refuses_to_overwrite_existing_data(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "project"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaises(ProtocolError):
                initialize_demo(target)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_cli_entry_point(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "project"
            output = StringIO()
            with redirect_stdout(output):
                code = main(["demo", "init", str(target), "--case", "throughput-sort"])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["case"], "throughput-sort")
            self.assertIn("research init", payload["next"][0])
            self.assertTrue((target / "model.py").is_file())

    def test_offline_smoke_concludes_with_brief_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_offline_smoke(root=Path(temp), case="ode-convergence")
            self.assertTrue(result["ok"])
            summary = result["summary"]
            self.assertEqual(summary["phase"], "concluded")
            self.assertTrue(summary["integrity_ok"])
            self.assertIn("inconclusive", summary["read_as"])

    def test_cli_smoke_and_status_brief(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "smoke-root"
            output = StringIO()
            with redirect_stdout(output):
                code = main(["demo", "smoke", "--root", str(root), "--case", "ode-convergence"])
            self.assertEqual(code, 0)
            smoke = json.loads(output.getvalue())
            self.assertTrue(smoke["ok"])
            brief_out = StringIO()
            with redirect_stdout(brief_out):
                code = main(["research", "status", smoke["run_dir"], "--brief"])
            self.assertEqual(code, 0)
            brief = json.loads(brief_out.getvalue())
            self.assertEqual(brief["phase"], "concluded")
            self.assertTrue(brief["integrity_ok"])
            self.assertIn("演示完成", brief["read_as"])
            self.assertEqual(
                set(brief),
                {"phase", "study_status", "integrity_ok", "observations",
                 "latest_judgment", "claim_status", "budget", "read_as"},
            )


if __name__ == "__main__":
    unittest.main()
