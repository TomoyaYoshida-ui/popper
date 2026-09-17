import unittest
from unittest.mock import patch

from popper.core import ProtocolError
from evaluation.blind_pilot.compose_repair_report import cell_key, compose


class ComposeRepairReportTests(unittest.TestCase):
    @staticmethod
    def _summary(task="T01", comparator="adaptive", run_index=1, valid=False):
        return {
            "cell": {"task_id": task, "comparator": comparator, "run_index": run_index},
            "task_id": task, "comparator": comparator, "run_index": run_index,
            "condition": "positive_effect", "family": "small_tabular",
            "status": "completed" if valid else "failed",
            "integrity_ok": valid, "phase": "concluded" if valid else None,
            "evidence_replay": {"ok": valid}, "conclusion_matched": valid,
            "test_exposure": {"consumed": valid},
        }

    def test_cell_key_uses_full_registered_identity(self):
        summary = {"cell": {"task_id": "T01", "comparator": "adaptive", "run_index": 2}}
        self.assertEqual(("T01", "adaptive", 2), cell_key(summary))

    def test_incomplete_cell_identity_is_rejected(self):
        with self.assertRaises(ProtocolError):
            cell_key({"cell": {"task_id": "T01", "comparator": "adaptive"}})

    def test_compose_replaces_exact_cell_and_keeps_failure_history(self):
        before = self._summary(valid=False)
        after = self._summary(valid=True)
        base_report = {"path": "base/blind-report.json", "sha256": "a" * 64,
                       "report": {"planned_trajectories": 1, "failures": [{"old": True}]}}
        repair_report = {"path": "repair/blind-report.json", "sha256": "b" * 64,
                         "report": {}}
        calls = [({cell_key(before): before}, {cell_key(before): {"trial": "base"}}, base_report),
                 ({cell_key(after): after}, {cell_key(after): {"trial": "repair"}}, repair_report)]
        with patch("evaluation.blind_pilot.compose_repair_report._audit_trial",
                   side_effect=calls):
            report = compose("base", ["repair"])

        self.assertEqual(1, report["valid_loops"])
        self.assertEqual(1, report["scientifically_valid_loops"])
        self.assertFalse(report["composite"]["is_single_frozen_run"])
        self.assertEqual([{"old": True}], report["composite"]["original_failures"])
        self.assertFalse(report["composite"]["replacements"][0]["before"]["valid_loop"])
        self.assertTrue(report["composite"]["replacements"][0]["after"]["valid_loop"])

    def test_replacement_for_unregistered_cell_is_rejected(self):
        base = self._summary(task="T01")
        repair = self._summary(task="T99", valid=True)
        base_report = {"path": "base/report", "sha256": "a", "report": {}}
        calls = [({cell_key(base): base}, {cell_key(base): {}}, base_report),
                 ({cell_key(repair): repair}, {cell_key(repair): {}}, None)]
        with patch("evaluation.blind_pilot.compose_repair_report._audit_trial",
                   side_effect=calls):
            with self.assertRaises(ProtocolError):
                compose("base", ["repair"])

    def test_later_repair_trial_supersedes_earlier_claim_of_same_cell(self):
        base = self._summary(task="T01", valid=False)
        v2 = self._summary(task="T01", valid=False)
        v3 = self._summary(task="T01", valid=True)
        base_report = {"path": "base/r", "sha256": "a", "report": {}}
        calls = [
            ({cell_key(base): base}, {cell_key(base): {"trial": "base"}}, base_report),
            ({cell_key(v2): v2}, {cell_key(v2): {"trial": "v2"}}, None),
            ({cell_key(v3): v3}, {cell_key(v3): {"trial": "v3"}}, None),
        ]
        with patch("evaluation.blind_pilot.compose_repair_report._audit_trial",
                   side_effect=calls):
            report = compose("base", ["v2", "v3"])

        self.assertEqual(1, report["valid_loops"])
        one = report["composite"]["replacements"][0]
        self.assertTrue(one["after"]["valid_loop"])
        self.assertEqual("v3", one["source"]["trial"])
        # the superseded v2 intermediate is preserved on the winner cell
        self.assertEqual([{"trial": "v2"}], one["supersedes"])


if __name__ == "__main__":
    unittest.main()
