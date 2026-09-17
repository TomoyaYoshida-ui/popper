import copy
import unittest

from evaluation.blind_pilot.policies import FixedPlanPolicy
from popper.research.actions import RUN_EXPERIMENT, STOP


class FixedPlanPolicyTests(unittest.TestCase):
    def setUp(self):
        self.first = {"model": "a"}
        self.second = {"model": "b"}
        self.policy = FixedPlanPolicy([self.first, self.second])

    def _context(self, first_status="untested", second_status="untested", observations=None):
        return {"candidates": [
            {"hypothesis_id": "H-a", "status": first_status, "config": self.first},
            {"hypothesis_id": "H-b", "status": second_status, "config": self.second},
        ], "observations": observations or []}

    def test_pre_registered_order_is_independent_of_evidence_values(self):
        low = self._context(observations=[{"value": -1000}])
        high = self._context(observations=[{"value": 1000}])
        self.assertEqual("H-a", self.policy.choose(low).hypothesis_id)
        self.assertEqual("H-a", self.policy.choose(high).hypothesis_id)

    def test_skips_completed_item_and_stops_only_after_plan_exhaustion(self):
        proposal = self.policy.choose(self._context(first_status="inconclusive"))
        self.assertEqual((RUN_EXPERIMENT, "H-b"), (proposal.kind, proposal.hypothesis_id))
        stopped = self.policy.choose(self._context("inconclusive", "contradicted_in_scope"))
        self.assertEqual(STOP, stopped.kind)

    def test_after_observation_is_evidence_blind(self):
        negative = self.policy.after_observation("H-a", -999, 0.1, True)
        positive = self.policy.after_observation("H-a", 999, 0.1, True)
        self.assertEqual(copy.deepcopy(negative), positive)
        self.assertEqual(RUN_EXPERIMENT, positive.kind)


if __name__ == "__main__":
    unittest.main()
