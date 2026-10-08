"""实验树只跟线性候选的开发集比，并且不消费确认集。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.capabilities import extra_available
if not extra_available('ml'):
    raise unittest.SkipTest('ml extra is required for MLE-bench Lite tree tests')
from evaluation.mlebench_lite import materialize
from evaluation.run_mlebench_lite_tree import (
    apply_min_improvement, beats_linear, drive_tree, margin_from_rows, oriented_delta,
    prior_tokens_used)


def _nomad_rows():
    rows = []
    for index in range(30):
        rows.append({"id": f"n{index}", "features": [float(index), 1.0],
                     "y": [float(index % 4), float((index + 1) % 3)]})
    return rows


class MarginTests(unittest.TestCase):
    def test_oriented_delta_follows_metric_direction(self):
        self.assertAlmostEqual(0.5507256758625302, oriented_delta(1.0865072784096697, 0.5357816025471395, "min"))
        self.assertAlmostEqual(0.1250238595151747, oriented_delta(0.5, 0.6250238595151747, "max"))

    def test_margin_requires_exactly_one_dev_observation_each(self):
        rows = [
            {"hypothesis_id": "control", "scope": "dev", "value": 1.0865072784096697},
            {"hypothesis_id": "linear", "scope": "dev", "value": 0.5357816025471395},
            {"hypothesis_id": "linear", "scope": "confirmation", "value": 0.1},
        ]
        margin = margin_from_rows(rows, "control", "linear", "min")
        self.assertAlmostEqual(0.5507256758625302, margin["delta"])
        with self.assertRaises(ProtocolError):
            margin_from_rows(rows + [dict(rows[1])], "control", "linear", "min")

    def test_prior_study_sqlite_supplies_the_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            study = Path(directory)
            research = study / "research"
            research.mkdir()
            (research / "research.json").write_text(json.dumps({
                "metric": {"direction": "max"},
                "control_hypothesis_id": "control",
                "candidate_hypothesis_ids": ["linear"],
            }), encoding="utf-8")
            database = sqlite3.connect(research / "research.sqlite")
            database.execute("CREATE TABLE snapshots (kind TEXT, payload TEXT)")
            database.executemany(
                "INSERT INTO snapshots VALUES ('observation', ?)",
                [(json.dumps(row),) for row in (
                    {"hypothesis_id": "control", "scope": "dev", "value": 0.5},
                    {"hypothesis_id": "linear", "scope": "dev", "value": 0.6250238595151747},
                )])
            database.commit()
            database.close()
            from evaluation.run_mlebench_lite_tree import load_prior_margin
            margin = load_prior_margin(study)
        self.assertAlmostEqual(0.1250238595151747, margin["delta"])

    def test_threshold_is_written_before_a_new_project_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            materialize("nomad2018-predict-transparent-conductors", _nomad_rows(), project_root)
            apply_min_improvement(project_root / "project", 0.08932056421258844)
            spec = json.loads((project_root / "project" / "experiment.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(0.08932056421258844, spec["min_improvement"])


class ConfirmGuardTests(unittest.TestCase):
    def test_ready_for_confirmation_does_not_call_confirm(self):
        class Controller:
            def run(self, **kwargs):
                self.kwargs = kwargs
                return {"phase": "ready_for_confirmation"}

            def confirm(self, *args, **kwargs):
                raise AssertionError("确认集不得再次消费")

        controller = Controller()
        outcome = drive_tree(controller, max_steps=8)
        self.assertEqual(0, outcome["confirmation_calls"])
        self.assertEqual("ready_for_confirmation", outcome["phase"])
        self.assertTrue(controller.kwargs["sandboxed"])
        self.assertTrue(controller.kwargs["autonomous_code"])
        self.assertTrue(controller.kwargs["search"])
        self.assertFalse(controller.kwargs["auto_confirm"])
        self.assertEqual(8, controller.kwargs["max_steps"])

    def test_beats_linear_uses_direction(self):
        self.assertTrue(beats_linear(0.4, 0.5, "min"))
        self.assertFalse(beats_linear(0.5, 0.5, "min"))
        self.assertTrue(beats_linear(0.7, 0.6, "max"))
        self.assertFalse(beats_linear(None, 0.6, "max"))

    def test_prior_token_spend_is_read_from_the_finished_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({"tokens_used": 22323}), encoding="utf-8")
            self.assertEqual(22323, prior_tokens_used(path))
            path.write_text(json.dumps({"tokens_used": 600000}), encoding="utf-8")
            with self.assertRaises(ProtocolError):
                prior_tokens_used(path)
