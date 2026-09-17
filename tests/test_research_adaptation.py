import copy
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import popper.research.store as research_store

from popper.core import ProtocolError, initialize
from popper.research.actions import (
    ADD_CONTROL, REQUEST_CONFIRMATION, RUN_EXPERIMENT, ActionProposal,
)
from popper.research.contracts import TransitionError
from popper.research.controller import ResearchController


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class ReflectingQuadraticPolicy:
    name = "quadratic_reflection_fixture"
    model = "deterministic_fixture"

    def __init__(self, fail_first_reflection=False):
        self.choose_calls = 0
        self.reflection_contexts = []
        self.responses = []
        self.fail_first_reflection = fail_first_reflection

    def choose(self, context):
        self.choose_calls += 1
        if self.choose_calls != 1:
            raise AssertionError("The persisted reflection must select the next experiment")
        candidate = next(row for row in context["candidates"]
                         if row["config"] == {"degree": 0})
        return ActionProposal(
            RUN_EXPERIMENT, "Test the registered constant-model hypothesis first.",
            candidate["hypothesis_id"], source=self.name, model=self.model)

    def reflect(self, context):
        self.reflection_contexts.append(copy.deepcopy(context))
        if self.fail_first_reflection:
            self.fail_first_reflection = False
            raise RuntimeError("reflection interrupted after scoring")
        response = {
            "action": REQUEST_CONFIRMATION,
            "rationale": "The observed improvement exceeds the registered threshold.",
            "alternative_explanation": "The development split may favor this model.",
            "next_hypothesis_id": None,
            "revision": None,
            "evidence_refs": list(context["evidence_refs"]),
        }
        if context["effect"] < context["threshold"]:
            candidate = next(row for row in context["remaining_candidates"]
                             if row["config"] == {"degree": 2})
            response.update(
                action=ADD_CONTROL,
                rationale="The constant model failed; test the registered quadratic control.",
                alternative_explanation="An apparent benefit may reflect sampling noise.",
                next_hypothesis_id=candidate["hypothesis_id"],
                revision={
                    "mechanism": "Quadratic features capture curvature absent in a constant model.",
                    "predictions": ["mechanism predicts improvement",
                                    "alternative predicts no improvement"],
                },
            )
        self.responses.append(copy.deepcopy(response))
        return response


class ResearchAdaptationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.run_dir = root / "research-run"
        self.project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.project / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.project)
        initialize(self.project)
        self.controller = None

    def tearDown(self):
        if self.controller is not None:
            self.controller.close()
        self.temp.cleanup()

    def open_controller(self, policy):
        ResearchController.initialize(self.project, self.run_dir, policy=policy)
        self.controller = ResearchController(self.run_dir, policy=policy)
        return self.controller

    def reopen_controller(self, policy):
        self.controller.close()
        self.controller = ResearchController(self.run_dir, policy=policy)
        return self.controller

    def events(self):
        return self.controller.store._unsafe_conn.execute(
            "SELECT * FROM events ORDER BY seq").fetchall()

    def test_negative_evidence_revises_control_and_resume_executes_its_new_design(self):
        policy = ReflectingQuadraticPolicy()
        controller = self.open_controller(policy)
        initial = controller.status()
        quadratic = next(row for row in initial["candidates"]
                         if row["config"] == {"degree": 2})
        old_design = controller.store.get("design", quadratic["design_id"])
        first = controller.run(trusted_local=True, max_steps=1)

        self.assertEqual("researching", first["phase"])
        self.assertEqual(2, len(first["observations"]))
        self.assertEqual(1, len(first["reflections"]))
        self.assertEqual(1, policy.choose_calls)
        self.assertEqual(1, len(policy.reflection_contexts))
        negative_context = policy.reflection_contexts[0]
        self.assertLess(negative_context["effect"], -negative_context["threshold"])
        self.assertEqual({"degree": 0}, negative_context["hypothesis"]["config"])
        self.assertAlmostEqual(
            negative_context["baseline"]["value"] - negative_context["candidate"]["value"],
            negative_context["effect"])
        self.assertTrue(all(row["scope"] == "dev" for row in negative_context["observations"]))

        reflection = first["reflections"][0]
        self.assertEqual(ADD_CONTROL, reflection["action"])
        self.assertEqual(quadratic["hypothesis_id"], reflection["next_hypothesis_id"])
        self.assertEqual(2, reflection["next_hypothesis_version"])
        revised = next(row for row in first["candidates"]
                       if row["hypothesis_id"] == quadratic["hypothesis_id"])
        self.assertEqual(2, revised["version"])
        self.assertEqual("untested", revised["status"])
        self.assertEqual(policy.responses[0]["revision"]["mechanism"], revised["mechanism"])
        self.assertEqual(policy.responses[0]["revision"]["predictions"], revised["predictions"])
        self.assertEqual(reflection["next_design_id"], revised["design_id"])
        self.assertTrue(revised["design_id"].endswith("-v2"))
        self.assertNotEqual(quadratic["design_id"], revised["design_id"])
        self.assertEqual(old_design, controller.store.get("design", quadratic["design_id"]))
        self.assertEqual(1, old_design["hypothesis_version"])
        self.assertEqual("frozen", old_design["status"])
        self.assertEqual(2.0, first["budget"]["spent"])

        original_observations = copy.deepcopy(first["observations"])
        controller = self.reopen_controller(policy)
        self.assertEqual(first["budget"], controller.status()["budget"])
        second = controller.run(trusted_local=True, max_steps=1)

        self.assertEqual("ready_for_confirmation", second["phase"])
        self.assertTrue(second["integrity"]["ok"])
        self.assertEqual(1, policy.choose_calls)
        self.assertEqual(2, len(policy.reflection_contexts))
        self.assertEqual(2, len(second["reflections"]))
        positive_context = policy.reflection_contexts[1]
        self.assertEqual({"degree": 2}, positive_context["hypothesis"]["config"])
        self.assertEqual(2, positive_context["hypothesis"]["version"])
        self.assertGreaterEqual(positive_context["effect"], positive_context["threshold"])
        self.assertEqual(REQUEST_CONFIRMATION, second["reflections"][-1]["action"])
        observations = {row["observation_id"]: row for row in second["observations"]}
        self.assertEqual(3, len(observations))
        for observation in original_observations:
            self.assertEqual(observation, observations[observation["observation_id"]])
        positive_observation = observations[positive_context["candidate"]["observation_id"]]
        self.assertEqual(revised["design_id"], positive_observation["design_id"])
        self.assertEqual(2, positive_observation["hypothesis_version"])
        self.assertTrue(all(row["scope"] == "dev" for row in observations.values()))
        self.assertEqual(old_design, controller.store.get("design", quadratic["design_id"]))
        self.assertEqual(first["budget"]["family_id"], second["budget"]["family_id"])
        self.assertEqual(first["budget"]["cap"], second["budget"]["cap"])
        self.assertEqual(first["budget"]["spent"] + 1.0, second["budget"]["spent"])
        self.assertEqual(first["budget"]["available"] - 1.0, second["budget"]["available"])

    def test_resume_completes_interrupted_reflection_without_repeating_scored_run(self):
        policy = ReflectingQuadraticPolicy(fail_first_reflection=True)
        controller = self.open_controller(policy)
        with self.assertRaisesRegex(RuntimeError, "reflection interrupted after scoring"):
            controller.run(trusted_local=True, max_steps=1)
        interrupted = controller.status()
        self.assertEqual(2, len(interrupted["observations"]))
        self.assertEqual([], interrupted["reflections"])
        self.assertEqual(2.0, interrupted["budget"]["spent"])
        runs_before = controller.store.list("run")

        controller = self.reopen_controller(policy)
        recovered = controller.run(trusted_local=True, max_steps=0)
        self.assertEqual(interrupted["observations"], recovered["observations"])
        self.assertEqual(interrupted["budget"], recovered["budget"])
        self.assertEqual(runs_before, controller.store.list("run"))
        self.assertEqual(1, len(recovered["reflections"]))
        self.assertEqual(1, policy.choose_calls)
        self.assertEqual(2, len(policy.reflection_contexts))
        self.assertEqual(policy.reflection_contexts[0], policy.reflection_contexts[1])

        final = controller.run(trusted_local=True, max_steps=1)
        self.assertEqual("ready_for_confirmation", final["phase"])
        self.assertEqual(3, len(final["observations"]))
        self.assertEqual(3.0, final["budget"]["spent"])
        self.assertEqual(1, policy.choose_calls)
        self.assertEqual(3, len(policy.reflection_contexts))
        self.assertEqual(2, len(final["reflections"]))

    def test_reflection_replay_is_idempotent_and_invalid_evidence_changes_no_events(self):
        policy = ReflectingQuadraticPolicy()
        controller = self.open_controller(policy)
        first = controller.run(trusted_local=True, max_steps=1)
        reflection = first["reflections"][0]
        prior_events = self.events()
        prior_status = controller.status()
        replayed = controller.store.apply_reflection(
            reflection["reflection_id"], controller.manifest["study_id"], policy.responses[0],
            reflection["source_state_version"], actor=policy.name, model=policy.model)
        self.assertEqual(reflection, replayed)
        self.assertEqual(prior_events, self.events())
        self.assertEqual(prior_status, controller.status())

        for refs in ([reflection["evidence_refs"][0], "OBS-does-not-exist"],
                     list(reversed(reflection["evidence_refs"]))):
            with self.subTest(evidence_refs=refs):
                invalid = copy.deepcopy(policy.responses[0])
                invalid["evidence_refs"] = refs
                with self.assertRaises(ProtocolError):
                    controller.store.apply_reflection(
                        "REF-invalid", controller.manifest["study_id"], invalid,
                        prior_status["study"]["state_version"], actor=policy.name, model=policy.model)
                self.assertEqual(prior_events, self.events())
                self.assertEqual(prior_status, controller.status())

    def test_reflection_transaction_rolls_back_late_failure_and_can_retry(self):
        policy = ReflectingQuadraticPolicy(fail_first_reflection=True)
        controller = self.open_controller(policy)
        with self.assertRaisesRegex(RuntimeError, "reflection interrupted after scoring"):
            controller.run(trusted_local=True, max_steps=1)
        result = policy.reflect(policy.reflection_contexts[0])
        self.assertLess(policy.reflection_contexts[0]["effect"], 0)
        self.assertEqual(ADD_CONTROL, result["action"])

        store = controller.store
        study_id = controller.manifest["study_id"]
        reflection_id = "REF-transaction-retry"
        target_id = result["next_hypothesis_id"]
        before = controller.status()
        before_events = self.events()
        before_snapshots = {
            kind: store.list(kind)
            for kind in ("hypothesis", "design", "reflection", "decision", "observation", "run")
        }
        self.assertEqual([], before_snapshots["reflection"])
        self.assertEqual(1, store.get("hypothesis", target_id)["version"])
        real_append = store._append
        attempted_events = []

        def fail_final_decision(kind, entity_id, event_type, payload, writer):
            attempted_events.append((kind, event_type))
            if kind == "decision" and event_type == "created":
                # Prove earlier writes happened inside the transaction before
                # injecting the failure whose rollback this test exercises.
                self.assertEqual(2, store.get("hypothesis", target_id)["version"])
                pending = store.get("reflection", reflection_id)
                self.assertIsNotNone(pending)
                self.assertEqual("frozen", store.get("design", pending["next_design_id"])["status"])
                raise RuntimeError("injected final decision append failure")
            return real_append(kind, entity_id, event_type, payload, writer)

        with patch.object(store, "_append", side_effect=fail_final_decision):
            with self.assertRaisesRegex(RuntimeError, "injected final decision append failure"):
                store.apply_reflection(
                    reflection_id, study_id, result, before["study"]["state_version"],
                    actor=policy.name, model=policy.model)

        self.assertEqual([
            ("hypothesis", "revised"), ("design", "created"), ("design", "frozen"),
            ("reflection", "created"), ("decision", "created"),
        ], attempted_events)
        self.assertEqual(before_events, self.events())
        for kind, snapshot in before_snapshots.items():
            self.assertEqual(snapshot, store.list(kind), kind)
        rolled_back = controller.status()
        self.assertEqual(before["study"]["state_version"], rolled_back["study"]["state_version"])
        self.assertEqual(before["budget"], rolled_back["budget"])
        self.assertEqual(before, rolled_back)

        applied = store.apply_reflection(
            reflection_id, study_id, result, before["study"]["state_version"],
            actor=policy.name, model=policy.model)
        self.assertEqual(2, store.get("hypothesis", target_id)["version"])
        self.assertEqual("frozen", store.get("design", applied["next_design_id"])["status"])
        self.assertEqual(applied, store.get("reflection", reflection_id))
        self.assertEqual(ADD_CONTROL, store.get("decision", applied["decision_id"])["action"])
        self.assertTrue(store.verify()["ok"])
        self.assertEqual(before["budget"], controller.status()["budget"])
        self.assertEqual(before_snapshots["observation"], store.list("observation"))
        self.assertEqual(before_snapshots["run"], store.list("run"))

    def test_reflection_channel_goes_through_the_design_transition_table(self):
        # A2：反思通道过去直接写 design FROZEN、跳过 4 张转移表。现在它与公开写方法
        # 共用 _TRANSITION_VALIDATORS：把 design 守卫换成拒绝实现，该通道必须失败并回滚。
        policy = ReflectingQuadraticPolicy(fail_first_reflection=True)
        controller = self.open_controller(policy)
        with self.assertRaisesRegex(RuntimeError, "reflection interrupted after scoring"):
            controller.run(trusted_local=True, max_steps=1)
        result = policy.reflect(policy.reflection_contexts[0])
        self.assertEqual(ADD_CONTROL, result["action"])

        store = controller.store
        before = controller.status()
        before_events = self.events()

        def reject_design(current, target):
            raise TransitionError("design 转移必须经状态机")

        with patch.dict(research_store._TRANSITION_VALIDATORS, {"design": reject_design}):
            with self.assertRaisesRegex(TransitionError, "必须经状态机"):
                store.apply_reflection(
                    "REF-guard", controller.manifest["study_id"], result,
                    before["study"]["state_version"], actor=policy.name, model=policy.model)
        self.assertEqual(before_events, self.events())
        self.assertEqual(before, controller.status())


if __name__ == "__main__":
    unittest.main()
