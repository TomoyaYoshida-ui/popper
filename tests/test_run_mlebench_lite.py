"""MLE-bench Lite 运行器：确认集隔离、token 上限、确认只调用一次。"""
import json
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, score
from popper.capabilities import extra_available
if not extra_available('ml'):
    raise unittest.SkipTest('ml extra is required for MLE-bench Lite runner tests')
from popper.research.models import EvidenceDrivenPolicy

from evaluation.mlebench_lite import materialize
from evaluation.mlebench_lite_entrypoint import predict
from evaluation.run_mlebench_lite import (TokenBudget, assert_confirm_outside_envelope,
                                          assert_holdout_isolated, build_run_report,
                                          execute_research, redact)


def _spooky_rows():
    rows = []
    for index in range(40):
        label = index % 3
        rows.append({"id": f"s{index}", "text": f"authorclass{label} token{index}", "label": label})
    return rows


class EntrypointTests(unittest.TestCase):
    def test_registered_models_score_without_reading_holdout(self):
        source = Path("evaluation/mlebench_lite_entrypoint.py").read_text(encoding="utf-8")
        self.assertNotIn("test.json", source)
        train = _spooky_rows()[:24]
        inputs = [{"id": row["id"], "text": row["text"]} for row in _spooky_rows()[24:]]
        for name in ("constant", "linear"):
            predictions = predict(train, inputs, {"model": name}, 0)
            score(_spooky_rows()[24:], predictions, "mlebench-spooky-logloss-v1")
        nomad_train = [{"id": f"n{i}", "features": [float(i), 1.0], "y": [float(i % 3), float(i % 2)]}
                       for i in range(12)]
        nomad_inputs = [{"id": row["id"], "features": row["features"]} for row in nomad_train[:4]]
        nomad_pred = predict(nomad_train, nomad_inputs, {"model": "linear"}, 0)
        score(nomad_train[:4], nomad_pred, "mlebench-nomad-rmsle-v1")
        pizza_train = [{"id": f"p{i}", "text": "yes please" if i % 2 else "no thanks", "label": i % 2}
                       for i in range(12)]
        pizza_inputs = [{"id": row["id"], "text": row["text"]} for row in pizza_train[:4]]
        pizza_pred = predict(pizza_train, pizza_inputs, {"model": "linear"}, 0)
        score(pizza_train[:4], pizza_pred, "mlebench-pizza-auc-v1")


class BudgetTests(unittest.TestCase):
    def test_cap_stops_the_next_call(self):
        budget = TokenBudget(total_cap=5, per_task_cap=5)
        budget.begin_task()

        def call(system, payload):
            call.tokens_used = lambda: 6
            return {"ok": True}

        call.tokens_used = lambda: 0
        wrapped = budget.wrap(call)
        with self.assertRaises(ProtocolError):
            wrapped("system", {})
        self.assertTrue(budget.stopped)
        with self.assertRaises(ProtocolError):
            wrapped("system", {})
        self.assertEqual(1, budget.calls)

    def test_report_rejects_secret_fragments(self):
        marker = "KG" + "AT_"
        sample = marker + "exampletoken"
        self.assertNotIn(marker, redact("prefix " + sample + " suffix"))
        with self.assertRaises(RuntimeError):
            build_run_report({"note": sample})


class ConfirmationOnceTests(unittest.TestCase):
    def test_kernel_confirms_holdout_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = materialize("spooky-author-identification", _spooky_rows(), root)
            assert_holdout_isolated(manifest)
            project = root / "project"
            train_ids = {row["id"] for row in json.loads((project / "train.json").read_text(encoding="utf-8"))}
            dev_ids = {row["id"] for row in json.loads((project / "dev.json").read_text(encoding="utf-8"))}
            self.assertFalse(set(manifest["holdout_ids"]) & train_ids)
            self.assertFalse(set(manifest["holdout_ids"]) & dev_ids)
            budget = TokenBudget()
            budget.begin_task()
            outcome = execute_research(
                project, root / "research", policy=EvidenceDrivenPolicy(), budget=budget,
                sandboxed=False, trusted_local=True)
            assert_confirm_outside_envelope(root / "research")
            self.assertEqual(1, outcome["confirmation_calls"])
            self.assertTrue(outcome["confirmation_consumed_once"])
            self.assertTrue(outcome["completed"])
            self.assertEqual(0, budget.calls)
            self.assertFalse((root / "research" / "envelope.json").exists())
