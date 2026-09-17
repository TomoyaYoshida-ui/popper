"""Campaign 内置节点测试：真实 Popper 命令接线、幂等与如实跳过。"""
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path

from popper.campaign_nodes import (BUILTIN_NODES, DEFAULT_CAMPAIGN_STEPS,
                                   arbor_node, confirm_node, dev_node,
                                   dispatch_node, freeze_node, idea_node,
                                   literature_node, materialize_node,
                                   propose_node, scoop_node, variant_node)
from popper.core import ProtocolError, initialize
from popper.orchestrator import Orchestrator, CampaignFatal

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


def _make_project(temp_root):
    """复制 quadratic 示例并初始化，返回项目目录。"""
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(EXAMPLE / name, temp_root / name)
    runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](temp_root)
    initialize(temp_root)
    return temp_root


class CampaignPresetTests(unittest.TestCase):
    def test_default_steps_covered_by_builtin_nodes(self):
        """默认编排的每个 key 都有内置节点，且依赖拓扑可排序。"""
        keys = {s["key"] for s in DEFAULT_CAMPAIGN_STEPS}
        self.assertTrue(keys <= set(BUILTIN_NODES))
        run = Orchestrator(Path(tempfile.mkdtemp()))
        run.init("obj", DEFAULT_CAMPAIGN_STEPS)
        _, chain = run.build_graph()
        # 依赖序：dev/freeze/confirm 与 idea→scoop→arbor→dispatch、propose→variant 主线各自有序
        self.assertEqual(chain.index("freeze"), chain.index("dev") + 1)
        self.assertEqual(chain.index("confirm"), chain.index("freeze") + 1)
        self.assertLess(chain.index("idea"), chain.index("scoop"))
        self.assertLess(chain.index("scoop"), chain.index("arbor"))
        self.assertLess(chain.index("arbor"), chain.index("dispatch"))
        self.assertLess(chain.index("propose"), chain.index("variant"))
        self.assertLess(chain.index("confirm"), chain.index("materialize"))

    def test_unknown_step_key_fails_topo_gracefully(self):
        run = Orchestrator(Path(tempfile.mkdtemp()))
        run.init("obj", [{"key": "nope", "needs": []}])
        with self.assertRaises(ProtocolError) as ctx:
            run.run(nodes=BUILTIN_NODES, config={"project": ".", "mode": "trusted_local"})
        self.assertIn("缺执行函数", str(ctx.exception))


class CampaignExperimentNodesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def _state(self, **extra):
        base = {"project": str(self.root), "mode": "trusted_local", "objective": "t"}
        base.update(extra)
        return base

    def test_dev_freeze_confirm_materialize_full_loop(self):
        """确定性闭环：dev→freeze→confirm→materialize 接线真实 Popper 命令。"""
        run_dir = Path(self.temp.name) / "camp"
        materialize = {
            "manuscript": {"title": "c", "abstract": "a",
                           "sections": [{"heading": "方法", "body": "b"}]},
            "template": "md", "disclosure": "nature",
        }
        # dev
        dev = dev_node(run_dir, self._state())
        self.assertEqual("success", dev["outcome"])
        self.assertEqual("searching", dev["phase"])
        self.assertEqual(4, dev["dev_runs"])
        # dev 重跑：幂等（search 复用 prior 缓存）
        dev2 = dev_node(run_dir, self._state())
        self.assertEqual("success", dev2["outcome"])
        # freeze
        frozen = freeze_node(run_dir, self._state())
        self.assertEqual("frozen", frozen["phase"])
        self.assertIn("selected", frozen)
        # freeze 重跑：跳过
        frozen2 = freeze_node(run_dir, self._state())
        self.assertEqual("frozen", frozen2["phase"])
        self.assertIn("跳过", frozen2["note"])
        # dev 在冻结后：跳过
        dev3 = dev_node(run_dir, self._state())
        self.assertIn("跳过", dev3["note"])
        # confirm
        confirmed = confirm_node(run_dir, self._state())
        self.assertEqual("completed", confirmed["phase"])
        self.assertIn("delta", confirmed)
        self.assertIn("status", confirmed)
        # confirm 重跑：跳过
        confirmed2 = confirm_node(run_dir, self._state())
        self.assertEqual("completed", confirmed2["phase"])
        self.assertIn("跳过", confirmed2["note"])
        # materialize
        mat = materialize_node(run_dir, self._state(materialize=materialize))
        self.assertEqual("success", mat["outcome"])
        self.assertTrue((self.root / "deliverables" / "manuscript.md").is_file())
        ws = (self.root / ".popper" / "workspace.json")
        self.assertTrue(ws.is_file())
        self.assertEqual(1, len(json.loads(ws.read_text(encoding="utf-8"))["deliverables"]))
        # materialize 重跑：跳过
        mat2 = materialize_node(run_dir, self._state(materialize=materialize))
        self.assertIn("已物化", mat2["reason"])

    def test_dev_requires_mode(self):
        with self.assertRaises(CampaignFatal):
            dev_node(Path(self.temp.name), {"project": str(self.root)})
        with self.assertRaises(CampaignFatal):
            confirm_node(Path(self.temp.name), {"project": str(self.root)})

    def test_uninitialized_project_fails_clearly(self):
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(CampaignFatal) as ctx:
            dev_node(Path(self.temp.name), {"project": str(empty), "mode": "trusted_local"})
        self.assertIn("未初始化", str(ctx.exception))

    def test_orchestrator_run_with_config_executes_loop(self):
        """Orchestrator.run 注入 config 后整链执行并落 campaign.json 历史。"""
        # vendor 步骤要求 run_dir 位于项目 integrations/runs 内（真实 VendorRegistry 约束）
        camp = self.root / "integrations" / "runs" / "camp2"
        camp.mkdir(parents=True)
        run = Orchestrator(camp)
        run.init("obj", DEFAULT_CAMPAIGN_STEPS)
        result = run.run(nodes=BUILTIN_NODES,
                         config=self._state(_registry=FakeRegistry(), materialize={
                             "manuscript": {"title": "t", "abstract": "a",
                                            "sections": [{"heading": "方法", "body": "b"}]},
                             "template": "md", "disclosure": "nature"}))
        self.assertEqual("completed", result["status"])
        self.assertEqual(["confirm", "materialize"], result["steps"][-2:])
        data = run._load()
        self.assertEqual("completed", data["status"])
        self.assertEqual(len(DEFAULT_CAMPAIGN_STEPS), len(data["history"]))
        # 所有历史 outcome 均非挂起
        self.assertTrue(all(h["outcome"] != "pending_approval" for h in data["history"]))
        self.assertNotIn("waiting_approval", data)

    def test_approval_gate_hangs_then_resumes(self):
        """审批点（variant 声明 approval）未满足 → waiting_approval；批准后重跑 → 完成。"""
        camp = self.root / "integrations" / "runs" / "camp3"
        camp.mkdir(parents=True)
        run = Orchestrator(camp)
        run.init("obj", DEFAULT_CAMPAIGN_STEPS)
        (camp / "proposal").mkdir()
        (camp / "proposal" / "proposal.json").write_text(
            json.dumps({"status": "review_required", "edits": []}), encoding="utf-8")
        # 第一轮：真实待审提案存在且不带审批 → 挂起。
        result = run.run(nodes=BUILTIN_NODES,
                         config=self._state(_registry=FakeRegistry()))
        self.assertEqual("waiting_approval", result["status"])
        self.assertEqual("variant", result["pending"]["step"])
        self.assertEqual("proposal_approved", result["pending"]["required"])
        data = run._load()
        self.assertEqual("waiting_approval", data["status"])
        self.assertEqual("variant", data["waiting_approval"]["step"])
        self.assertIn("pending_approval", [h["outcome"] for h in data["history"]])
        # 恢复：带审批重跑 → 物化已审提案并完成。
        reg = FakeRegistry()
        result2 = run.run(nodes=BUILTIN_NODES,
                          config=self._state(proposal_approved=True, _registry=reg,
                                             materialize={
                                 "manuscript": {"title": "t", "abstract": "a",
                                                "sections": [{"heading": "方法", "body": "b"}]},
                                 "template": "md", "disclosure": "nature"}))
        self.assertEqual("completed", result2["status"])
        data2 = run._load()
        self.assertEqual("completed", data2["status"])
        self.assertNotIn("waiting_approval", data2)
        self.assertIn("code_materialize", reg.calls)
        # 挂起审计记录保留（如实），但 variant 最终成功执行
        variant_entries = [h for h in data2["history"] if h["step"] == "variant"]
        self.assertTrue(any(h["outcome"] == "success" for h in variant_entries))
        self.assertTrue(any(h["outcome"] == "pending_approval" for h in variant_entries))
        # 挂起不阻塞完成判定
        self.assertEqual(len(DEFAULT_CAMPAIGN_STEPS),
                         len({h["step"] for h in data2["history"]}))

    def test_approval_gate_with_proposal_materializes_variant(self):
        """审批 + proposal 存在 → variant 真正执行 code-materialize。"""
        camp = self.root / "integrations" / "runs" / "camp4"
        camp.mkdir(parents=True)
        run = Orchestrator(camp)
        run.init("obj", DEFAULT_CAMPAIGN_STEPS)
        (camp / "proposal").mkdir(parents=True)
        (camp / "proposal" / "proposal.json").write_text(
            json.dumps({"status": "review_required", "edits": []}),
                                                         encoding="utf-8")
        reg = FakeRegistry()
        result = run.run(nodes=BUILTIN_NODES,
                         config=self._state(proposal_approved=True, _registry=reg,
                                            materialize={
                                "manuscript": {"title": "t", "abstract": "a",
                                               "sections": [{"heading": "方法", "body": "b"}]},
                                "template": "md", "disclosure": "nature"}))
        self.assertEqual("completed", result["status"])
        self.assertIn("code_materialize", reg.calls)

    def test_provisional_research_stops_then_resumes_to_real_proposal_review(self):
        """全文不足不能伪报审批或消耗最终测试集；证据恢复后才生成待审提案。"""
        from popper.core import Experiment
        camp = self.root / "campaign-provisional"
        run = Orchestrator(camp)
        run.init("obj", DEFAULT_CAMPAIGN_STEPS)
        reg = FakeRegistry()
        reg.scoop_phase = "provisional"
        config = self._state(base_url="b", model="m", _registry=reg)
        result = run.run(nodes=BUILTIN_NODES, config=config)
        self.assertEqual("retryable", result["status"])
        self.assertEqual("scoop", result["final_step"])
        self.assertNotIn("waiting_approval", run._load())
        self.assertEqual("provisional", run._load()["history"][-1]["result"]["phase"])
        self.assertNotIn("code_propose", reg.calls)
        self.assertFalse((camp / "proposal" / "proposal.json").is_file())
        exp = Experiment(self.root)
        try:
            self.assertEqual("searching", exp.state()["phase"])
            self.assertEqual([], exp.results())
        finally:
            exp.close()
        reg.scoop_phase = "completed"
        resumed = run.run(nodes=BUILTIN_NODES, config=config)
        self.assertEqual("waiting_approval", resumed["status"])
        self.assertEqual("variant", resumed["pending"]["step"])
        self.assertTrue((camp / "proposal" / "proposal.json").is_file())
        self.assertIn("code_propose", reg.calls)

    def test_allow_provisional_cannot_complete_required_code_research(self):
        camp = self.root / "campaign-allow-provisional"
        run = Orchestrator(camp)
        run.init("obj", DEFAULT_CAMPAIGN_STEPS)
        reg = FakeRegistry()
        reg.scoop_phase = "provisional"
        result = run.run(nodes=BUILTIN_NODES,
                         config=self._state(base_url="b", model="m", _registry=reg,
                                            allow_provisional=True))
        self.assertEqual("retryable", result["status"])
        self.assertEqual("propose", result["final_step"])
        self.assertNotIn("waiting_approval", run._load())
        self.assertNotIn("code_propose", reg.calls)

    def test_real_variant_is_evaluated_and_restored_on_resume(self):
        import difflib
        from popper.core import Experiment, file_hash
        from popper.code_variant import materialize

        camp = self.root / "campaign-variant"
        proposal = camp / "proposal"
        proposal.mkdir(parents=True)
        source = self.root / "model.py"
        original = source.read_text(encoding="utf-8")
        replacement = original.replace('degree = load(args.config)["degree"]', 'degree = 2')
        self.assertNotEqual(original, replacement)
        (proposal / "proposal.json").write_text(json.dumps({
            "status": "review_required", "adapter": "code-proposal-v1",
            "hypothesis": "Quadratic features reduce error",
            "edits": [{"path": "model.py", "original_sha256": file_hash(source),
                       "replacement": replacement}]}), encoding="utf-8")
        (proposal / "proposal.diff").write_text("".join(difflib.unified_diff(
            original.splitlines(True), replacement.splitlines(True),
            fromfile="a/model.py", tofile="b/model.py")), encoding="utf-8")

        class RealMaterializer(FakeRegistry):
            def code_materialize(self, proposal_run_dir, experiment_dir,
                                 config_index=0, approved=False):
                return materialize(Path(proposal_run_dir), Path(experiment_dir),
                                   Path(proposal_run_dir) / "variant-project",
                                   config_index, approved)

        run = Orchestrator(camp)
        run.init("variant regression", DEFAULT_CAMPAIGN_STEPS)
        config = self._state(proposal_approved=True, _registry=RealMaterializer(),
                             materialize={"manuscript": {"title": "variant", "abstract": "a",
                                 "sections": [{"heading": "Results", "body": "b"}]},
                                 "template": "md", "disclosure": "nature"})
        self.assertEqual("completed", run.run(nodes=BUILTIN_NODES, config=config)["status"])
        derived = proposal / "variant-project"
        self.assertTrue((derived / "deliverables" / "manuscript.md").is_file())
        self.assertFalse((self.root / "deliverables" / "manuscript.md").exists())
        exp = Experiment(derived)
        try:
            self.assertEqual("completed", exp.state()["phase"])
            scores = exp.results("dev")
            self.assertEqual(2, len(scores))
            self.assertGreater(scores[0]["mean"], scores[1]["mean"])
            before = exp.results()
            exp.replay()
        finally:
            exp.close()
        # Recreate the orchestrator as the CLI does; cache hit must restore routing.
        resumed = Orchestrator(camp)
        self.assertEqual("completed", resumed.run(nodes=BUILTIN_NODES, config=config)["status"])
        exp = Experiment(derived)
        try:
            self.assertEqual(before, exp.results())
        finally:
            exp.close()
        source_exp = Experiment(self.root)
        try:
            self.assertEqual("searching", source_exp.state()["phase"])
            self.assertEqual([], source_exp.results())
        finally:
            source_exp.close()
        self.assertEqual(original, source.read_text(encoding="utf-8"))



class FakeRegistry:
    """最小 fake：不触网，仅记录调用并返回契约结构。"""

    def __init__(self):
        self.calls = []

    def idea_next(self, run_dir, query, base_url, model, evaluation_contract=None):
        self.calls.append("idea_next")
        target = Path(run_dir)
        (target / "phase3_revise").mkdir(parents=True, exist_ok=True)
        (target / "phase3_revise" / "final_candidate.json").write_text(
            json.dumps({"evaluation_contract": evaluation_contract, "title": "t", "core_mechanism": "m",
                        "falsification_prediction": "p"}), encoding="utf-8")
        return {"process": "automated", "navigation": {"step": "phase3_revise"}}

    def paper_search(self, run_dir, queries, start_year, end_year, **kw):
        self.calls.append("paper_search")
        out = Path(run_dir) / "literature"
        out.mkdir(parents=True, exist_ok=True)
        (out / "search-x.json").write_text(
            json.dumps({"papers": [{"id": "1"}], "cache": "miss"}), encoding="utf-8")
        return {"papers": [{"id": "1"}], "cache": "miss", "artifact": str(out / "search-x.json")}

    def scoop_run(self, idea_run_dir, scoop_run_dir, base_url, model, start_year, end_year,
                  **kw):
        self.calls.append("scoop_run")
        phase = getattr(self, "scoop_phase", "completed")
        target = Path(scoop_run_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "step7.json").write_text(json.dumps({"status": phase}), encoding="utf-8")
        return {"phase": phase, "last_step": 7}

    def arbor_init(self, run_dir, objective, dev_eval, test_eval, **kw):
        self.calls.append("arbor_init")
        (Path(run_dir) / ".arbor").mkdir(parents=True, exist_ok=True)
        (Path(run_dir) / ".arbor" / "tree.json").write_text(
            json.dumps({"nodes": {"n0": {"id": "n0", "status": "pending"}}, "root": "n0"}),
            encoding="utf-8")
        (Path(run_dir) / ".arbor" / "run.json").write_text("{}", encoding="utf-8")

    def idea_to_arbor(self, idea_run_dir, arbor_run_dir, parent="n0"):
        if not getattr(self, "_idea_linked", False):
            self.calls.append("idea_to_arbor")
            self._idea_linked = True
            return {"status": "linked"}
        return {"status": "already_linked"}

    def scoop_to_arbor(self, idea_run_dir, scoop_run_dir, arbor_run_dir, parent="n0",
                       allow_provisional=False):
        if not getattr(self, "_scoop_linked", False):
            self.calls.append("scoop_to_arbor")
            self._scoop_linked = True
            return {"status": "linked"}
        return {"status": "already_linked"}

    def arbor_dispatch(self, idea_run_dir, scoop_run_dir, arbor_run_dir, experiment_dir,
                       node, base_url=None, model=None, trusted_local=False,
                       allow_provisional=False, llm=None):
        if not getattr(self, "_dispatched", False):
            self.calls.append("arbor_dispatch")
            self._dispatched = True
            return {"status": "implementable", "mapping": {"candidate_index": 1}}
        return {"status": "already_dispatched", "mapping": {"candidate_index": 1}}

    def code_propose(self, idea_run_dir, scoop_run_dir, proposal_run_dir, experiment_dir,
                     base_url=None, model=None, llm=None):
        if not getattr(self, "_proposed", False):
            self.calls.append("code_propose")
            self._proposed = True
            target = Path(proposal_run_dir)
            target.mkdir(parents=True, exist_ok=True)
            (target / "proposal.json").write_text(
                json.dumps({"status": "review_required", "edits": []}), encoding="utf-8")
            return {"status": "review_required", "proposal": str(target / "proposal.json")}
        return {"status": "review_required"}

    def code_materialize(self, proposal_run_dir, experiment_dir, config_index=0,
                         approved=False):
        self.calls.append("code_materialize")
        return {"status": "ready", "cache": "miss", "project": str(experiment_dir)}

    def arbor_state(self, run_dir):
        return {"nodes": [{"id": "n0"}], "frontier": []}


class CampaignVendorNodesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name) / "camp"
        self.run_dir.mkdir(parents=True)
        self.registry = FakeRegistry()

    def tearDown(self):
        self.temp.cleanup()

    def test_idea_skipped_without_llm_honest(self):
        res = idea_node(self.run_dir, {"objective": "q"})
        self.assertTrue(res["skipped"])
        self.assertIn("--base-url", res["reason"])
        # 提供 LLM 后走 fake：落 canonical candidate
        res2 = idea_node(self.run_dir, {"objective": "q", "base_url": "b", "model": "m",
                                        "_registry": self.registry})
        self.assertEqual("automated", res2["process"])
        self.assertTrue((self.run_dir / "idea" / "phase3_revise" / "final_candidate.json").is_file())
        # 已有候选：复用
        res3 = idea_node(self.run_dir, {"objective": "q", "_registry": self.registry})
        self.assertEqual("existing", res3["process"])

    def test_literature_skipped_without_queries(self):
        res = literature_node(self.run_dir, {"mode": "trusted_local"})
        self.assertTrue(res["skipped"])
        res2 = literature_node(self.run_dir, {"mode": "trusted_local", "queries": ["q1"],
                                              "start_year": 2015, "end_year": 2026,
                                              "_registry": self.registry})
        self.assertEqual(1, res2["papers"])
        self.assertEqual(["paper_search"], self.registry.calls)

    def test_literature_requires_trusted_local_mode(self):
        for state in ({}, {"mode": "sandboxed" if False else "sandboxed"}):
            res = literature_node(self.run_dir, {**state, "queries": ["q1"],
                                                 "_registry": self.registry})
            self.assertTrue(res["skipped"])
            self.assertIn("trusted-local", res["reason"])
        self.assertEqual([], self.registry.calls)

    def test_scoop_skipped_without_llm(self):
        res = scoop_node(self.run_dir, {"mode": "trusted_local"})
        self.assertTrue(res["skipped"])
        res2 = scoop_node(self.run_dir, {"mode": "trusted_local", "base_url": "b",
                                         "model": "m", "_registry": self.registry})
        self.assertEqual("completed", res2["phase"])

    def test_scoop_requires_trusted_local_mode(self):
        from unittest.mock import Mock
        registry = Mock()
        result = scoop_node(self.run_dir, {"mode": "sandboxed", "base_url": "b",
                                           "model": "m", "_registry": registry})
        self.assertTrue(result["skipped"])
        self.assertIn("trusted-local", result["reason"])
        registry.scoop_run.assert_not_called()

    def test_scoop_requires_report_even_when_vendor_reports_completed(self):
        from unittest.mock import Mock
        registry = Mock()
        registry.scoop_run.return_value = {"phase": "completed", "last_step": 7}
        result = scoop_node(self.run_dir, {"mode": "trusted_local", "base_url": "b",
                                           "model": "m", "_registry": registry})
        self.assertEqual("retryable", result["outcome"])
        self.assertIn("step7.json", result["reason"])

    def test_arbor_inits_tree_and_bridges_existing_evidence(self):
        # 先造 idea candidate + scoop report
        idea_dir = self.run_dir / "idea"
        (idea_dir / "phase3_revise").mkdir(parents=True)
        (idea_dir / "phase3_revise" / "final_candidate.json").write_text("{}", encoding="utf-8")
        scoop_dir = self.run_dir / "scoop"
        scoop_dir.mkdir(parents=True)
        (scoop_dir / "step7.json").write_text(
            json.dumps({"status": "completed"}), encoding="utf-8")
        res = arbor_node(self.run_dir, {"objective": "o", "_registry": self.registry})
        self.assertEqual(1, res["nodes"])
        self.assertEqual(["arbor_init", "idea_to_arbor", "scoop_to_arbor"], self.registry.calls)
        # 幂等：已有 tree.json 不重复 init；桥接仍幂等返回
        res2 = arbor_node(self.run_dir, {"objective": "o", "_registry": self.registry})
        self.assertEqual(1, res2["nodes"])
        self.assertEqual(["arbor_init", "idea_to_arbor", "scoop_to_arbor"],
                         self.registry.calls)

    def test_arbor_without_evidence_still_inits_tree(self):
        res = arbor_node(self.run_dir, {"objective": "o", "_registry": self.registry})
        self.assertEqual(1, res["nodes"])
        self.assertEqual([], res["linked"])

    def _seed_evidence(self):
        """造 idea candidate + completed scoop report + arbor 桥接节点。"""
        idea_dir = self.run_dir / "idea"
        (idea_dir / "phase3_revise").mkdir(parents=True)
        (idea_dir / "phase3_revise" / "final_candidate.json").write_text("{}", encoding="utf-8")
        scoop_dir = self.run_dir / "scoop"
        scoop_dir.mkdir(parents=True)
        (scoop_dir / "step7.json").write_text(
            json.dumps({"status": "completed"}), encoding="utf-8")
        arbor_dir = self.run_dir / "arbor"
        (arbor_dir / ".popper-integration").mkdir(parents=True)
        (arbor_dir / ".popper-integration" / "idea-arbor-links.json").write_text(
            json.dumps({"schema_version": "1.0",
                        "links": [{"node_id": "n1", "parent": "n0"}]}), encoding="utf-8")

    def test_dispatch_skips_without_prereqs(self):
        # 无 LLM → 跳过
        res = dispatch_node(self.run_dir, {"mode": "trusted_local"})
        self.assertTrue(res["skipped"])
        self.assertIn("--base-url", res["reason"])
        # 非 trusted_local → 跳过
        res2 = dispatch_node(self.run_dir, {"mode": "sandboxed", "base_url": "b", "model": "m"})
        self.assertTrue(res2["skipped"])
        self.assertIn("--trusted-local", res2["reason"])
        # 有 LLM 但无 Scoop 报告 → 跳过
        res3 = dispatch_node(self.run_dir, {"mode": "trusted_local", "base_url": "b",
                                            "model": "m", "_registry": self.registry})
        self.assertTrue(res3["skipped"])
        self.assertIn("Scoop", res3["reason"])

    def test_dispatch_happy_path_and_idempotent(self):
        self._seed_evidence()
        proj = _make_project(Path(tempfile.mkdtemp()))
        state = {"mode": "trusted_local", "base_url": "b", "model": "m",
                 "project": str(proj), "_registry": self.registry}
        res = dispatch_node(self.run_dir, state)
        self.assertEqual("implementable", res["status"])
        self.assertEqual(1, res["candidate_index"])
        self.assertEqual(["arbor_dispatch"], self.registry.calls)
        # 幂等：dispatches 缓存 → 不重复调用，返回既有映射（模拟真实 execution_key 缓存）
        res2 = dispatch_node(self.run_dir, state)
        self.assertEqual(["arbor_dispatch"], self.registry.calls)
        self.assertEqual("already_dispatched", res2["status"])

    def test_propose_skips_without_prereqs(self):
        res = propose_node(self.run_dir, {})
        self.assertTrue(res["skipped"])
        self.assertIn("--base-url", res["reason"])
        # 无 LLM 之外的缺 candidate/report
        res2 = propose_node(self.run_dir, {"base_url": "b", "model": "m",
                                           "_registry": self.registry})
        self.assertEqual("retryable", res2["outcome"])
        # provisional scoop 报告 → 阻断，不能假报该科研步骤完成。
        idea_dir = self.run_dir / "idea"
        (idea_dir / "phase3_revise").mkdir(parents=True)
        (idea_dir / "phase3_revise" / "final_candidate.json").write_text("{}", encoding="utf-8")
        scoop_dir = self.run_dir / "scoop"
        scoop_dir.mkdir(parents=True)
        (scoop_dir / "step7.json").write_text(
            json.dumps({"status": "provisional"}), encoding="utf-8")
        res3 = propose_node(self.run_dir, {"base_url": "b", "model": "m",
                                           "_registry": self.registry})
        self.assertEqual("retryable", res3["outcome"])
        self.assertIn("completed", res3["reason"])

    def test_propose_happy_path(self):
        self._seed_evidence()
        proj = _make_project(Path(tempfile.mkdtemp()))
        res = propose_node(self.run_dir, {"base_url": "b", "model": "m",
                                          "project": str(proj), "_registry": self.registry})
        self.assertEqual("review_required", res["status"])
        self.assertTrue((self.run_dir / "proposal" / "proposal.json").is_file())
        self.assertEqual(["code_propose"], self.registry.calls)

    def test_propose_requires_actual_review_artifact(self):
        from unittest.mock import Mock
        self._seed_evidence()
        registry = Mock()
        registry.code_propose.return_value = {"status": "review_required"}
        result = propose_node(self.run_dir, {"base_url": "b", "model": "m",
                                             "project": ".", "_registry": registry})
        self.assertEqual("retryable", result["outcome"])
        self.assertIn("proposal.json", result["reason"])

    def test_variant_requires_approval_and_proposal(self):
        res = variant_node(self.run_dir, {})
        self.assertTrue(res["skipped"])
        self.assertIn("缺少 proposal", res["reason"])
        # 有审批但无 proposal → 跳过
        res2 = variant_node(self.run_dir, {"project": ".", "proposal_approved": True,
                                           "_registry": self.registry})
        self.assertTrue(res2["skipped"])
        self.assertIn("proposal", res2["reason"])
        # 模型研究模式没有提案不能退化为配置队列完成。
        res3 = variant_node(self.run_dir, {"base_url": "b", "model": "m"})
        self.assertEqual("retryable", res3["outcome"])
        proposal = self.run_dir / "proposal"
        proposal.mkdir()
        (proposal / "proposal.json").write_text(
            json.dumps({"status": "review_required", "edits": []}), encoding="utf-8")
        res4 = variant_node(self.run_dir, {})
        self.assertEqual("retryable", res4["outcome"])
        self.assertIn("--proposal-approved", res4["reason"])

    def test_variant_happy_path(self):
        proposal = self.run_dir / "proposal"
        proposal.mkdir(parents=True)
        (proposal / "proposal.json").write_text(
            json.dumps({"status": "review_required", "edits": []}), encoding="utf-8")
        res = variant_node(self.run_dir, {"project": ".", "proposal_approved": True,
                                          "config_index": 2, "_registry": self.registry})
        self.assertEqual("ready", res["status"])
        self.assertEqual(["code_materialize"], self.registry.calls)

    def _seed_provisional(self):
        """造 idea candidate + provisional scoop report。"""
        idea_dir = self.run_dir / "idea"
        (idea_dir / "phase3_revise").mkdir(parents=True)
        (idea_dir / "phase3_revise" / "final_candidate.json").write_text("{}", encoding="utf-8")
        scoop_dir = self.run_dir / "scoop"
        scoop_dir.mkdir(parents=True)
        (scoop_dir / "step7.json").write_text(
            json.dumps({"status": "provisional", "level": "low"}), encoding="utf-8")

    def test_arbor_provisional_requires_allow_flag(self):
        self._seed_provisional()
        # 未放行 → scoop 桥接记为 skipped（如实），idea 桥接正常
        res = arbor_node(self.run_dir, {"objective": "o", "_registry": self.registry})
        scoop_entry = next(l for l in res["linked"] if l["from"] == "scoop")
        self.assertEqual("skipped", scoop_entry["status"])
        self.assertNotIn("scoop_to_arbor", self.registry.calls)
        # 放行 → 桥接执行
        res2 = arbor_node(self.run_dir, {"objective": "o", "_registry": self.registry,
                                         "allow_provisional": True})
        scoop2 = next(l for l in res2["linked"] if l["from"] == "scoop")
        self.assertEqual("linked", scoop2["status"])
        self.assertIn("scoop_to_arbor", self.registry.calls)

    def test_dispatch_provisional_requires_allow_flag(self):
        self._seed_provisional()
        arbor_dir = self.run_dir / "arbor"
        (arbor_dir / ".popper-integration").mkdir(parents=True)
        (arbor_dir / ".popper-integration" / "idea-arbor-links.json").write_text(
            json.dumps({"schema_version": "1.0",
                        "links": [{"node_id": "n1"}]}), encoding="utf-8")
        proj = _make_project(Path(tempfile.mkdtemp()))
        state = {"mode": "trusted_local", "base_url": "b", "model": "m",
                 "project": str(proj), "_registry": self.registry}
        # 未放行 → 跳过
        res = dispatch_node(self.run_dir, state)
        self.assertTrue(res["skipped"])
        self.assertIn("provisional", res["reason"])
        # 放行 → 执行
        res2 = dispatch_node(self.run_dir, {**state, "allow_provisional": True})
        self.assertEqual("implementable", res2["status"])
        self.assertIn("arbor_dispatch", self.registry.calls)


if __name__ == "__main__":
    unittest.main()
