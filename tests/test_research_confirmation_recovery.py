"""Confirmation recovery must never buy a second look at held-out data."""
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, initialize
from popper.research.actions import ActionProposal, REQUEST_CONFIRMATION, RUN_EXPERIMENT
from popper.research.budget import BudgetLedger
from popper.research.contracts import HypothesisStatus, StudyStatus
from popper.research.controller import ResearchController


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class ConfirmationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.run_dir = Path(self.temp.name) / "research-run"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.project / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.project)
        self.controller = None

    def tearDown(self):
        if self.controller:
            self.controller.close()
        self.temp.cleanup()

    def prepare(self, degree=2, min_improvement=None):
        if min_improvement is not None:
            path = self.project / "experiment.json"
            spec = json.loads(path.read_text(encoding="utf-8"))
            spec["min_improvement"] = min_improvement
            path.write_text(json.dumps(spec), encoding="utf-8")
        initialize(self.project)
        ResearchController.initialize(self.project, self.run_dir)
        self.controller = ResearchController(self.run_dir)
        controller = self.controller
        self.candidate_id = next(h for h in controller.manifest["candidate_hypothesis_ids"]
                                 if controller.manifest["configs"][h]["degree"] == degree)
        control_id = controller.manifest["control_hypothesis_id"]
        for hypothesis_id in (control_id, self.candidate_id):
            controller._execute_development(
                hypothesis_id, ActionProposal(RUN_EXPERIMENT, "recovery fixture", hypothesis_id),
                True, False)
        controller.store.set_hypothesis_status(control_id, 1, HypothesisStatus.INCONCLUSIVE)
        # The losing candidate fixture exercises a final confirmation rejection.
        controller.store.set_hypothesis_status(
            self.candidate_id, 1, HypothesisStatus.SUPPORTED_IN_SCOPE)
        controller._record(ActionProposal(REQUEST_CONFIRMATION, "fixture dev decision",
                                           self.candidate_id))
        self.initial_spent = controller.status()["budget"]["spent"]
        return controller

    def reopen(self):
        self.controller.close()
        self.controller = ResearchController(self.run_dir)
        return self.controller

    def confirmation_runs(self):
        return [r for r in self.controller.store.list("run")
                if r["run_id"].startswith("RUN-confirm-")]

    def consumed(self):
        return self.controller.exp.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='test_consumed'").fetchone()[0]

    def assert_budget(self, consumed):
        budget = self.controller.status()["budget"]
        self.assertEqual(0, budget["reserved"])
        self.assertEqual(self.initial_spent + consumed, budget["spent"])

    def test_second_reservation_failure_refunds_first_and_retry_is_new_attempt(self):
        controller = self.prepare()
        reserve = BudgetLedger.reserve

        def fail_second(ledger, family_id, item, amount):
            if item.startswith("RUN-confirm-candidate-"):
                raise RuntimeError("second reservation failed")
            return reserve(ledger, family_id, item, amount)

        with patch.object(BudgetLedger, "reserve", fail_second):
            with self.assertRaisesRegex(RuntimeError, "second reservation"):
                controller.confirm(trusted_local=True)
        self.assertEqual([], self.confirmation_runs())
        self.assert_budget(0)
        self.assertEqual(0, self.consumed())
        status = self.reopen().confirm(trusted_local=True)
        self.assertEqual("concluded", status["phase"])
        self.assertTrue(all(r["run_id"].endswith("-attempt-2") for r in self.confirmation_runs()))
        self.assert_budget(2)
        self.assertEqual(1, self.consumed())

    def test_registration_failure_cancels_run_and_releases_both_reservations(self):
        controller = self.prepare()
        register = controller.store.runs.register

        def fail_candidate(run_id, design_id, *args, **kwargs):
            if run_id.startswith("RUN-confirm-candidate-"):
                raise RuntimeError("register failed")
            return register(run_id, design_id, *args, **kwargs)

        with patch.object(controller.store.runs, "register", side_effect=fail_candidate):
            with self.assertRaisesRegex(RuntimeError, "register failed"):
                controller.confirm(trusted_local=True)
        self.assertEqual(["cancelled"], [r["status"] for r in self.confirmation_runs()])
        self.assert_budget(0)
        self.assertEqual(0, self.consumed())
        self.assertEqual("concluded", self.reopen().confirm(trusted_local=True)["phase"])
        self.assert_budget(2)

    def test_pre_consumption_confirmation_failure_refunds_and_allows_retry(self):
        controller = self.prepare()
        # C1 拆分后控制器走 begin_confirmation + finalize_confirmation，
        # 不再调用 exp.confirm。要模拟「消费前失败」（test_consumed 未提交、
        # phase 仍为 frozen），patch begin_confirmation 即可。
        with patch.object(controller.exp, "begin_confirmation",
                         side_effect=RuntimeError("before consumption")):
            with self.assertRaisesRegex(RuntimeError, "before consumption"):
                controller.confirm(trusted_local=True)
        self.assertEqual("frozen", controller.exp.state()["phase"])
        self.assertEqual(["cancelled", "cancelled"], [r["status"] for r in self.confirmation_runs()])
        self.assert_budget(0)
        self.assertEqual("concluded", self.reopen().confirm(trusted_local=True)["phase"])
        self.assert_budget(2)
        self.assertEqual(1, self.consumed())

    def test_consumed_failure_settles_runs_and_blocks_test_retry_and_policy(self):
        controller = self.prepare()
        evaluate = controller.exp.evaluate

        def fail_candidate(config, split, *args, **kwargs):
            if split == "test" and config["degree"] == 2:
                raise RuntimeError("candidate execution failed")
            return evaluate(config, split, *args, **kwargs)

        with patch.object(controller.exp, "evaluate", side_effect=fail_candidate):
            with self.assertRaisesRegex(RuntimeError, "candidate execution failed"):
                controller.confirm(trusted_local=True)
        self.assertCountEqual(["succeeded", "infrastructure_failed"],
                              [r["status"] for r in self.confirmation_runs()])
        self.assert_budget(2)
        self.assertEqual(1, self.consumed())
        controller = self.reopen()
        with patch.object(controller.exp, "confirm", side_effect=AssertionError("test rerun")), \
                patch.object(controller.policy, "choose", side_effect=AssertionError("policy choose")), \
                patch.object(controller.policy, "reflect", create=True,
                             side_effect=AssertionError("policy reflect")):
            self.assertEqual("confirmation_failed", controller.run(trusted_local=True)["phase"])
            with self.assertRaisesRegex(ProtocolError, "禁止重复执行"):
                controller.confirm(trusted_local=True)
        self.assert_budget(2)
        self.assertEqual(1, self.consumed())
        self.assertEqual([], [o for o in controller.store.list("observation")
                              if o["scope"] == "confirmation"])

    def test_interrupted_refund_reconciles_both_old_reservations_before_retry(self):
        controller = self.prepare()
        settle = BudgetLedger.settle
        calls = 0

        def fail_second_settlement(ledger, reservation_id, spent):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("refund interrupted")
            return settle(ledger, reservation_id, spent)

        # C1 拆分后控制器走 begin_confirmation，不再调用 exp.confirm。
        # 模拟「消费前失败」需 patch begin_confirmation。
        with patch.object(controller.exp, "begin_confirmation",
                         side_effect=RuntimeError("before consumption")), \
                patch.object(BudgetLedger, "settle", fail_second_settlement):
            with self.assertRaisesRegex(RuntimeError, "refund interrupted"):
                controller.confirm(trusted_local=True)
        self.assertEqual(1, controller.status()["budget"]["reserved"])
        self.assertEqual("concluded", self.reopen().confirm(trusted_local=True)["phase"])
        self.assert_budget(2)
        self.assertEqual(1, self.consumed())

    def test_scorer_failure_recovers_saved_predictions_and_partial_observation(self):
        controller = self.prepare()
        score = controller.evaluator.score_core_result

        def fail_candidate(state, result):
            if result["config"]["degree"] == 2:
                raise RuntimeError("scorer failed")
            return score(state, result)

        with patch.object(controller.evaluator, "score_core_result", side_effect=fail_candidate):
            with self.assertRaisesRegex(RuntimeError, "scorer failed"):
                controller.confirm(trusted_local=True)
        prior = [o for o in controller.store.list("observation") if o["scope"] == "confirmation"]
        self.assertEqual(1, len(prior))
        self.assertEqual(["succeeded", "succeeded"], [r["status"] for r in self.confirmation_runs()])
        self.assert_budget(2)
        self.assertEqual("completed", controller.exp.state()["phase"])
        controller = self.reopen()
        with patch.object(controller.exp, "confirm", side_effect=AssertionError("test rerun")), \
                patch.object(controller.policy, "choose", side_effect=AssertionError("policy choose")), \
                patch.object(controller.policy, "reflect", create=True,
                             side_effect=AssertionError("policy reflect")), \
                patch.object(controller.evaluator, "score_core_result",
                             wraps=controller.evaluator.score_core_result) as rescored:
            self.assertEqual("ready_for_confirmation", controller.run(trusted_local=True)["phase"])
            status = controller.run(trusted_local=True, auto_confirm=True)
        self.assertEqual("concluded", status["phase"])
        self.assertEqual(1, rescored.call_count)
        self.assertEqual(prior[0], controller.store.get("observation", prior[0]["observation_id"]))
        self.assertEqual(2, len([o for o in status["observations"] if o["scope"] == "confirmation"]))
        self.assert_budget(2)
        self.assertEqual(1, self.consumed())

    def test_interrupted_final_study_write_recovers_inconclusive_candidate(self):
        controller = self.prepare(degree=0, min_improvement=1e9)
        with patch.object(controller.store, "set_study_status", side_effect=RuntimeError("final write")):
            with self.assertRaisesRegex(RuntimeError, "final write"):
                controller.confirm(trusted_local=True)
        self.assertEqual("inconclusive", controller.store.get("hypothesis", self.candidate_id)["status"])
        decisions = controller.store.list("decision")
        observations = controller.store.list("observation")
        controller = self.reopen()
        with patch.object(controller.exp, "confirm", side_effect=AssertionError("test rerun")), \
                patch.object(controller.policy, "choose", side_effect=AssertionError("policy choose")), \
                patch.object(controller.policy, "reflect", create=True,
                             side_effect=AssertionError("policy reflect")):
            self.assertEqual("ready_for_confirmation", controller.run(trusted_local=True)["phase"])
            status = controller.run(trusted_local=True, auto_confirm=True)
        self.assertEqual(StudyStatus.CONCLUDED.value, status["phase"])
        self.assertEqual(decisions, controller.store.list("decision"))
        self.assertEqual(observations, controller.store.list("observation"))
        self.assert_budget(2)
        self.assertEqual(1, self.consumed())
        self.assertTrue(status["integrity"]["ok"])

    def test_interrupted_core_requires_explicit_recovery_before_settlement(self):
        controller = self.prepare()
        with patch.object(controller.exp, "evaluate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                controller.confirm(trusted_local=True)
        controller = self.reopen()
        with patch.object(controller.exp, "confirm", side_effect=AssertionError("test rerun")), \
                patch.object(controller.policy, "choose", side_effect=AssertionError("policy choose")), \
                patch.object(controller.policy, "reflect", create=True,
                             side_effect=AssertionError("policy reflect")):
            self.assertEqual("confirming", controller.run(trusted_local=True)["phase"])
            with self.assertRaisesRegex(ProtocolError, "显式 recover"):
                controller.confirm(trusted_local=True)
            # This fixture owns the interrupted runner; there is no live process.
            controller.exp.recover()
            with self.assertRaisesRegex(ProtocolError, "禁止重复执行"):
                controller.confirm(trusted_local=True)
        self.assertEqual(["infrastructure_failed", "infrastructure_failed"],
                         [r["status"] for r in self.confirmation_runs()])
        self.assert_budget(2)
        self.assertEqual(1, self.consumed())


if __name__ == "__main__":
    unittest.main()
