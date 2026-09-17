import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popper.capabilities import extra_available
from popper.core import ProtocolError, file_hash, read_json, validate_spec

from evaluation.blind_pilot.tasks.base import (assert_objective_neutral, neutral_objective,
                                               CONTROLLED_EVIDENCE_CONDITIONS)
from evaluation.blind_pilot.tasks.registry import all_task_ids, specs
from evaluation.blind_pilot.policies import FixedPlanPolicy, scripted_plan


class TaskBuilderTests(unittest.TestCase):
    def test_all_families_and_conditions_covered(self):
        metas = [specs()[i] for i in range(len(specs()))]
        self.assertEqual(12, len(metas))
        families = {s.family for s in metas}
        self.assertEqual({"tabular", "small_time_series", "small_vision"}, families)
        for family in families:
            conds = {s.condition for s in metas if s.family == family}
            self.assertEqual(set(CONTROLLED_EVIDENCE_CONDITIONS), conds,
                             f"{family} 应覆盖全部四类隐藏情形")

    @unittest.skipUnless(extra_available("ml"),
                         "ml extra 未安装（scikit-learn）：构建后的任务要跑真模型代码做自校验")
    def test_build_is_deterministic(self):
        import tempfile
        import shutil
        base = Path(tempfile.mkdtemp(prefix="blind-task-test-"))
        try:
            for spec in specs():
                p1, g1 = base / (spec.task_id + "-1") / "public", base / (spec.task_id + "-1") / "gold"
                p2, g2 = base / (spec.task_id + "-2") / "public", base / (spec.task_id + "-2") / "gold"
                p1.mkdir(parents=True)
                p2.mkdir(parents=True)
                spec.build(p1, g1)
                spec.build(p2, g2)
                names = {f.name for f in p1.iterdir()}
                self.assertEqual(names, {f.name for f in p2.iterdir()})
                for name in names:
                    self.assertEqual(file_hash(p1 / name), file_hash(p2 / name),
                                     f"{spec.task_id}:{name} 破坏确定性")
                spec_obj = read_json(p1 / "experiment.json")
                validate_spec(spec_obj)
                assert_objective_neutral(spec_obj["objective"])
                validation = read_json(g1 / "validation-manifest.json")
                self.assertTrue(validation["passed"], spec.task_id)
                self.assertEqual(spec.condition, validation["condition"])
                if spec.condition == "scope_boundary_or_counterexample":
                    self.assertTrue(validation["checks"]["dev_boundary_candidate_exists"])
                    self.assertTrue(validation["checks"]["holdout_boundary_same_candidate"])
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_objective_denylist_rejects_method_keywords(self):
        with self.assertRaises(ProtocolError):
            assert_objective_neutral(
                "compare a logistic regression against a random forest baseline")


class FixedPlanPolicyTests(unittest.TestCase):
    def _context(self, candidates):
        return {"candidates": candidates}

    def test_plan_exhaustion_stops(self):
        policy = FixedPlanPolicy([{"degree": 2}])
        context = self._context([{"hypothesis_id": "H-1", "config": {"degree": 2},
                                  "status": "untested"},
                                 {"hypothesis_id": "H-2", "config": {"degree": 3},
                                  "status": "untested"}])
        first = policy.choose(context)
        self.assertEqual("run_experiment", first.kind)
        # 计划只含 degree2；H-2 不在计划内，H-1 已测后计划耗尽 → STOP
        context["candidates"][0]["status"] = "supported_in_scope"
        second = policy.choose(context)
        self.assertEqual("stop", second.kind)

    def test_plan_skips_tested_and_targets_next_in_plan(self):
        policy = FixedPlanPolicy([{"degree": 2}, {"degree": 3}])
        context = self._context([{"hypothesis_id": "H-1", "config": {"degree": 2},
                                  "status": "untested"},
                                 {"hypothesis_id": "H-2", "config": {"degree": 3},
                                  "status": "untested"}])
        self.assertEqual("H-1", policy.choose(context).hypothesis_id)
        # H-1 已测 → 应推进到计划内的 H-2
        context["candidates"][0]["status"] = "supported_in_scope"
        self.assertEqual("H-2", policy.choose(context).hypothesis_id)
        context["candidates"][1]["status"] = "supported_in_scope"
        self.assertEqual("stop", policy.choose(context).kind)

    def test_after_observation_is_evidence_blind(self):
        policy = FixedPlanPolicy([{"degree": 2}])
        for delta, threshold in ((0.5, 0.1), (-0.5, 0.1), (0.01, 0.1)):
            proposal = policy.after_observation("H-1", delta, threshold, True)
            self.assertEqual("run_experiment", proposal.kind)

    def test_scripted_plan_registration_order(self):
        spec = {"candidates": [{"degree": 3}, {"degree": 0}, {"degree": 2}]}
        self.assertEqual([{"degree": 3}, {"degree": 0}, {"degree": 2}],
                         scripted_plan(spec))


if __name__ == "__main__":
    unittest.main()
