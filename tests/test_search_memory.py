import unittest

from popper.research.search_memory import journal_summary, progress_for, research_notes
from popper.research.tree_search import node_record


class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.kinds = []

    def list(self, kind):
        self.kinds.append(kind)
        return self.rows if kind == "observation" else []


class SearchMemoryTests(unittest.TestCase):
    def test_progress_comes_from_dev_observations(self):
        rows = [{"hypothesis_id": "H-1", "scope": "dev", "value": 0.2, "observation_id": "O-1"}]
        self.assertEqual("idea", progress_for("H-2", rows))
        self.assertEqual("implemented", progress_for("H-1", rows))
        self.assertEqual("progress", progress_for("H-1", rows, baseline_value=0.5, direction="min"))
        self.assertEqual("implemented", progress_for("H-1", rows, baseline_value=0.1, direction="min"))

    def test_notes_are_a_bounded_projection(self):
        store = _Store([
            {"hypothesis_id": "H-1", "scope": "dev", "value": 0.2, "observation_id": "O-1"},
            {"hypothesis_id": "H-1", "scope": "confirmation", "value": 0.1, "observation_id": "O-2"},
        ])
        text = research_notes(store, {"candidate_hypothesis_ids": ["H-1"]}, "问题", limit=80)
        self.assertLessEqual(len(text.encode("utf-8")), 80)
        self.assertNotIn("O-2", research_notes(
            store, {"candidate_hypothesis_ids": ["H-1"]}, "问题"))
        self.assertIn("observation", store.kinds)

    def test_journal_ignores_smoke_and_names_the_representative(self):
        nodes = [node_record("n0", "draft", None, 0, "buggy"),
                 node_record("n1", "draft", None, 0, "scored", 0.4),
                 node_record("n2", "improve", "n1", 1, "scored", 0.1, "dev:smoke")]
        summary = journal_summary(nodes)
        self.assertEqual({"tried": 3, "buggy": 1, "representative": "n1"}, summary)
