import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, initialize
from popper.research.controller import ResearchController
from popper.research.autonomy import register_experiment
from popper.research.context import build_context
from popper.research.envelope import assert_active, build_envelope, save_envelope
from popper.research.prediction import research_contract
from popper.research.revisions import CodeEdit


class AutonomousDesignTests(unittest.TestCase):
    def test_complete_design_loop_stops_and_does_not_repeat_on_resume(self):
        source = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(source, root / "project", ignore=shutil.ignore_patterns(".popper"))
            initialize(root / "project")
            ResearchController.initialize(root / "project", root / "research", budget_cap=7)
            class Policy:
                name = "offline_design_test"
                calls = 0
                def propose_experiment(self, context):
                    self.calls += 1
                    return {"schema": "popper.experiment-proposal/v2",
                        "action": "new_candidate" if self.calls == 1 else "stop",
                        "mechanism": "二次项待验证", "config": {"degree": 2, "variant": "new"},
                        "intervention": {"kind": "model", "parameters": {"degree": 2}},
                        "contrasts": [{"explanation": "二次关系", "expected_direction": "gte"},
                                      {"explanation": "噪声", "expected_direction": "lt"}],
                        "prediction_contract": context["prediction_contract"],
                        "evidence_refs": [], "rationale": "先检验二次项，再停止", "parent_hypothesis_id": None}
            class Harness:
                name, model, tokens_used = "offline", None, 0
                def propose_revision(self, objective, hypothesis, config, code_files, **kwargs):
                    row = code_files[0]
                    return {"edits": (CodeEdit(row["path"], row["content"].replace(
                        'degree = load(args.config)["degree"]',
                        'degree = int(load(args.config)["degree"])'), row["sha256"]),), "rationale": "实际实现二次干预"}
            policy = Policy()
            controller = ResearchController(root / "research", policy=policy, harness=Harness())
            try:
                controller.worker.executor = lambda command, workspace, env, stdout, stderr, spec: subprocess.run(
                    command, cwd=workspace, env=env, stdout=stdout, stderr=stderr, timeout=spec.timeout_seconds, check=True)
                manifest = controller.manifest
                save_envelope(controller.run_dir, build_envelope(manifest["study_id"], max_dev_runs=4,
                    max_model_tokens=50000, max_wall_clock_minutes=60, code_files=["model.py"],
                    state_version=controller.store.get("study", manifest["study_id"])["state_version"],
                    allowed_actions=("design_dev", "implement", "execute_dev"), contract_sha256=research_contract(manifest)))
                result = controller.run(sandboxed=True, autonomous_design=True, max_steps=4)
                self.assertEqual(2, policy.calls)
                self.assertEqual(2, len([o for o in result["observations"] if o["scope"] == "dev"]))
                self.assertTrue(controller.store.verify()["ok"])
                self.assertEqual("concluded", result["study"]["status"])
                controller.run(sandboxed=True, autonomous_design=True, max_steps=4)
                self.assertEqual(2, policy.calls)
            finally:
                controller.close()

    def test_new_configuration_runs_and_restores_without_rewriting_manifest(self):
        source = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(source, root / "project", ignore=shutil.ignore_patterns(".popper"))
            initialize(root / "project")
            ResearchController.initialize(root / "project", root / "research", budget_cap=8)
            controller = ResearchController(root / "research")
            try:
                original = controller.manifest_path.read_bytes()
                manifest = controller.manifest
                context = build_context(controller.store, manifest["study_id"], manifest)
                context["designs"] = controller.store.list("design")
                envelope = build_envelope(manifest["study_id"], max_dev_runs=4, max_model_tokens=50000,
                    max_wall_clock_minutes=60, code_files=["model.py"],
                    state_version=context["study"]["state_version"],
                    allowed_actions=("design_dev", "implement", "execute_dev"),
                    contract_sha256=research_contract(manifest))
                save_envelope(controller.run_dir, envelope)
                proposal = {"schema": "popper.experiment-proposal/v2", "action": "new_candidate",
                    "mechanism": "待验证：二次项解释残差", "config": {"degree": 2, "variant": "autonomous"},
                    "intervention": {"kind": "model", "parameters": {"degree": 2}},
                    "contrasts": [{"explanation": "二次项", "expected_direction": "gte"},
                                  {"explanation": "随机误差", "expected_direction": "lt"}],
                    "prediction_contract": manifest["prediction_contract"], "evidence_refs": [],
                    "rationale": "改变实际多项式阶数", "parent_hypothesis_id": None}
                registered = register_experiment(controller, proposal, context)
                self.assertEqual(original, controller.manifest_path.read_bytes())
                self.assertTrue(controller.store.verify()["ok"])
                assert_active(envelope, dev_runs_used=0, state_version=999,
                              contract_sha256=research_contract(manifest))
                with self.assertRaises(ProtocolError):
                    assert_active(envelope, dev_runs_used=0, state_version=999, contract_sha256="a" * 64)
                context["designs"] = controller.store.list("design")
                with self.assertRaisesRegex(ProtocolError, "重复"):
                    register_experiment(controller, proposal, context)
                class Harness:
                    name = "offline_test"
                    model = None
                    tokens_used = 0
                    def propose_revision(self, objective, hypothesis, config, code_files, **kwargs):
                        row = code_files[0]
                        return {"edits": (CodeEdit(row["path"], row["content"].replace(
                            'degree = load(args.config)["degree"]',
                            'degree = int(load(args.config)["degree"])'), row["sha256"]),),
                            "rationale": "执行二次干预"}
                controller.harness = Harness()
                controller.worker.executor = lambda command, workspace, env, stdout, stderr, spec: subprocess.run(
                    command, cwd=workspace, env=env, stdout=stdout, stderr=stderr,
                    timeout=spec.timeout_seconds, check=True)
                result = controller.implement(registered["hypothesis_id"], search_node=True,
                                             design_reservation=registered["reservation"])
                self.assertEqual("succeeded", result["status"])
                self.assertLess(result["observation"]["value"], .1)
                self.assertTrue(controller.store.verify()["ok"])
            finally:
                controller.close()
            restored = ResearchController(root / "research")
            try:
                self.assertEqual(proposal["config"], restored.manifest["configs"][registered["hypothesis_id"]])
                self.assertEqual(1, len([r for r in restored.store.list("observation") if r["scope"] == "dev"]))
            finally:
                restored.close()
