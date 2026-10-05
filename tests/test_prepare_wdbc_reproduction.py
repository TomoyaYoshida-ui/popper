import importlib.util
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.reproduction import ReproductionTask


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_wdbc_reproduction.py"
SPEC = importlib.util.spec_from_file_location("prepare_wdbc_reproduction", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PrepareWdbcReproductionTests(unittest.TestCase):
    def test_clean_project_uses_pinned_inputs_and_registers(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "new-reproduction"
            result = MODULE.prepare(target)
            self.assertEqual("registered", result["phase"])
            self.assertTrue((target / "source" / "wdbc.data").is_file())
            self.assertFalse((target / "results.json").exists())
            self.assertFalse((target / "history").exists())
            self.assertEqual("registered", ReproductionTask(target).audit()["phase"])

    def test_existing_files_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "existing"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaises(ProtocolError):
                MODULE.prepare(target)
            self.assertEqual("keep", marker.read_text(encoding="utf-8"))

    @unittest.skipUnless(importlib.util.find_spec("sklearn")
                         and importlib.util.find_spec("scipy"),
                         "WDBC reproduction requires optional ML dependencies")
    def test_prepared_case_completes_real_offline_reproduction(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "new-reproduction"
            MODULE.prepare(target)
            task = ReproductionTask(target)
            result = task.run(trusted_local=True)
            self.assertEqual("verified", result["phase"])
            self.assertEqual("supports_reproduction", result["assessment"]["outcome"])
            self.assertEqual("verified", task.audit()["phase"])


if __name__ == "__main__":
    unittest.main()
