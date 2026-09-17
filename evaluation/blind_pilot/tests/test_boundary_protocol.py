import shutil
import tempfile
import unittest
from pathlib import Path

from popper.capabilities import extra_available
from popper.core import initialize, write_json
from popper.research.actions import RUN_EXPERIMENT, ActionProposal
from popper.research.confirmation_contracts import (load_private_key, public_key_b64)
from popper.research.confirmation_runner import run_confirmation_bundle
from popper.research.confirmation_service import HoldoutService
from popper.research.controller import ResearchController
from evaluation.blind_pilot.audit import _infer_conclusion, audit_cell
from evaluation.blind_pilot.tasks.time_series_tasks import build_t08_ts_boundary


class BoundaryPolicy:
    name = "boundary_test_policy"
    model = None

    def choose(self, context):
        chosen = next(item for item in context["candidates"]
                      if item["status"] == "untested" and item["config"].get("degree") == 2)
        return ActionProposal(RUN_EXPERIMENT, "Test the registered quadratic intervention.",
                              chosen["hypothesis_id"], source=self.name)

    def reflect(self, context):
        action = context["allowed_actions"][0]
        return {"action": action,
                "rationale": "The global effect is small while slice s0 crosses the threshold.",
                "alternative_explanation": "The intervention applies only in a restricted region.",
                "next_hypothesis_id": None, "revision": None,
                "evidence_refs": context["evidence_refs"]}

    def propose_revision(self, objective, hypothesis, config, code_files,
                         parent_revision=None, failure=None):
        return {"edits": (), "rationale": "The registered source already implements degree=2."}


class BoundaryProtocolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bp-"))
        self.cell = self.root / "cell"
        self.project = self.cell / "project"
        self.gold = self.root / "gold"
        self.project.mkdir(parents=True)
        build_t08_ts_boundary(self.project, self.gold)
        initialize(self.project)
        self.runner_key = load_private_key(self.root / "runner.pem", create=True)
        self.service = HoldoutService(self.root / "svc")
        from popper.core import read_json
        spec = read_json(self.project / "experiment.json")
        self.contract = self.service.register(
            dataset_id="t08-boundary", dataset_version="1", evaluation_group="signed-slices",
            train_path=self.project / spec["train"], dev_path=self.project / spec["dev"],
            holdout_path=self.project / spec["test"], evaluator_id="mse-v1",
            seeds=spec["seeds"], min_effect=spec["min_improvement"],
            runtime_id="boundary-integration", runner_public_key=public_key_b64(self.runner_key),
            analysis_slices=spec["analysis_slices"])

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.root, ignore_errors=True)

    @unittest.skipUnless(extra_available("ml"),
                         "ml extra 未安装（scikit-learn）：真实切片的分数来自跑模型代码")
    def test_real_slice_scores_drive_auditable_boundary_stop(self):
        run_dir = self.cell / "research"
        ResearchController.initialize(
            self.project, run_dir, policy=BoundaryPolicy(), confirmation_contract=self.contract,
            confirmation_public_key=self.service.public_key)
        controller = ResearchController(run_dir, policy=BoundaryPolicy())
        try:
            status = controller.run(sandboxed=True, autonomous_code=True, max_steps=1)
            self.assertEqual("ready_for_confirmation", status["phase"])
            prepared = controller.prepare_external_confirmation()
            ticket = self.service.begin(prepared["submission"])
            executed = run_confirmation_bundle(
                prepared["bundle_dir"], ticket, self.service.features(ticket),
                self.service.public_key, self.runner_key, self.root / "xr")
            result = self.service.complete(ticket, executed["runner_receipt"],
                                           executed["predictions"])
            status = controller.accept_external_confirmation(result)
        finally:
            controller.close()
        write_json(self.cell / "cell-summary.json", {"phase": status["phase"]})

        audit = audit_cell(self.cell, self.gold)

        self.assertEqual("concluded", status["phase"])
        self.assertEqual(12, len(status["observations"]))
        self.assertEqual(0, status["code_revisions"])
        self.assertEqual("request_scope_boundary_confirmation",
                         status["reflections"][-1]["action"])
        self.assertEqual(6, len(status["reflections"][-1]["evidence_refs"]))
        self.assertEqual("conclude_scope_boundary", status["decisions"][-1]["action"])
        self.assertEqual(6, len(status["decisions"][-1]["observation_refs"]))
        self.assertTrue(audit["ok"], audit)
        self.assertTrue(audit["conclusion"]["matched"], audit)
        self.assertEqual("scope_boundary_or_counterexample",
                         audit["conclusion"]["inferred"])


class EvidenceDrivenBoundaryTests(unittest.TestCase):
    """边界结论应可由预注册切片 + 签名 holdout 证据判定，而无需策略显式声明。"""

    def _manifest(self):
        return {
            "candidate_hypothesis_ids": ["c2", "c3"],
            "control_hypothesis_id": "c1",
            "min_meaningful_effect": 0.08,
            "metric": {"name": "mse", "direction": "max"},
        }

    def _boundary_observations(self):
        rows = []
        # dev 全局：小幅，不越阈值；但 confirmation 上全局低于阈值、两个预注册
        # 子群跨越阈值 → 证据本身呈现边界形。
        spec = [
            ("dev", "c1", 0.500), ("dev", "c2", 0.545), ("dev", "c3", 0.510),
            ("dev:slice:s0", "c1", 0.500), ("dev:slice:s0", "c2", 0.650),
            ("dev:slice:s1", "c1", 0.500), ("dev:slice:s1", "c2", 0.515),
            ("confirmation", "c1", 0.510), ("confirmation", "c2", 0.560),
            ("confirmation", "c3", 0.505),
            ("confirmation:slice:s0", "c1", 0.510), ("confirmation:slice:s0", "c2", 0.640),
            ("confirmation:slice:s1", "c1", 0.510), ("confirmation:slice:s1", "c2", 0.520),
            ("confirmation:slice:s0", "c3", 0.505), ("confirmation:slice:s1", "c3", 0.506),
        ]
        observations = []
        for index, (scope, hypothesis_id, value) in enumerate(spec):
            observations.append({"observation_id": f"obs-{index}", "scope": scope,
                                 "hypothesis_id": hypothesis_id, "value": value})
        return observations

    def test_boundary_shaped_evidence_is_concluded_without_explicit_declaration(self):
        observations = self._boundary_observations()
        obs = {row["observation_id"]: row for row in observations}
        c2_slices = ["confirmation:slice:s0", "confirmation:slice:s1"]
        required = set()
        for scope in ["confirmation", *c2_slices]:
            for hid in ("c1", "c2"):
                required.add((scope, hid))
        refs = sorted(obs_id for obs_id, row in obs.items() if (row["scope"], row["hypothesis_id"]) in required)
        # 策略在签名证据上以普通 stop 结束，未发出 conclude_scope_boundary。
        decisions = [{"decision_id": "d-stop", "action": "stop", "actor": "controller",
                      "observation_refs": refs}]
        inferred = _infer_conclusion(self._manifest(), observations, decisions, {},
                                     "scope_boundary_or_counterexample", "concluded")
        self.assertTrue(inferred["matched"])
        self.assertEqual("scope_boundary_or_counterexample", inferred["inferred"])
        self.assertEqual("derived", inferred["boundary_basis"]["level"])
        self.assertEqual("c2", inferred["boundary_basis"]["candidate_id"])

    def test_explicit_boundary_declaration_keeps_explicit_level(self):
        observations = self._boundary_observations()
        obs = {row["observation_id"]: row for row in observations}
        c2_slices = ["confirmation:slice:s0", "confirmation:slice:s1"]
        required = set()
        for scope in ["confirmation", *c2_slices]:
            for hid in ("c1", "c2"):
                required.add((scope, hid))
        refs = sorted(o for o, r in obs.items() if (r["scope"], r["hypothesis_id"]) in required)
        decisions = [{"decision_id": "d-b", "action": "conclude_scope_boundary",
                      "actor": "controller", "observation_refs": refs}]
        inferred = _infer_conclusion(self._manifest(), observations, decisions, {},
                                     "scope_boundary_or_counterexample", "concluded")
        self.assertTrue(inferred["matched"])
        self.assertEqual("explicit", inferred["boundary_basis"]["level"])

    def test_boundary_shaped_evidence_without_signed_global_is_not_concluded(self):
        observations = self._boundary_observations()
        # 去掉 confirmation 全局（无签名 holdout），不应被判 boundary。
        observations = [r for r in observations if r["scope"] != "confirmation"]
        decisions = [{"decision_id": "d-stop", "action": "stop", "actor": "controller",
                      "observation_refs": []}]
        inferred = _infer_conclusion(self._manifest(), observations, decisions, {},
                                     "scope_boundary_or_counterexample", "concluded")
        self.assertNotEqual("scope_boundary_or_counterexample", inferred["inferred"])


if __name__ == "__main__":
    unittest.main()
