import json
import runpy
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from popper.core import EVALUATORS, ProtocolError, digest, file_hash, initialize, read_json
from popper.research.actions import (ADD_CONTROL, REQUEST_CONFIRMATION,
                                     RUN_EXPERIMENT, STOP)
from popper.research.controller import ResearchController
from popper.research.contracts import ExperimentDesign, Hypothesis
from popper.research.evaluation_service import evaluate_request, scoring_code_hash
from popper.research.models import EvidenceDrivenPolicy
from popper.research.models import DeepSeekResearchPolicy


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class EvidencePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = EvidenceDrivenPolicy()

    def test_result_sign_changes_scientific_action(self):
        self.assertEqual(
            REQUEST_CONFIRMATION,
            self.policy.after_observation("H1", 0.2, 0.1, True).kind)
        self.assertEqual(
            ADD_CONTROL,
            self.policy.after_observation("H1", -0.2, 0.1, True).kind)
        uncertain = self.policy.after_observation("H1", 0.01, 0.1, False)
        self.assertEqual(STOP, uncertain.kind)
        self.assertIn("证据不足", uncertain.rationale)

    def test_policy_never_selects_already_tested_hypothesis(self):
        context = {"candidates": [
            {"hypothesis_id": "H1", "status": "contradicted_in_scope", "config": {"x": 1}},
            {"hypothesis_id": "H2", "status": "untested", "config": {"x": 2}},
        ]}
        proposal = self.policy.choose(context)
        self.assertEqual(RUN_EXPERIMENT, proposal.kind)
        self.assertEqual("H2", proposal.hypothesis_id)


class DeepSeekPolicyContractTests(unittest.TestCase):
    def _policy(self, response):
        policy = object.__new__(DeepSeekResearchPolicy)
        policy.model = "deepseek-flash"
        policy.name = "deepseek_json_policy"
        policy._call = lambda *_: response
        return policy

    def test_generated_hypotheses_are_index_bound(self):
        response = {"hypotheses": [
            {"candidate_index": 1, "mechanism": "m1", "applicability": "a1",
             "predictions": ["p1"], "falsification": ["f1"], "alternatives": ["x1"]},
            {"candidate_index": 0, "mechanism": "m0", "applicability": "a0",
             "predictions": ["p0"], "falsification": ["f0"], "alternatives": ["x0"]},
        ]}
        rows = self._policy(response).generate_hypotheses("q", [{"x": 0}, {"x": 1}])
        self.assertEqual("m0", rows[0]["mechanism"])
        self.assertEqual("m1", rows[1]["mechanism"])

    def test_model_cannot_select_unregistered_hypothesis(self):
        policy = self._policy({"kind": RUN_EXPERIMENT, "hypothesis_id": "FAKE",
                               "rationale": "try", "alternatives": []})
        context = {"study": {"question": "q"},
                   "metric": {"name": "mse", "direction": "min"},
                   "min_meaningful_effect": 0.1,
                   "budget": {"available": 1}, "observations": [],
                   "candidates": [{"hypothesis_id": "H1", "status": "untested",
                                   "config": {"x": 1}}]}
        with self.assertRaisesRegex(ProtocolError, "不存在或已检验"):
            policy.choose(context)

    def test_invalid_choice_gets_one_bounded_correction_with_original_context(self):
        policy = self._policy(None)
        invalid = {"kind": RUN_EXPERIMENT, "hypothesis_id": "FAKE",
                   "rationale": "try", "alternatives": []}
        valid = {"kind": RUN_EXPERIMENT, "hypothesis_id": "H1",
                 "rationale": "registered", "alternatives": []}
        policy._call = Mock(side_effect=[invalid, valid])
        context = {"study": {"question": "q"},
                   "metric": {"name": "mse", "direction": "min"},
                   "min_meaningful_effect": 0.1,
                   "budget": {"available": 1}, "observations": [{"value": 2.0}],
                   "candidates": [{"hypothesis_id": "H1", "status": "untested",
                                   "config": {"x": 1}}]}

        proposal = policy.choose(context)

        self.assertEqual("H1", proposal.hypothesis_id)
        self.assertEqual(2, policy._call.call_count)
        first_payload = policy._call.call_args_list[0].args[1]
        correction = policy._call.call_args_list[1].args[1]
        self.assertEqual(first_payload, correction["research_context"])
        self.assertEqual(invalid, correction["previous_invalid_response"])
        self.assertIn("hypothesis_id", correction["validation_error"])

    def test_repeated_invalid_choice_stops_after_two_calls(self):
        policy = self._policy(None)
        invalid = {"kind": RUN_EXPERIMENT, "hypothesis_id": "FAKE",
                   "rationale": "try", "alternatives": []}
        policy._call = Mock(side_effect=[invalid, dict(invalid)])
        context = {"study": {"question": "q"},
                   "metric": {"name": "mse", "direction": "min"},
                   "min_meaningful_effect": 0.1, "budget": {"available": 1},
                   "observations": [],
                   "candidates": [{"hypothesis_id": "H1", "status": "untested",
                                   "config": {"x": 1}}]}
        with self.assertRaisesRegex(ProtocolError, "hypothesis_id"):
            policy.choose(context)
        self.assertEqual(2, policy._call.call_count)

    def test_code_proposal_accepts_replacement_and_rejects_source_alias(self):
        for key in ("replacement", "source"):
            with self.subTest(key=key):
                policy = self._policy({"edits": [{"path": "helper.py",
                    "original_sha256": None, key: "x = 1\n"}], "rationale": "helper"})
                if key == "replacement":
                    result = policy.propose_revision("q", {}, {}, [])
                    self.assertEqual("x = 1\n", result["edits"][0].replacement)
                else:
                    with self.assertRaisesRegex(ProtocolError, "字段不正确"):
                        policy.propose_revision("q", {}, {}, [])

    def test_empty_code_proposal_accepts_registered_implementation(self):
        policy = self._policy(None)
        valid = {"edits": [], "rationale": "already supported"}
        policy._call = Mock(return_value=valid)
        code_files = [{"path": "model.py", "sha256": "a" * 64, "content": "x = 0\n"}]

        result = policy.propose_revision("q", {"hypothesis_id": "H1"}, {"x": 1}, code_files)

        self.assertEqual((), result["edits"])
        self.assertEqual(1, policy._call.call_count)

    def test_repeated_malformed_code_proposal_stops_after_two_calls(self):
        policy = self._policy(None)
        invalid = {"edits": None, "rationale": "already supported"}
        policy._call = Mock(side_effect=[invalid, dict(invalid)])
        with self.assertRaisesRegex(ProtocolError, "edits"):
            policy.propose_revision("q", {"hypothesis_id": "H1"}, {"x": 1}, [])
        self.assertEqual(2, policy._call.call_count)


class IndependentEvaluationTests(unittest.TestCase):
    def test_request_recomputes_prediction_and_rejects_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = root / "data.json"
            predictions = root / "predictions.json"
            data.write_text('[{"id":"a","x":1,"y":2}]', encoding="utf-8")
            predictions.write_text('[{"id":"a","prediction":2}]', encoding="utf-8")
            request = {"schema_version": "2.0", "request_id": "R1",
                       "dataset": str(data), "dataset_sha256": file_hash(data),
                       "evaluator_id": "mse-v1",
                       "evaluator_hash": digest(EVALUATORS["mse-v1"]),
                       "expected_seeds": [1], "scoring_code_sha256": scoring_code_hash(),
                       "predictions": [{"seed": 1, "path": str(predictions),
                                        "sha256": file_hash(predictions)}]}
            self.assertEqual(0.0, evaluate_request(request)["mean"])
            predictions.write_text('[]', encoding="utf-8")
            with self.assertRaisesRegex(ProtocolError, "摘要变化"):
                evaluate_request(request)


class ResearchControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.run_dir = Path(self.temp.name) / "research-run"
        self.root.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(EXAMPLE / name, self.root / name)
        runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](self.root)
        initialize(self.root)
        self.controller = None

    def tearDown(self):
        if self.controller:
            self.controller.close()
        self.temp.cleanup()

    def _open(self):
        ResearchController.initialize(self.root, self.run_dir)
        self.controller = ResearchController(self.run_dir)
        return self.controller

    def test_init_materializes_hypotheses_and_frozen_designs(self):
        controller = self._open()
        status = controller.status()
        self.assertEqual("researching", status["phase"])
        self.assertEqual(3, len(status["candidates"]))
        self.assertTrue(status["integrity"]["ok"])
        self.assertEqual("registered_hypothesis_baseline", status["capability_mode"])
        self.assertEqual(4, len(controller.store.list("design")))
        self.assertTrue(all(d["status"] == "frozen" for d in controller.store.list("design")))

    def test_frozen_agent_design_routes_only_to_registered_candidate(self):
        controller = self._open()
        candidate_id = controller.manifest["candidate_hypothesis_ids"][0]
        original = controller.store.get("design", controller._current_design_id(candidate_id))

        def add_design(design_id, interventions):
            controller.store.create_design(ExperimentDesign(
                design_id=design_id, hypothesis_id=candidate_id,
                interventions=tuple(interventions), control=original["control"],
                baseline=original["baseline"], metric=original["metric"],
                scorer_id=original["scorer_id"],
                metric_direction=original["metric_direction"],
                analysis_unit=original["analysis_unit"],
                splits=tuple(original["splits"]), seeds=tuple(original["seeds"]),
                min_meaningful_effect=original["min_meaningful_effect"]))
            controller.store.freeze_design(design_id)

        add_design("D-agent-unregistered", ('{"unregistered":true}',))
        with patch.object(controller, "_require_sandbox_backend"), self.assertRaisesRegex(
                ProtocolError, "未匹配预注册候选"):
            controller.execute_registered_design("D-agent-unregistered")
        add_design("D-agent-registered", original["interventions"])
        with patch.object(controller, "_require_sandbox_backend"), patch.object(
                controller, "_execute_development", return_value={"ok": True}) as execute:
            self.assertEqual({"ok": True}, controller.execute_registered_design(
                "D-agent-registered"))
            self.assertEqual("D-agent-registered", execute.call_args.kwargs["design_id"])
            self.assertEqual(controller.manifest["configs"][candidate_id],
                             execute.call_args.kwargs["config"])

    def test_frozen_agent_design_executes_with_independent_observation(self):
        from popper import sandbox
        if not sandbox.available():
            self.skipTest("sandbox backend unavailable")
        controller = self._open()
        candidate_id = controller.manifest["candidate_hypothesis_ids"][0]
        original = controller.store.get("design", controller._current_design_id(candidate_id))
        hypothesis_id = "H-agent-new-registered"
        controller.store.add_hypothesis(Hypothesis(
            hypothesis_id=hypothesis_id, study_id=controller.manifest["study_id"],
            version=1, parent_version=None, mechanism="registered candidate mechanism",
            applicability="current development scope", predictions=("directional effect",),
            falsification=("no directional effect",)))
        design_id = "D-agent-executed"
        controller.store.create_design(ExperimentDesign(
            design_id=design_id, hypothesis_id=hypothesis_id,
            interventions=tuple(original["interventions"]),
            control=original["control"], baseline=original["baseline"],
            metric=original["metric"], scorer_id=original["scorer_id"],
            metric_direction=original["metric_direction"],
            analysis_unit=original["analysis_unit"],
            splits=tuple(original["splits"]), seeds=tuple(original["seeds"]),
            min_meaningful_effect=original["min_meaningful_effect"]))
        controller.store.freeze_design(design_id)
        spent_before = controller.status()["budget"]["spent"]
        observation = controller.execute_registered_design(design_id)
        run = controller.store.get("run", observation["run_id"])
        self.assertEqual(design_id, run["design_id"])
        self.assertEqual("succeeded", run["status"])
        self.assertEqual("dev", observation["scope"])
        self.assertTrue(run["revision_id"].startswith("REV-"))
        self.assertEqual(design_id,
                         controller.revisions.verify(run["revision_id"])["identity"]["design_id"])
        self.assertTrue(controller.exp.results("dev"))
        self.assertEqual("searching", controller.exp.state()["phase"])
        self.assertEqual([], controller.exp.results("test"))
        self.assertTrue(controller.store.verify()["ok"])
        self.assertEqual("inconclusive", controller.store.get(
            "hypothesis", hypothesis_id)["status"])
        self.assertEqual(spent_before + 1, controller.status()["budget"]["spent"])
        self.assertEqual(observation["observation_id"],
                         controller.execute_registered_design(design_id)["observation_id"])
        second_hypothesis_id = "H-agent-duplicate-evidence"
        controller.store.add_hypothesis(Hypothesis(
            hypothesis_id=second_hypothesis_id,
            study_id=controller.manifest["study_id"], version=1,
            parent_version=None, mechanism="same registered candidate",
            applicability="current development scope", predictions=("effect",),
            falsification=("no effect",)))
        controller.store.create_design(ExperimentDesign(
            design_id="D-agent-duplicate-evidence",
            hypothesis_id=second_hypothesis_id,
            interventions=tuple(original["interventions"]),
            control=original["control"], baseline=original["baseline"],
            metric=original["metric"], scorer_id=original["scorer_id"],
            metric_direction=original["metric_direction"],
            analysis_unit=original["analysis_unit"],
            splits=tuple(original["splits"]), seeds=tuple(original["seeds"]),
            min_meaningful_effect=original["min_meaningful_effect"]))
        controller.store.freeze_design("D-agent-duplicate-evidence")
        from popper.research.readout import controller_routing_readiness, read_research_state
        projection = read_research_state(self.run_dir)
        duplicate = next(row for row in projection["designs"]
                         if row["design_id"] == "D-agent-duplicate-evidence")
        route = controller_routing_readiness(self.run_dir, projection, duplicate)
        self.assertFalse(route["registered_design_route_ready"])
        self.assertIn("candidate_evidence_already_used", route["registered_design_reasons"])
        with self.assertRaisesRegex(ProtocolError, "不能作为新设计的独立证据"):
            controller.execute_registered_design("D-agent-duplicate-evidence")

    def test_execute_design_cli_dispatches_to_controller(self):
        from popper.cli import main
        with patch("popper.cli.ResearchController") as controller_type, patch(
                "builtins.print"):
            controller_type.return_value.execute_registered_design.return_value = {
                "observation_id": "OBS-cli"}
            self.assertEqual(0, main(["research", "execute-design", str(self.run_dir),
                                      "--design", "D-agent-cli"]))
            controller_type.return_value.execute_registered_design.assert_called_once_with(
                "D-agent-cli")
            controller_type.return_value.close.assert_called_once()

    def test_real_adaptive_loop_and_confirmation(self):
        controller = self._open()
        status = controller.run(trusted_local=True, auto_confirm=True)
        self.assertEqual("concluded", status["phase"])
        self.assertTrue(status["integrity"]["ok"])
        self.assertEqual(2, len([o for o in status["observations"]
                                 if o["scope"] == "confirmation"]))
        self.assertTrue(any(d["action"] == REQUEST_CONFIRMATION
                            for d in status["decisions"]))
        self.assertTrue(any(c["status"] == "supported_in_scope"
                            for c in status["candidates"]))
        self.assertGreater(status["budget"]["spent"], 0)
        self.assertEqual("separate_process_same_account", status["evaluation_trust"])
        self.assertEqual(1, len(status["claims"]))
        claim = status["claims"][0]
        self.assertEqual("registered_independent_confirmation", claim["scope"])
        self.assertEqual("supported_in_scope", claim["status"])
        self.assertEqual(2, len(claim["observation_refs"]))
        self.assertGreaterEqual(claim["oriented_delta"], claim["min_meaningful_effect"])
        from popper.research.readout import read_research_state
        projected = read_research_state(self.run_dir)
        self.assertEqual(claim["claim_id"], projected["claims"][0]["entity_id"])
        self.assertTrue(projected["experiment_graph"]["integrity"]["ok"])
        decision_id = next(row["entity_id"] for row in
                           controller.store.list_with_ids("decision")
                           if row["actor"] == "confirmation_controller")
        duplicate = controller.store.claims.confirm(
            controller.manifest["study_id"], decision_id,
            *claim["observation_refs"])
        self.assertEqual(claim, duplicate)
        dev_observation = next(item for item in status["observations"]
                               if item["scope"] == "dev")
        with self.assertRaisesRegex(ProtocolError, "决策"):
            controller.store.claims.confirm(
                controller.manifest["study_id"], decision_id,
                dev_observation["observation_id"],
                claim["observation_refs"][1])

    def test_resume_does_not_repeat_completed_side_effects(self):
        controller = self._open()
        first = controller.run(trusted_local=True, max_steps=1)
        observation_count = len(first["observations"])
        spent = first["budget"]["spent"]
        controller.close()
        self.controller = ResearchController(self.run_dir)
        second = self.controller.run(trusted_local=True, max_steps=1)
        self.assertGreaterEqual(len(second["observations"]), observation_count)
        self.assertGreaterEqual(second["budget"]["spent"], spent)
        ids = [o["observation_id"] for o in second["observations"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_claim_semantics_reject_rehashed_evidence_inflation(self):
        controller = self._open()
        controller.run(trusted_local=True, auto_confirm=True)
        conn = controller.store._unsafe_conn
        row = conn.execute("SELECT seq,payload FROM events WHERE kind='claim'").fetchone()
        self.assertIsNotNone(row)
        tampered = json.loads(row[1])
        tampered["oriented_delta"] += 1.0
        conn.execute("UPDATE events SET payload=? WHERE seq=?",
                     (json.dumps(tampered), row[0]))
        snapshot = dict(tampered)
        snapshot.pop("status")
        conn.execute("UPDATE snapshots SET payload=? WHERE kind='claim'",
                     (json.dumps(snapshot),))
        self._rehash_research_events(conn)
        integrity = controller.store.verify()
        self.assertFalse(integrity["ok"])
        self.assertIn("Claim 与确认依据不一致", integrity["reason"])

    def test_claim_semantics_reject_rehashed_failed_run(self):
        controller = self._open()
        claim = controller.run(trusted_local=True, auto_confirm=True)["claims"][0]
        candidate = controller.store.get("observation", claim["observation_refs"][1])
        run_id = candidate["run_id"]
        conn = controller.store._unsafe_conn
        row = conn.execute(
            "SELECT seq,payload FROM events WHERE kind='run' AND entity_id=? "
            "AND event_type='status' ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
        changed = json.loads(row[1])
        self.assertEqual("succeeded", changed["to"])
        changed["to"] = "infrastructure_failed"
        conn.execute("UPDATE events SET payload=? WHERE seq=?",
                     (json.dumps(changed), row[0]))
        conn.execute("UPDATE snapshots SET status=? WHERE kind='run' AND entity_id=?",
                     ("infrastructure_failed", run_id))
        self._rehash_research_events(conn)
        integrity = controller.store.verify()
        self.assertFalse(integrity["ok"])
        self.assertIn("Claim 与确认依据不一致", integrity["reason"])

    @staticmethod
    def _rehash_research_events(conn):
        previous = "0" * 64
        last_seq = 0
        for last_seq, kind, entity_id, event_type, payload, writer in conn.execute(
                "SELECT seq,kind,entity_id,event_type,payload,writer FROM events "
                "ORDER BY seq").fetchall():
            event_hash = digest({"kind": kind, "entity_id": entity_id,
                                 "event_type": event_type, "payload": json.loads(payload),
                                 "writer": writer, "previous": previous})
            conn.execute("UPDATE events SET previous_hash=?,hash=? WHERE seq=?",
                         (previous, event_hash, last_seq))
            previous = event_hash
        conn.execute("UPDATE store_meta SET value=? WHERE key='chain_head'",
                     (json.dumps([last_seq, previous]),))
        conn.commit()

    def test_manifest_tamper_blocks_resume(self):
        controller = self._open()
        controller.close()
        self.controller = None
        manifest = read_json(self.run_dir / "research.json")
        manifest["min_meaningful_effect"] = 0
        from popper.core import write_json
        write_json(self.run_dir / "research.json", manifest)
        with self.assertRaisesRegex(ProtocolError, "manifest 已修改"):
            ResearchController(self.run_dir)

    def test_registered_scoring_implementation_cannot_change_mid_study(self):
        controller = self._open()
        self.assertEqual(scoring_code_hash(), controller.manifest["scoring_code_sha256"])
        with patch("popper.research.controller.scoring_code_hash", return_value="0" * 64):
            with self.assertRaisesRegex(ProtocolError, "计分实现"):
                controller.status()

    def test_legacy_study_is_readable_but_cannot_mix_new_scoring_semantics(self):
        # Build a historical manifest through the normal registration path, so
        # its event chain and scope hash remain valid without a scorer binding.
        from popper.core import write_json as original_write_json

        def write_legacy(path, value):
            if Path(path).name == "research.json":
                value = {k: v for k, v in value.items() if k != "scoring_code_sha256"}
            return original_write_json(path, value)

        with patch("popper.research.controller.write_json", side_effect=write_legacy):
            controller = self._open()
        before = controller.status()
        self.assertTrue(before["integrity"]["ok"])
        candidate = before["candidates"][0]["hypothesis_id"]
        for action in (lambda: controller.run(trusted_local=True),
                       lambda: controller.implement(candidate),
                       lambda: controller.confirm(trusted_local=True)):
            with self.assertRaisesRegex(ProtocolError, "只允许读取和审计"):
                action()
        after = controller.status()
        self.assertEqual(before["budget"], after["budget"])
        self.assertEqual(before["integrity"], after["integrity"])
        self.assertEqual([], after["observations"])

    def test_evaluation_receipt_tamper_blocks_further_research(self):
        controller = self._open()
        status = controller.run(trusted_local=True, max_steps=0)
        baseline = status["observations"][0]
        Path(baseline["artifact_id"]).write_text('{"mean": 999}', encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "完整性"):
            controller.status()

    def test_missing_sandbox_backend_points_to_trusted_local(self):
        """C7：本平台没有沙箱后端时，要让用户看到可行路径而不是两边都被堵死。"""
        controller = self._open()
        calls = {
            "run --sandbox": lambda: controller.run(sandboxed=True),
            "run --sandbox --autonomous-code": lambda: controller.run(
                sandboxed=True, autonomous_code=True),
            "confirm --sandbox": lambda: controller.confirm(sandboxed=True),
        }
        with patch("popper.sandbox.available", return_value=False):
            for label, call in calls.items():
                with self.subTest(label):
                    with self.assertRaises(ProtocolError) as caught:
                        call()
                    message = str(caught.exception)
                    self.assertIn("--trusted-local", message)
                    self.assertIn("--autonomous-code", message)

    def test_available_sandbox_backend_is_not_blocked(self):
        controller = self._open()
        with patch("popper.sandbox.available", return_value=True):
            controller._require_sandbox_backend()


if __name__ == "__main__":
    unittest.main()
