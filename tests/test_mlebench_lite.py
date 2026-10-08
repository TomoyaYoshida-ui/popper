"""MLE-bench Lite 三道小题：计分、确认集隔离，以及未备数据时不调用模型。"""
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, initialize, score
from evaluation.mlebench_lite import (TOKEN_CAP, build_report, holdout_split,
                                      load_task, materialize)


def _spooky_rows():
    rows = []
    for index in range(30):
        rows.append({"id": f"s{index}", "text": f"sentence {index}", "label": index % 3})
    return rows


def _nomad_rows():
    rows = []
    for index in range(30):
        rows.append({"id": f"n{index}", "features": [float(index), 1.0],
                     "y": [float(index % 4), float((index + 1) % 3)]})
    return rows


def _pizza_rows():
    rows = []
    for index in range(30):
        rows.append({"id": f"p{index}", "text": f"request {index}", "label": index % 2})
    return rows


class ScoreTests(unittest.TestCase):
    def test_spooky_log_loss(self):
        rows = [{"id": "a", "text": "one", "label": 0},
                {"id": "b", "text": "two", "label": 1}]
        predictions = [{"id": "a", "prediction": [0.5, 0.25, 0.25]},
                       {"id": "b", "prediction": [0.2, 0.6, 0.2]}]
        expected = (-math.log(0.5) - math.log(0.6)) / 2
        self.assertAlmostEqual(expected, score(rows, predictions, "mlebench-spooky-logloss-v1"))

    def test_nomad_rmsle_and_pizza_auc(self):
        nomad = [{"id": "a", "features": [1.0], "y": [0.0, math.e - 1]},
                 {"id": "b", "features": [1.0], "y": [0.0, math.e - 1]}]
        predictions = [{"id": "a", "prediction": [0.0, 0.0]},
                       {"id": "b", "prediction": [0.0, 0.0]}]
        self.assertAlmostEqual(0.5, score(nomad, predictions, "mlebench-nomad-rmsle-v1"))
        pizza = [{"id": "a", "text": "no", "label": 0}, {"id": "b", "text": "no", "label": 0},
                 {"id": "c", "text": "yes", "label": 1}, {"id": "d", "text": "yes", "label": 1}]
        ranked = [{"id": "a", "prediction": 0.1}, {"id": "b", "prediction": 0.4},
                  {"id": "c", "prediction": 0.35}, {"id": "d", "prediction": 0.8}]
        self.assertAlmostEqual(0.75, score(pizza, ranked, "mlebench-pizza-auc-v1"))

    def test_bad_probability_is_rejected(self):
        rows = _spooky_rows()[:3]
        with self.assertRaises(ProtocolError):
            score(rows, [{"id": row["id"], "prediction": [1, 0, 0]} for row in rows],
                  "mlebench-spooky-logloss-v1")


class HoldoutTests(unittest.TestCase):
    def test_confirmation_rows_stay_out_of_development_files(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = materialize("spooky-author-identification", _spooky_rows(), directory)
            project = Path(directory) / "project"
            train = json.loads((project / "train.json").read_text(encoding="utf-8"))
            dev = json.loads((project / "dev.json").read_text(encoding="utf-8"))
            test = json.loads((project / "test.json").read_text(encoding="utf-8"))
            holdout = set(manifest["holdout_ids"])
            self.assertEqual(holdout, {row["id"] for row in test})
            self.assertFalse(holdout & {row["id"] for row in train})
            self.assertFalse(holdout & {row["id"] for row in dev})
            state = initialize(project)
            self.assertEqual("mlebench-spooky-logloss-v1", state["evaluator_id"])

    def test_split_is_stable_for_each_task(self):
        for rows in (_spooky_rows(), _nomad_rows(), _pizza_rows()):
            first = [row["id"] for row in holdout_split(rows)[2]]
            second = [row["id"] for row in holdout_split(rows)[2]]
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 1)

    def test_duplicate_rows_stay_in_one_split(self):
        rows = _nomad_rows()
        rows.append({**rows[0], "id": "n-copy"})
        with tempfile.TemporaryDirectory() as directory:
            materialize("nomad2018-predict-transparent-conductors", rows, directory)
            project = Path(directory) / "project"
            initialize(project)
            placed = {}
            for name in ("train.json", "dev.json", "test.json"):
                for row in json.loads((project / name).read_text(encoding="utf-8")):
                    key = (tuple(row["features"]), tuple(row["y"]))
                    placed.setdefault(key, set()).add(name)
            self.assertTrue(all(len(names) == 1 for names in placed.values()))

    def test_loaders_keep_typed_labels_and_do_not_spend_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spooky_dir = root / "spooky-author-identification"
            nomad_dir = root / "nomad2018-predict-transparent-conductors"
            pizza_dir = root / "random-acts-of-pizza"
            for path in (spooky_dir, nomad_dir, pizza_dir):
                path.mkdir()
            with (spooky_dir / "train.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, ["id", "text", "author"])
                writer.writeheader()
                for index in range(30):
                    writer.writerow({"id": f"s{index}", "text": f"t{index}",
                                     "author": ("EAP", "HPL", "MWS")[index % 3]})
            with (nomad_dir / "train.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, ["id", "spacegroup",
                                                 "formation_energy_ev_natom", "bandgap_energy_ev"])
                writer.writeheader()
                for index in range(30):
                    writer.writerow({"id": index, "spacegroup": 1,
                                     "formation_energy_ev_natom": 0.1, "bandgap_energy_ev": 1.0})
            records = [{"request_id": f"p{index}", "request_text": f"please {index}",
                        "requester_received_pizza": index % 2} for index in range(30)]
            (pizza_dir / "train.json").write_text(json.dumps(records), encoding="utf-8")
            self.assertEqual(1, load_task("spooky-author-identification", spooky_dir)[1]["label"])
            nomad = load_task("nomad2018-predict-transparent-conductors", nomad_dir)
            self.assertEqual([0.1, 1.0], nomad[0]["y"])
            self.assertEqual([1.0], nomad[0]["features"])
            self.assertEqual(0, load_task("random-acts-of-pizza", pizza_dir)[0]["label"])
            report = build_report(root)
            self.assertEqual("ready", report["status"])
            self.assertEqual(0, report["paid_model_calls"])


class BudgetTests(unittest.TestCase):
    def test_missing_raw_data_records_the_confirmed_cap_without_a_call(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_report(directory)
        self.assertEqual("blocked_missing_raw_data", report["status"])
        self.assertEqual(TOKEN_CAP, report["budget_confirmed_tokens"])
        self.assertEqual(0, report["paid_model_calls"])
        self.assertEqual(0, report["tokens_used"])
        self.assertEqual(3, len(report["missing_tasks"]))
