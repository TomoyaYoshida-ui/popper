import json
import shutil
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from popper import domains
from popper.core import EVALUATORS, Experiment, ProtocolError, initialize
from popper.domains import protocol
from popper.domains.convergence_order import ConvergenceOrderV1
from popper.domains.protocol import MetricSpec


class ExternalOrderPack(ConvergenceOrderV1):
    pack_id = "test-external-order"
    evaluator_id = "test-external-order-v1"
    _metrics = (MetricSpec(name="test_external_order", direction="max"),)
    _entry = {
        "id": evaluator_id,
        "metric": {"name": "test_external_order", "direction": "max"},
        "definition": "test-only external convergence score",
        "dataset": "declared refinement rows",
    }


class FakeEntryPoint:
    name = "external-order"

    def __init__(self, pack, dist=None):
        self.pack = pack
        self.dist = dist or FakeDistribution(Path(__file__).resolve().parent)
        self.calls = 0

    def load(self):
        self.calls += 1
        return self.pack


class FakeDistribution:
    def __init__(self, root, files=None):
        self.root = root
        self.files = ((PurePosixPath(Path(__file__).name),)
                      if files is None else files)

    def locate_file(self, name):
        return self.root / name


class DomainEntryPointTests(unittest.TestCase):
    def tearDown(self):
        protocol._PACKS.pop(ExternalOrderPack.evaluator_id, None)
        protocol._EXTERNAL_SOURCES.pop(ExternalOrderPack.evaluator_id, None)
        protocol._EXTERNAL_DISTRIBUTIONS.pop(ExternalOrderPack.evaluator_id, None)

    def test_loading_is_explicit_and_visible_to_evaluator_registry(self):
        entry = FakeEntryPoint(ExternalOrderPack())
        with patch("popper.domains.entry_points", return_value=[entry]) as discover:
            self.assertEqual((), domains.load_external_packs(""))
            discover.assert_not_called()
            self.assertEqual((ExternalOrderPack.evaluator_id,),
                             domains.load_external_packs("external-order"))
            discover.assert_called_once_with(group="popper.domain_packs")
        self.assertEqual(1, entry.calls)
        self.assertIn(ExternalOrderPack.evaluator_id, EVALUATORS)
        self.assertIs(protocol.get(ExternalOrderPack.evaluator_id), entry.pack)

    def test_missing_duplicate_or_ambiguous_entry_points_fail_closed(self):
        entry = FakeEntryPoint(ExternalOrderPack())
        with patch("popper.domains.entry_points", return_value=[entry]):
            with self.assertRaisesRegex(ValueError, "重复名称"):
                domains.load_external_packs("external-order,external-order")
            with self.assertRaisesRegex(ValueError, "不存在或重名"):
                domains.load_external_packs("not-installed")
        with patch("popper.domains.entry_points", return_value=[entry, entry]):
            with self.assertRaisesRegex(ValueError, "不存在或重名"):
                domains.load_external_packs("external-order")
        self.assertEqual(0, entry.calls)
        self.assertNotIn(ExternalOrderPack.evaluator_id, EVALUATORS)

    def test_multi_pack_selection_rolls_back_on_later_failure(self):
        entry = FakeEntryPoint(ExternalOrderPack())
        with patch("popper.domains.entry_points", return_value=[entry]):
            with self.assertRaisesRegex(ValueError, "不存在或重名"):
                domains.load_external_packs("external-order,not-installed")
        self.assertEqual(1, entry.calls)
        self.assertNotIn(ExternalOrderPack.evaluator_id, EVALUATORS)

    def test_existing_evaluator_identity_cannot_be_replaced(self):
        pack = ExternalOrderPack()
        pack.evaluator_id = "convergence-order-v1"
        entry = FakeEntryPoint(pack)
        with patch("popper.domains.entry_points", return_value=[entry]):
            with self.assertRaisesRegex(ValueError, "重复注册"):
                domains.load_external_packs("external-order")
        self.assertIsNot(protocol.get("convergence-order-v1"), pack)

    def test_external_pack_drives_complete_non_ml_experiment(self):
        entry = FakeEntryPoint(ExternalOrderPack())
        with patch("popper.domains.entry_points", return_value=[entry]):
            domains.load_external_packs("external-order")
        example = Path(__file__).resolve().parents[1] / "examples" / "ode-convergence"
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            for name in ("experiment.json", "model.py", "train.json", "dev.json", "test.json"):
                shutil.copyfile(example / name, project / name)
            spec_path = project / "experiment.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["metric"]["name"] = "test_external_order"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            initialize(project)
            experiment = Experiment(project)
            try:
                experiment.search(True)
                experiment.freeze()
                experiment.confirm(True)
                replay = experiment.replay()
                self.assertTrue(replay["claim_recomputed"])
                self.assertGreaterEqual(replay["runs_recomputed"], 4)
            finally:
                experiment.close()

    def test_external_source_change_invalidates_saved_experiment(self):
        entry = FakeEntryPoint(ExternalOrderPack())
        example = Path(__file__).resolve().parents[1] / "examples" / "ode-convergence"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "external_pack.py"
            source.write_text("implementation version one\n", encoding="utf-8")
            with patch("popper.domains.entry_points", return_value=[entry]), patch(
                    "popper.domains.inspect.getsourcefile", return_value=str(source)):
                domains.load_external_packs("external-order")
            project = root / "project"
            project.mkdir()
            for name in ("experiment.json", "model.py", "train.json", "dev.json", "test.json"):
                shutil.copyfile(example / name, project / name)
            spec_path = project / "experiment.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["metric"]["name"] = "test_external_order"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            initialize(project)
            experiment = Experiment(project)
            try:
                experiment.search(True)
                experiment.freeze()
                experiment.confirm(True)
                self.assertTrue(experiment.replay()["claim_recomputed"])
                source.write_text("implementation version two\n", encoding="utf-8")
                with self.assertRaisesRegex(ProtocolError, "评估器版本已改变"):
                    experiment.replay()
            finally:
                experiment.close()

    def test_distribution_helper_change_updates_evaluator_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "external_pack.py"
            source.write_text("implementation\n", encoding="utf-8")
            helper = root / "helper.py"
            helper.write_text("helper version one\n", encoding="utf-8")
            entry = FakeEntryPoint(ExternalOrderPack(), FakeDistribution(
                root, (PurePosixPath("external_pack.py"), PurePosixPath("helper.py"))))
            with patch("popper.domains.entry_points", return_value=[entry]), patch(
                    "popper.domains.inspect.getsourcefile", return_value=str(source)):
                domains.load_external_packs("external-order")
            before = EVALUATORS[ExternalOrderPack.evaluator_id]
            self.assertIn("implementation_distribution_sha256", before)
            helper.write_text("helper version two\n", encoding="utf-8")
            after = EVALUATORS[ExternalOrderPack.evaluator_id]
            self.assertNotEqual(before["implementation_distribution_sha256"],
                                after["implementation_distribution_sha256"])

    def test_distribution_without_file_manifest_is_rejected(self):
        entry = FakeEntryPoint(ExternalOrderPack(), FakeDistribution(
            Path(__file__).resolve().parent, ()))
        with patch("popper.domains.entry_points", return_value=[entry]):
            with self.assertRaisesRegex(ValueError, "可枚举文件清单"):
                domains.load_external_packs("external-order")
        self.assertNotIn(ExternalOrderPack.evaluator_id, EVALUATORS)


if __name__ == "__main__":
    unittest.main()
