"""三类候选的固定序列。不调用模型。"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from popper.core import ProtocolError
from popper.research.budget import BudgetLedger
from popper.research.candidate_space import (
    choose_space_action, claim_from_observation, record_observation, register_candidate,
)
from popper.research.intake import intake
from popper.research.recipe import approve_recipe, compile_recipe, propose_recipe
from popper.research.recipe_runner import run_recipe, run_workspace
from popper.research.schema import migrate_budget

RATIOS = {"train_ratio": "0.600000", "dev_ratio": "0.200000", "holdout_ratio": "0.200000"}


def _ledger(cap):
    conn = sqlite3.connect(":memory:")
    migrate_budget(conn)
    ledger = BudgetLedger(conn)
    ledger.open_family("F1", cap)
    return ledger


def _recipe(steps):
    return {
        "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
        "steps": steps,
    }


class CandidateSpaceTests(unittest.TestCase):
    def test_fixed_sequence_covers_the_three_actions(self):
        ledger = _ledger(3.0)
        seen = set()
        base = _recipe([{"op": "impute_median", "column": "income"}])
        engineered = _recipe([
            {"op": "impute_median", "column": "income"},
            {"op": "scale", "column": "age", "output": "age_scaled"},
        ])
        ablated = _recipe([{"op": "select_missing_rate", "max_missing_rate": 0.4}])
        feature = register_candidate(
            ledger, "F1", seen, parent_id=None, action="feature_engineering",
            reason="增加训练集上拟合的标准化列", recipe=engineered)
        family = register_candidate(
            ledger, "F1", seen, parent_id=feature["candidate_id"], action="model_family",
            reason="在模板集合里选择线性模型", recipe=base, template={"id": "linear", "C": 1.0})
        ablation = register_candidate(
            ledger, "F1", seen, parent_id=feature["candidate_id"], action="ablation",
            reason="去掉标准化这一步", recipe=ablated)
        self.assertEqual(
            ["feature_engineering", "model_family", "ablation"],
            [feature["action"], family["action"], ablation["action"]])
        self.assertNotEqual(feature["recipe_sha256"], family["recipe_sha256"])
        with self.assertRaises(ProtocolError) as unknown:
            register_candidate(
                ledger, "F1", seen, parent_id=None, action="model_family",
                reason="不在模板里", template={"id": "forest"})
        self.assertEqual(str(unknown.exception), "模型族候选不在模板集合内")
        observation = record_observation(ablation, scope="dev", value=0.5)
        self.assertIs(False, observation["claim"])
        with self.assertRaises(ProtocolError) as claim:
            claim_from_observation(observation)
        self.assertEqual(str(claim.exception), "消融观察不能升级为 Claim")
        self.assertEqual({"written": False, "candidate_id": feature["candidate_id"]},
                         claim_from_observation(record_observation(feature, scope="dev", value=0.2)))
        with self.assertRaises(ProtocolError) as duplicate:
            register_candidate(
                ledger, "F1", seen, parent_id=feature["candidate_id"], action="ablation",
                reason="去掉标准化这一步", recipe=ablated)
        self.assertEqual(str(duplicate.exception), "重复节点，未执行")
        with self.assertRaises(ProtocolError) as exhausted:
            register_candidate(
                ledger, "F1", seen, parent_id=None, action="model_family",
                reason="换一个树模板", template={"id": "tree", "max_depth": 3})
        self.assertIn("预算不足", str(exhausted.exception))
        self.assertEqual("feature_engineering", choose_space_action([])["action"])

    def test_feature_recipe_changes_input_hash_and_keeps_holdout(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "table.csv").write_text(
                "age,income,label\n1,1,0\n2,,1\n3,3,0\n4,4,1\n", encoding="utf-8")
            intake(root, raw="table.csv", split_strategy="random", seed=3,
                   source_note="fixture", target_column="label", **RATIOS)
            base = _compile(root, _recipe([{"op": "impute_median", "column": "income"}]))
            engineered = _compile(root, _recipe([
                {"op": "impute_median", "column": "income"},
                {"op": "scale", "column": "age", "output": "age_scaled"},
            ]))
            self.assertNotEqual(base["input_identity_sha256"], engineered["input_identity_sha256"])
            self.assertEqual(base["holdout_row_set_sha256"], engineered["holdout_row_set_sha256"])
            self.assertEqual(base["seal_sha256"], engineered["seal_sha256"])

    def test_scale_fits_on_train_only(self):
        recipe = _recipe([{"op": "scale", "column": "age", "output": "age_scaled"}])
        train = [
            {"id": "t1", "values": {"age": 0, "label": 0}},
            {"id": "t2", "values": {"age": 2, "label": 1}},
        ]
        dev = [{"id": "d1", "values": {"age": 10, "label": 0}}]
        holdout = [{"id": "h1", "values": {"age": -10, "label": 1}}]
        types = {"age": "integer", "label": "integer"}
        fit, train_out, dev_out, holdout_out = run_recipe(recipe, train, dev, holdout, types)
        self.assertEqual(fit["steps"][0]["mean"], 1)
        self.assertEqual(fit["steps"][0]["std"], 1)
        self.assertEqual(train_out[0]["values"]["age_scaled"], -1)
        self.assertEqual(dev_out[0]["values"]["age"], 10)
        self.assertEqual(holdout_out[0]["id"], "h1")
        self.assertEqual(holdout_out[0]["values"]["age_scaled"], -11)


def _compile(root, recipe):
    path = root / "recipe.json"
    path.write_text(__import__("json").dumps(recipe), encoding="utf-8")
    proposed = propose_recipe(root, path)
    approve_recipe(root, proposed["proposal_id"])
    return compile_recipe(root, proposed["proposal_id"], "binary-accuracy-v1",
                          executor=run_workspace)


if __name__ == "__main__":
    unittest.main()
