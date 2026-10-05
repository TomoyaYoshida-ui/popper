"""Offline evaluation tests; none are evidence of model research ability."""
import copy
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, score
from popper.domains.oscillator_trajectory import PACK, FAILURE_ERROR
from popper.research.oscillator_study import (
    EvaluationBudgetExceeded, MeteredRHS, baseline_rk4, cases, prepare,
    preflight, reference, verify,
)


def small_row(system="harmonic"):
    return {"id": "one", "system": system, "omega": 1.0, "q0": 0.3,
            "v0": 0.0, "t_end": 1.0, "budget": 1024,
            "times": [i / 16 for i in range(17)]}


class OscillatorScoringTests(unittest.TestCase):
    def setUp(self):
        self.row = small_row()
        self.row["reference"], _ = reference(self.row)
        self.payload = {"trajectories": [{"id": "one", "states": self.row["reference"]}]}

    def test_exact_solution_scores_zero_through_kernel_registry(self):
        PACK.validate_rows([self.row], "dev")
        self.assertEqual(0, score([self.row], self.payload, PACK.evaluator_id))

    def test_phase_error_not_hidden_by_energy_conservation(self):
        states = [[self.row["q0"], 0.0] for _ in self.row["times"]]
        output = {"trajectories": [{"id": "one", "states": states}]}
        diagnostic = PACK.diagnostics([self.row], output)[0]
        self.assertEqual(0, diagnostic["energy_drift"])
        self.assertGreater(diagnostic["trajectory_error"], 0.4)

    def test_failed_case_is_not_dropped(self):
        output = {"trajectories": [{"id": "one", "states": None}]}
        self.assertEqual(FAILURE_ERROR, PACK.score([self.row], output, "trajectory_error"))
        self.assertTrue(PACK.diagnostics([self.row], output)[0]["failed"])

    def test_reject_scores_costs_missing_case_duplicates(self):
        for extra in ("trajectory_error", "function_evaluations"):
            with self.subTest(extra=extra), self.assertRaises(ProtocolError):
                PACK.score([self.row], {**self.payload, extra: 0}, "trajectory_error")
        with self.assertRaises(ProtocolError):
            PACK.score([self.row], {"trajectories": []}, "trajectory_error")
        with self.assertRaises(ProtocolError):
            PACK.score([self.row], {"trajectories": self.payload["trajectories"] * 2}, "trajectory_error")

    def test_initial_state_and_nonfinite_values_rejected(self):
        for value in (float("nan"), float("inf"), 1e200, True, "1"):
            altered = copy.deepcopy(self.payload)
            altered["trajectories"][0]["states"][1][0] = value
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                PACK.score([self.row], altered, "trajectory_error")
        altered = copy.deepcopy(self.payload)
        altered["trajectories"][0]["states"][0] = [0, 0]
        with self.assertRaises(ProtocolError):
            PACK.score([self.row], altered, "trajectory_error")

    def test_reference_not_projected_to_candidate(self):
        visible = PACK.project_inputs([self.row])
        self.assertNotIn("reference", visible[0])
        PACK.validate_rows(visible, "inputs")
        self.assertEqual(PACK.row_identity(self.row), PACK.row_identity(visible[0]))

    def test_zero_normalization_and_invalid_schedule_rejected(self):
        for change in ({"q0": 0}, {"times": [0, 0, 1]}, {"omega": 0}, {"budget": True}):
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                PACK.validate_rows([{**self.row, **change}], "dev")

    def test_mean_case_weight_not_median_hiding_failure(self):
        second = {**self.row, "id": "two"}
        output = {"trajectories": [self.payload["trajectories"][0], {"id": "two", "states": None}]}
        self.assertEqual(FAILURE_ERROR / 2, PACK.score([self.row, second], output, "trajectory_error"))


class OscillatorPreparationTests(unittest.TestCase):
    def test_host_rk4_counts_every_stage_and_respects_limit(self):
        row = small_row()
        states, calls = baseline_rk4(row)
        self.assertEqual(1024, calls)
        self.assertEqual(17, len(states))
        self.assertAlmostEqual(0.3 * math.cos(1), states[-1][0], places=10)
        rhs = MeteredRHS(row)
        rhs.calls = row["budget"]
        with self.assertRaises(EvaluationBudgetExceeded):
            rhs(0, (0.3, 0))
        self.assertEqual(row["budget"], rhs.calls)

    def test_nonlinear_reference_cross_method_and_tightening(self):
        states, check = reference(small_row("pendulum"))
        self.assertEqual(17, len(states))
        self.assertEqual([0.3, 0.0], states[0])
        self.assertEqual(["DOP853", "RK45", "DOP853_tightened"], check["methods"])
        self.assertLess(check["normalized_disagreement"], 1e-8)

    def test_factorial_cases_and_heldout_identity(self):
        identities = set()
        for split in ("train", "dev", "test"):
            rows = cases(split)
            self.assertEqual(32, len(rows))
            new = {PACK.row_identity(row) for row in rows}
            self.assertEqual(32, len(new))
            self.assertFalse(new & identities)
            identities |= new

    def test_preflight_never_claims_flags_prove_formal_execution(self):
        result = preflight()
        self.assertFalse(result["formal_run_ready"])
        self.assertGreater(len(result["blockers"]), 0)
        self.assertEqual(0, result["model_requests"])

    def test_prepare_freeze_verify_restart_and_refuse_overwrite(self):
        def tiny(split):
            row = small_row()
            row["id"] = split
            row["omega"] = {"train": 1, "dev": 2, "test": 3}[split]
            return [row]
        with tempfile.TemporaryDirectory() as directory, patch(
                "popper.research.oscillator_study.cases", side_effect=tiny):
            root = Path(directory) / "prepared"
            result = prepare(root)
            self.assertEqual(0, result["model_requests"])
            self.assertEqual("ok", verify(root)["integrity"])
            self.assertFalse((root / "agent-visible" / "test.json").exists())
            with self.assertRaises(ProtocolError):
                prepare(root)
            (root / "agent-visible" / "dev.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(ProtocolError):
                verify(root)


if __name__ == "__main__":
    unittest.main()
