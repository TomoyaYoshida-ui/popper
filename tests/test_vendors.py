import json
import io
import runpy
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from popper.capabilities import vendor_corpus_status
from popper.core import Experiment, ProtocolError, file_hash, initialize
from popper.vendors import VendorRegistry
from popper.scoop import ScoopRun, fetch_pdf_text


PROJECT = Path(__file__).resolve().parents[1]
QUADRATIC = PROJECT / "examples" / "quadratic"


VENDOR_CORPUS_OK, VENDOR_CORPUS_DETAIL = vendor_corpus_status()


@unittest.skipUnless(VENDOR_CORPUS_OK,
                     "真实开源语料不可用（`integrations/vendors.json` 的 source_root 指向与本项目"
                     "同级的上游仓库）：" + VENDOR_CORPUS_DETAIL +
                     "；注册表本身的逻辑由 tests/test_vendors_registry.py 用仓库内合成 fixture 覆盖")
class VendorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = VendorRegistry(PROJECT)

    def test_registry_verifies_licenses_entrypoints_and_hashes(self):
        result = self.registry.inspect()
        self.assertEqual("verified", result["status"])
        self.assertEqual({"arbor", "ai_research_autoresearch", "ai_research_ml_training",
                          "researchstudio_idea", "researchstudio_paper_search",
                          "researchstudio_scoop_check"},
                         {component["id"] for component in result["components"]})
        self.assertTrue(all(component["license"] == "MIT" for component in result["components"]))
        self.assertTrue(all(component["files_verified"] >= 2 for component in result["components"]))
        self.assertEqual("protocol", next(c for c in result["components"]
                                          if c["id"] == "researchstudio_scoop_check")["kind"])

    def test_researchstudio_next_is_called_through_pinned_adapter(self):
        result = self.registry.idea_next(
            "integrations/runs/test-idea-next",
            "Improve evidence-grounded scientific hypothesis generation")
        self.assertEqual("idea-next-v1", result["adapter"])
        self.assertEqual("researchstudio_idea", result["component"])
        self.assertIn("STATE  : Fresh run", result["stdout"])
        self.assertIn("STEP   : Phase 0", result["stdout"])
        self.assertEqual("llm_subagent", result["navigation"]["type"])
        self.assertIn("Fresh run", result["navigation"]["state"])

    def test_idea_llm_subagent_falls_back_to_manual_without_llm(self):
        # 无 --base-url/--model：导航到 llm_subagent 时不写产物、不失败，标记 manual。
        result = self.registry.idea_next(
            "integrations/runs/test-idea-next",
            "Improve evidence-grounded scientific hypothesis generation")
        self.assertEqual("manual", result["process"]["process"])
        self.assertFalse((PROJECT / "integrations" / "runs" / "test-idea-next"
                          / "phase3_revise" / "final_candidate.json").exists())

    def test_arbor_initializes_and_validates_real_vendor_tree(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory:
            result = self.registry.arbor_init(
                directory, "Improve a measurable fixture", "python dev.py", "python test.py",
                branching=2, max_depth=2, budget=4)
            self.assertEqual("arbor-tree-v1", result["adapter"])
            self.assertTrue((Path(directory) / ".arbor" / "tree.json").is_file())
            observed = self.registry.arbor_read(directory, "observe")
            self.assertIn("Improve a measurable fixture", observed["stdout"])
            added = self.registry.arbor_add(
                directory, "n0", "A pinned vendor adapter preserves research state")
            self.assertEqual("n1", added["state"]["frontier"][0]["id"])
            evidence = self.registry.arbor_evidence(
                directory, "n1", 0.8, "Adapter state is readable",
                "Structured state prevents text-only integration", "refs/heads/candidate")
            self.assertEqual("executed", evidence["state"]["nodes"][1]["status"])
            propagated = self.registry.arbor_propagate(
                directory, "n1", "Keep vendor state structured", to_root=True)
            self.assertIn("Keep vendor state structured", propagated["state"]["root"]["insight"])
            merged = self.registry.arbor_merge(
                directory, "n1", 0.75, "refs/heads/candidate")
            self.assertEqual("n1", merged["state"]["run"]["best_node"])
            cycled = self.registry.arbor_cycle(directory)
            self.assertEqual(1, cycled["state"]["run"]["cycles_used"])
            validated = self.registry.arbor_read(directory, "validate")
            self.assertIn("2 nodes", validated["stdout"])

    def test_adapter_rejects_run_directory_outside_integration_area(self):
        with self.assertRaisesRegex(ProtocolError, "integrations/runs"):
            self.registry.idea_next(PROJECT.parent / "escaped", "query")

    def test_idea_candidate_is_linked_to_real_arbor_tree_idempotently(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory:
            root = Path(directory)
            idea = root / "idea"
            arbor = root / "arbor"
            candidate_dir = idea / "phase2_generate"
            candidate_dir.mkdir(parents=True)
            candidate = {
                "title": "Evidence-linked candidate",
                "falsification_prediction": "Accuracy increases on the development split",
                "core_mechanism": "A fixture for the integration bridge",
            }
            (candidate_dir / "phase2_generate_output.json").write_text(
                json.dumps(candidate), encoding="utf-8")
            self.registry.arbor_init(
                arbor, "Improve a measurable fixture", "python dev.py", "python test.py")

            linked = self.registry.idea_to_arbor(idea, arbor)
            self.assertEqual("linked", linked["status"])
            self.assertEqual("n1", linked["candidate"]["node_id"])
            self.assertIn("Accuracy increases", linked["state"]["frontier"][0]["hypothesis"])
            repeated = self.registry.idea_to_arbor(idea, arbor)
            self.assertEqual("already_linked", repeated["status"])
            self.assertEqual(2, len(repeated["state"]["nodes"]))
            self.assertTrue((arbor / ".popper-integration" / "idea-arbor-links.json").is_file())

    def test_paper_search_is_structured_and_cached(self):
        runs = PROJECT / "integrations" / "runs"
        payload = {"raw_by_source": {"arxiv": []}, "source_counts": {"arxiv": 0},
                   "papers": [], "duplicate_count": 0, "dropped_count": 0}
        completed = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with tempfile.TemporaryDirectory(dir=runs) as directory, \
                mock.patch("popper.vendors.subprocess.run", return_value=completed) as called:
            first = self.registry.paper_search(directory, ["evidence agents"], 2024, 2026,
                                               sources=["arxiv"], trusted_local=True)
            second = self.registry.paper_search(directory, ["evidence agents"], 2024, 2026,
                                                sources=["arxiv"], trusted_local=True)
            self.assertEqual("miss", first["cache"])
            self.assertEqual("hit", second["cache"])
            self.assertEqual([], second["papers"])
            called.assert_called_once()
            with self.assertRaisesRegex(ProtocolError, "年份"):
                self.registry.paper_search(directory, ["q"], 2027, 2026, trusted_local=True)

    def test_scoop_run_and_provisional_arbor_gate(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory:
            root = Path(directory); idea = root / "idea"; scoop = root / "scoop"; arbor = root / "arbor"
            candidate_dir = idea / "phase2_generate"; candidate_dir.mkdir(parents=True)
            candidate = {"title": "Candidate", "core_mechanism": "Evidence graph",
                         "falsification_prediction": "Accuracy increases",
                         "differentiation_from_lit": []}
            (candidate_dir / "phase2_generate_output.json").write_text(json.dumps(candidate), encoding="utf-8")
            responses = iter([
                {"axes": {axis: axis for axis in ("problem_framing", "core_mechanism", "key_insight", "application_domain")},
                 "queries": ["q1 mechanism", "q2 domain", "q3 signature"]},
                {"papers": []},
                {"comparisons": [], "closest_paper_id": None},
                {"delta": "No verified closest paper; the result remains provisional."},
            ])
            run = ScoopRun(idea, scoop,
                           lambda q, s, e: {"papers": [], "request": {"queries": q}},
                           lambda system, payload: next(responses))
            result = run.run(2024, 2026)
            self.assertEqual("provisional", result["phase"])
            self.registry.arbor_init(arbor, "Objective", "dev", "test")
            with self.assertRaisesRegex(ProtocolError, "allow-provisional"):
                self.registry.scoop_to_arbor(idea, scoop, arbor)
            linked = self.registry.scoop_to_arbor(idea, scoop, arbor, allow_provisional=True)
            self.assertEqual("provisional", linked["link"]["scoop_status"])
            repeated = self.registry.scoop_to_arbor(idea, scoop, arbor, allow_provisional=True)
            self.assertEqual("already_linked", repeated["status"])

    def test_scoop_completed_with_fulltext_evidence(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory:
            root = Path(directory); idea = root / "idea"; scoop = root / "scoop"
            candidate_dir = idea / "phase2_generate"; candidate_dir.mkdir(parents=True)
            candidate = {"title": "Candidate", "core_mechanism": "Evidence graph",
                         "falsification_prediction": "Accuracy increases"}
            (candidate_dir / "phase2_generate_output.json").write_text(json.dumps(candidate), encoding="utf-8")
            axes = ("problem_framing", "core_mechanism", "key_insight", "application_domain")
            responses = iter([
                {"axes": {axis: axis for axis in axes}, "queries": ["q1", "q2", "q3"]},
                {"papers": [{"paper_id": "P-fixed", "overlap_score": 2,
                              **{axis: axis for axis in axes}}]},
                {"paper_id": "P-fixed", "axis_matches": {axis: "partial" for axis in axes},
                 "assumptions_scope": "bounded", "closest_passage": "verified full text describes this method"},
                {"comparisons": [{"paper_id": "P-fixed",
                                  "axis_matches": {"problem_framing": "match", "core_mechanism": "match",
                                                   "key_insight": "differ", "application_domain": "differ"}}],
                 "closest_paper_id": "P-fixed"},
                {"delta": "Unlike the closest work, the candidate changes the key insight."},
            ])
            paper = {"title": "Prior", "abstract": "abstract", "url": "https://arxiv.org/abs/1",
                     "doi": "fixed", "year": 2025}
            # Match the deterministic id generated from DOI.
            from popper.scoop import _paper_id
            pid = _paper_id(paper)
            responses_list = list(responses)
            responses_list[1]["papers"][0]["paper_id"] = pid
            responses_list[2]["paper_id"] = pid
            responses_list[3]["comparisons"][0]["paper_id"] = pid
            responses_list[3]["closest_paper_id"] = pid
            responses_list[3]["closest_paper_id"] = pid
            calls = iter(responses_list)
            run = ScoopRun(idea, scoop, lambda q, s, e: {"papers": [paper]},
                           lambda system, payload: next(calls),
                           lambda url, out, paper_id: {"pdf": "p.pdf", "pdf_sha256": "a" * 64,
                                                       "text": "p.txt", "text_sha256": "b" * 64,
                                                       "content": "verified full text describes this method"})
            result = run.run(2024, 2026)
            self.assertEqual("completed", result["phase"])
            self.assertEqual(3, result["report"]["level"])
            self.assertEqual(["b" * 64], result["report"]["fulltext_sha256"])

    def test_pdf_fetch_rejects_html_disguised_as_pdf(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory, \
                mock.patch("popper.scoop.urllib.request.urlopen",
                           return_value=io.BytesIO(b"<html>rate limited</html>")):
            with self.assertRaisesRegex(ProtocolError, "不是有效"):
                fetch_pdf_text("https://example.test/paper.pdf", Path(directory), "P-test")

    def test_arbor_node_executes_registered_popper_candidate_and_records_evidence(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory:
            root = Path(directory); arbor = root / "arbor"; experiment = root / "experiment"
            experiment.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(QUADRATIC / name, experiment / name)
            runpy.run_path(str(QUADRATIC / "generate_data.py"))["generate"](experiment)
            initialize(experiment)
            self.registry.arbor_init(arbor, "Quadratic features reduce MSE", "popper dev",
                                     "popper confirm", metric_direction="min")
            self.registry.arbor_add(arbor, "n0", "Degree two reduces development MSE")
            with self.assertRaisesRegex(ProtocolError, "trusted-local"):
                self.registry.arbor_evaluate(arbor, experiment, "n1", 1)
            evaluated = self.registry.arbor_evaluate(arbor, experiment, "n1", 1, True)
            self.assertEqual("evaluated", evaluated["status"])
            self.assertEqual("executed", evaluated["state"]["nodes"][1]["status"])
            self.assertGreater(evaluated["link"]["oriented_delta"], 0)
            self.assertTrue(evaluated["link"]["candidate_run_id"])
            repeated = self.registry.arbor_evaluate(arbor, experiment, "n1", 1, True)
            self.assertEqual("already_evaluated", repeated["status"])
            snapshot = self.registry.research_snapshot(arbor, experiment)
            self.assertEqual(1, snapshot["counts"]["results"])
            findings = Path(snapshot["findings"]).read_text(encoding="utf-8")
            self.assertIn("oriented_delta", findings)
            self.assertIn("Degree two reduces", findings)
            # An executed node cannot silently be used for a second configuration.
            with self.assertRaisesRegex(ProtocolError, "其他实验"):
                self.registry.arbor_evaluate(arbor, experiment, "n1", 2, True)

    def test_dispatch_maps_idea_only_to_registered_candidate(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory:
            root = Path(directory); idea = root / "idea"; scoop = root / "scoop"
            arbor = root / "arbor"; experiment = root / "experiment"
            candidate_dir = idea / "phase2_generate"; candidate_dir.mkdir(parents=True)
            candidate_path = candidate_dir / "phase2_generate_output.json"
            candidate_path.write_text(json.dumps({"title": "Quadratic relation",
                                                   "core_mechanism": "Add a quadratic feature",
                                                   "falsification_prediction": "MSE decreases"}), encoding="utf-8")
            scoop.mkdir()
            (scoop / "step7.json").write_text(json.dumps({"status": "completed", "level": 4,
                                                           "label": "Low Overlap", "delta": "Uses x squared",
                                                           "closest_paper_id": "P1",
                                                           "candidate_sha256": file_hash(candidate_path)}), encoding="utf-8")
            experiment.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(QUADRATIC / name, experiment / name)
            runpy.run_path(str(QUADRATIC / "generate_data.py"))["generate"](experiment)
            initialize(experiment)
            self.registry.arbor_init(arbor, "Reduce MSE", "dev", "test", metric_direction="min")
            self.registry.arbor_add(arbor, "n0", "Quadratic feature reduces MSE")
            decision = lambda system, payload: {"implementable": True, "candidate_index": 1,
                                                "rationale": "Degree two represents the mechanism",
                                                "expected_effect": "Development MSE decreases"}
            dispatched = self.registry.arbor_dispatch(
                idea, scoop, arbor, experiment, "n1", trusted_local=True, llm=decision)
            self.assertEqual("evaluated", dispatched["status"])
            self.assertEqual({"degree": 2}, dispatched["evaluation"]["config"])
            repeated = self.registry.arbor_dispatch(
                idea, scoop, arbor, experiment, "n1", trusted_local=True,
                llm=lambda *_: self.fail("cached dispatch must not call model"))
            self.assertEqual("already_evaluated", repeated["status"])

    def test_dispatch_can_refuse_unrepresentable_idea_without_execution(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory:
            root = Path(directory); idea = root / "idea"; scoop = root / "scoop"
            arbor = root / "arbor"; experiment = root / "experiment"
            candidate_dir = idea / "phase2_generate"; candidate_dir.mkdir(parents=True)
            candidate_path = candidate_dir / "phase2_generate_output.json"
            candidate_path.write_text(json.dumps({"title": "New architecture", "core_mechanism": "Change layers",
                                                   "falsification_prediction": "MSE decreases"}), encoding="utf-8")
            scoop.mkdir(); (scoop / "step7.json").write_text(json.dumps(
                {"status": "completed", "candidate_sha256": file_hash(candidate_path)}), encoding="utf-8")
            experiment.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(QUADRATIC / name, experiment / name)
            runpy.run_path(str(QUADRATIC / "generate_data.py"))["generate"](experiment); initialize(experiment)
            self.registry.arbor_init(arbor, "Reduce MSE", "dev", "test", metric_direction="min")
            self.registry.arbor_add(arbor, "n0", "Change architecture")
            result = self.registry.arbor_dispatch(
                idea, scoop, arbor, experiment, "n1", trusted_local=True,
                llm=lambda *_: {"implementable": False, "candidate_index": None,
                                "rationale": "No registered configuration changes layers",
                                "expected_effect": "No experiment should run"})
            self.assertEqual("not_implementable", result["status"])
            self.assertEqual("pending", result["state"]["nodes"][1]["status"])

    def test_code_proposal_creates_reviewable_diff_without_mutating_project(self):
        runs = PROJECT / "integrations" / "runs"
        with tempfile.TemporaryDirectory(dir=runs) as directory:
            root = Path(directory); idea = root / "idea"; scoop = root / "scoop"
            proposal = root / "proposal"; experiment = root / "experiment"
            candidate_dir = idea / "phase2_generate"; candidate_dir.mkdir(parents=True)
            candidate_path = candidate_dir / "phase2_generate_output.json"
            candidate_path.write_text(json.dumps({"title": "Stable training", "core_mechanism": "Guard inputs",
                                                   "falsification_prediction": "MSE decreases"}), encoding="utf-8")
            scoop.mkdir(); (scoop / "step7.json").write_text(json.dumps(
                {"status": "completed", "candidate_sha256": file_hash(candidate_path),
                 "level": 4, "delta": "Adds a guarded transform"}), encoding="utf-8")
            experiment.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(QUADRATIC / name, experiment / name)
            runpy.run_path(str(QUADRATIC / "generate_data.py"))["generate"](experiment); initialize(experiment)
            original = (experiment / "model.py").read_text(encoding="utf-8")
            def proposal_llm(system, payload):
                source = payload["files"][0]
                return {"summary": "Document the guarded candidate", "hypothesis": "The guard improves stability",
                        "edits": [{"path": source["path"], "original_sha256": source["sha256"],
                                   "replacement": source["content"] + "\n# proposed guarded variant\n"}]}
            result = self.registry.code_propose(
                idea, scoop, proposal, experiment, model="fixture", llm=proposal_llm)
            self.assertEqual("review_required", result["status"])
            self.assertIn("proposed guarded variant", Path(result["diff"]).read_text(encoding="utf-8"))
            self.assertEqual(original, (experiment / "model.py").read_text(encoding="utf-8"))
            cached = self.registry.code_propose(
                idea, scoop, proposal, experiment, model="fixture",
                llm=lambda *_: self.fail("cached proposal must not call model"))
            self.assertEqual("hit", cached["cache"])
            with self.assertRaisesRegex(ProtocolError, "approved"):
                self.registry.code_materialize(proposal, experiment)
            variant = self.registry.code_materialize(proposal, experiment, approved=True)
            self.assertEqual("ready", variant["status"])
            self.assertEqual(original, (experiment / "model.py").read_text(encoding="utf-8"))
            derived = Experiment(variant["project"])
            try:
                baseline = derived.evaluate({"degree": 1, "__popper_variant": "baseline"}, "dev", True)
                candidate_result = derived.evaluate({"degree": 1, "__popper_variant": "candidate"}, "dev", True)
                self.assertEqual(baseline["mean"], candidate_result["mean"])
            finally:
                derived.close()
            repeated_variant = self.registry.code_materialize(proposal, experiment, approved=True)
            self.assertEqual("hit", repeated_variant["cache"])


if __name__ == "__main__":
    unittest.main()
