import random
import unittest

from popper.core import ProtocolError
from evaluation.compare_search_policies import compare
from popper.domains.binary_classification import BinaryClassificationV1
from popper.domains.convergence_order import ConvergenceOrderV1
from popper.research.search_memory import research_notes
from popper.research.tree_search import (
    best_node, can_support_claim, choose_action, family_switch_required, node_record,
    reject_duplicate, representative_observation)


class _NotesStore:
    def list(self, kind):
        return []


def _node(node_id, kind, status, value=None, depth=0, parent_id=None, scope="dev"):
    return node_record(node_id, kind, parent_id, depth, status, value, scope)


class TreeSearchTests(unittest.TestCase):
    def test_smoke_cannot_support_a_claim_or_win(self):
        self.assertFalse(can_support_claim("dev:smoke"))
        self.assertTrue(can_support_claim("dev"))
        self.assertTrue(can_support_claim("dev:slice:0"))
        nodes = [_node("smoke", "draft", "scored", 0.1, scope="dev:smoke"),
                 _node("full", "draft", "scored", 0.4)]
        self.assertEqual("full", best_node(nodes)["node_id"])
        rows = [{"scope": "dev:smoke", "value": 0.1}, {"scope": "dev", "value": 0.4},
                {"scope": "dev", "value": 0.2}]
        self.assertEqual(0.2, representative_observation(rows, "min")["value"])
        self.assertEqual(1, ConvergenceOrderV1().smoke_repeat_limit())
        self.assertEqual(1, BinaryClassificationV1().smoke_repeat_limit())

    def test_drafts_then_debug_or_improve(self):
        self.assertEqual("draft", choose_action([], rng=random.Random(0))["action"])
        drafts = [_node("a", "draft", "buggy"), _node("b", "draft", "scored", 0.5)]
        debug = choose_action(drafts, debug_prob=1, rng=random.Random(0))
        self.assertEqual("debug", debug["action"])
        self.assertEqual("a", debug["parent_id"])
        improve = choose_action(drafts, debug_prob=0, rng=random.Random(0))
        self.assertEqual(("improve", "b"), (improve["action"], improve["parent_id"]))

    def test_duplicate_diff_is_rejected_before_execution(self):
        seen = set()
        edits = [{"path": "src/model.py", "search": "a", "replace": "b"}]
        reject_duplicate(edits, seen)
        with self.assertRaises(ProtocolError) as caught:
            reject_duplicate(edits, seen)
        self.assertIn("重复节点", str(caught.exception))

    def test_stagnant_score_requires_a_different_model_family(self):
        stagnant = [
            _node("n0", "draft", "scored", 7.895),
            _node("rejected", "improve", "buggy", 5.0, depth=1, parent_id="n0"),
            _node("n1", "improve", "scored", 7.887, depth=1, parent_id="n0"),
        ]
        self.assertTrue(family_switch_required(stagnant))
        self.assertFalse(family_switch_required([
            _node("n0", "draft", "scored", 10.0),
            _node("rejected", "improve", "buggy", 5.0, depth=1, parent_id="n0"),
            _node("n1", "improve", "scored", 9.85, depth=1, parent_id="n0"),
        ]))
        self.assertTrue(family_switch_required([
            _node("n0", "draft", "scored", 20.0),
            _node("n1", "improve", "scored", 19.85, depth=1, parent_id="n0"),
        ]))
        manifest = {"candidate_hypothesis_ids": ["H-1"], "metric": {"direction": "min"}}
        text = research_notes(_NotesStore(), manifest, "问题", nodes=stagnant)
        self.assertIn("换模型族", text)
        self.assertIn("树", text)
        self.assertIn("核方法", text)
        self.assertIn("特征变换", text)
        self.assertIn("岭回归", text)
        self.assertIn("L2", text)
        self.assertIn("正则", text)
        clear = research_notes(_NotesStore(), manifest, "问题", nodes=[
            _node("n0", "draft", "scored", 7.895),
            _node("n1", "improve", "scored", 7.70, depth=1, parent_id="n0"),
        ])
        self.assertNotIn("换模型族", clear)

    def test_scripted_tree_beats_one_repair_without_model_calls(self):
        report = compare([
            {"value": 0.9}, {"value": None}, {"value": 0.4}, {"value": 0.2},
        ], baseline=1.0)
        self.assertEqual(0, report["paid_model_calls"])
        one_shot, tree = report["policies"]
        self.assertEqual(1, one_shot["tried"])
        self.assertFalse(one_shot["reaches_threshold"])
        self.assertTrue(tree["reaches_threshold"])
        self.assertEqual(0.2, tree["value"])
