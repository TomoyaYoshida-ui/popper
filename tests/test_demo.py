import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from popper.cli import main
from popper.core import ProtocolError
from popper.demo import CASES, FILES, initialize_demo


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
            self.assertEqual(json.loads(output.getvalue())["case"], "throughput-sort")
            self.assertTrue((target / "model.py").is_file())


if __name__ == "__main__":
    unittest.main()
