"""议程循环：提案者只看数据卡，失败后的后继提案不能改栏杆。不调用模型。"""
from __future__ import annotations

import builtins
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from popper.core import ProtocolError, read_json, write_json
from popper.research.actions import RUN_EXPERIMENT, ActionProposal
from popper.research.candidate_space import choose_space_action
from popper.research.models import EvidenceDrivenPolicy
from popper.research.followup import prepare_followup_project
from popper.research.formulate import _proposal
from popper.research.models import DeepSeekResearchPolicy
from popper.research.agenda import (
    above_threshold_id, accept_revised_mechanism, next_mechanism, public_card)
from popper.research.question_proposer import (
    FIXTURES, CardProposer, FixtureProposer, ModelProposer,
    proposal_from_card_reply, require_explanation_action)
from popper.research.successor import (
    confirmation_row_set_sha256, reject_lower_threshold, reject_reused_holdout,
    successor_for_failure)


def _explanations():
    return [dict(item) for item in FIXTURES["fixture-table"]["explanations"]]


class AgendaLoopTests(unittest.TestCase):
    def test_fixture_proposer_reads_the_card_and_not_the_files(self):
        card = {"columns": [{"name": "label", "type": "integer"}]}
        reads = []

        def record(self, *args, **kwargs):
            reads.append(str(self))
            raise AssertionError(f"不应读取 {self}")

        original_text, original_bytes, original_open = Path.read_text, Path.read_bytes, builtins.open

        def guarded_open(*args, **kwargs):
            reads.append(str(args[0]) if args else "?")
            raise AssertionError("不应读取文件")

        Path.read_text = record
        Path.read_bytes = record
        builtins.open = guarded_open
        try:
            proposal = FixtureProposer("fixture-table").propose(card, "夹具目的", "ab" * 32)
        finally:
            Path.read_text = original_text
            Path.read_bytes = original_bytes
            builtins.open = original_open
        self.assertEqual([], reads)
        self.assertEqual("年龄与收入能否预测标签", proposal["question"])
        self.assertEqual(2, len(proposal["hypothesis"]["explanations"]))

    def test_model_reply_is_rejected_before_anything_is_written(self):
        def call(_system, _payload):
            return {"question": "x"}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ProtocolError):
                ModelProposer(call).propose(
                    {"columns": [{"name": "label"}]}, "目的", "cd" * 32)
            self.assertEqual([], list(root.iterdir()))

    def test_new_study_action_must_name_one_explanation(self):
        explanations = _explanations()
        context = {
            "candidates": [{
                "hypothesis_id": "H-1", "status": "untested", "config": {"model": "linear"},
            }],
            "explanations": explanations,
        }
        chosen = EvidenceDrivenPolicy().choose(context)
        self.assertEqual("linear-signal", chosen.explanation_id)
        bare = {
            "candidates": [{
                "hypothesis_id": "H-1", "status": "untested", "config": {"model": "linear"},
            }],
        }
        self.assertIsNone(EvidenceDrivenPolicy().choose(bare).explanation_id)
        wrong = ActionProposal(RUN_EXPERIMENT, "试一下", "H-1", explanation_id="missing")
        with self.assertRaises(ProtocolError) as rejected:
            require_explanation_action(wrong, explanations)
        self.assertEqual(str(rejected.exception), "下一步实验必须指向一个竞争解释")
        with self.assertRaises(ProtocolError):
            choose_space_action([], explanations=explanations)

    def test_failed_prediction_successor_keeps_the_bar(self):
        parent_hash = "a" * 64
        proposal = successor_for_failure(
            explanations=_explanations(),
            tested_explanation_id="linear-signal",
            observation_refs=("OBS-baseline", "OBS-candidate"),
            parent_threshold=0.1,
            parent_question="年龄与收入能否预测标签",
            parent_holdout_sha256=parent_hash)
        self.assertEqual("missingness", proposal["surviving_explanation_id"])
        self.assertNotIn(parent_hash, json.dumps(proposal))
        self.assertEqual(0.1, proposal["parent_threshold"])
        with self.assertRaises(ProtocolError) as lowered:
            reject_lower_threshold(0.1, 0.01)
        self.assertEqual(str(lowered.exception), "后继研究不能降低最小有意义改善")
        with self.assertRaises(ProtocolError) as reused:
            reject_reused_holdout(parent_hash, parent_hash)
        self.assertEqual(str(reused.exception), "后继研究不能复用父确认集")
        reject_lower_threshold(0.1, 0.1)
        reject_reused_holdout(parent_hash, "b" * 64)

    def test_controller_stop_writes_successor_and_followup_keeps_the_bar(self):
        import runpy
        import shutil

        from popper.core import digest, file_hash, initialize, read_json, write_json
        from popper.research.controller import ResearchController
        from popper.research.followup import create_followup_study, prepare_followup_project
        from popper.research.intake import IntakeLog
        from popper.research.successor import confirmation_row_set_sha256

        example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(example / name, project / name)
            runpy.run_path(str(example / "generate_data.py"))["generate"](project)
            spec = read_json(project / "experiment.json")
            spec["candidates"] = [{"degree": 0}]
            spec["budget"] = 1
            spec["seeds"] = [11]
            spec["min_improvement"] = 1000000
            spec["objective"] = "二次项是否在开发集上超过线性基线一个不可达的阈值"
            write_json(project / "experiment.json", spec)
            initialize(project)
            explanations = [
                {"id": "quadratic-signal", "statement": "二次项解释残差",
                 "prediction": "二次候选的开发集误差小于线性基线"},
                {"id": "noise", "statement": "残差只是噪声",
                 "prediction": "二次候选的开发集误差不小于线性基线"},
            ]
            problem = {"hypothesis": {"explanations": explanations},
                       "question": spec["objective"]}
            problem_sha = digest(problem)
            write_json(project / ".popper-intake" / "problems" / f"{problem_sha}.json", problem)
            log = IntakeLog(project / ".popper-intake" / "intake.sqlite")
            try:
                log.append(problem_sha, "problem_approved", {
                    "proposal_id": problem_sha[:16],
                    "problem_sha256": problem_sha,
                    "experiment_sha256": file_hash(project / "experiment.json"),
                })
                log.commit()
            finally:
                log.close()
            run_dir = root / "run"
            ResearchController.initialize(project, run_dir)
            controller = ResearchController(run_dir)
            try:
                status = controller.run(trusted_local=True)
            finally:
                controller.close()
            self.assertEqual("concluded", status["phase"])
            self.assertEqual([], status["claims"])
            self.assertTrue(any(item["action"] == "stop" for item in status["decisions"]))
            proposal_path = run_dir / "successor-proposal.json"
            proposal = read_json(proposal_path)
            parent_file_sha = read_json(run_dir / "research.json")["input_identity"][
                "input_hashes"][spec["test"]]
            parent_row_sha = confirmation_row_set_sha256(read_json(project / spec["test"]))
            stored = proposal_path.read_text(encoding="utf-8")
            self.assertNotIn(parent_file_sha, stored)
            self.assertNotIn(parent_row_sha, stored)
            self.assertEqual(1000000, proposal["parent_threshold"])
            self.assertIsNone(proposal["surviving_explanation_id"])
            self.assertEqual("unverified", proposal["mechanism_status"])
            self.assertEqual(2, len(proposal["observation_refs"]))
            fresh = [{"id": f"followup-{i}", "x": i / 5, "y": 0.2} for i in range(8)]
            fresh_path = root / "fresh.json"
            write_json(fresh_path, fresh)
            reused = root / "reused.json"
            reused.write_text(json.dumps(read_json(project / spec["test"]),
                                         ensure_ascii=False, separators=(",", ":")),
                              encoding="utf-8")
            self.assertNotEqual(file_hash(reused), parent_file_sha)
            low = root / "low"
            with self.assertRaises(ProtocolError) as lowered:
                create_followup_study(
                    run_dir, low, root / "low-run", [{"degree": 2}], fresh_path,
                    budget=1, min_improvement=0.1)
            self.assertEqual(str(lowered.exception), "后继研究不能降低最小有意义改善")
            self.assertFalse(low.exists())
            reused_project = root / "reused-project"
            with self.assertRaises(ProtocolError) as same_rows:
                prepare_followup_project(
                    run_dir, reused_project, [{"degree": 2}], reused, budget=1)
            self.assertEqual(str(same_rows.exception), "后继研究不能复用父确认集")
            self.assertFalse(reused_project.exists())
            prepared = prepare_followup_project(
                run_dir, root / "kept", [{"degree": 2}], fresh_path,
                budget=1, min_improvement=1000000, objective=proposal["revised_mechanism"])
            self.assertEqual(1000000, read_json(root / "kept" / "experiment.json")[
                "min_improvement"])
            self.assertNotIn("label_stats", prepared)

    def test_same_direction_is_rejected_and_legacy_text_still_passes(self):
        body = {
            "question": "年龄与收入能否预测标签",
            "hypothesis": {"explanations": _explanations()},
            "target_column": "label",
            "task_shape": "aligned_prediction",
            "metric": "mse-v1",
            "min_improvement": {"value": 0.1, "unit": "squared_error", "reason": "夹具阈值"},
            "repeat_unit": "train_seed",
            "stop_rule": {"kind": "reach_baseline"},
            "split_declaration_sha256": "ab" * 32,
        }
        self.assertEqual(body["question"], _proposal(json.loads(json.dumps(body)))["question"])
        opposed = json.loads(json.dumps(body))
        opposed["hypothesis"]["explanations"][0]["contrast"] = {"direction": "above_threshold"}
        opposed["hypothesis"]["explanations"][1]["contrast"] = {"direction": "not_above_threshold"}
        self.assertEqual(2, len(_proposal(opposed)["hypothesis"]["explanations"]))
        same = json.loads(json.dumps(opposed))
        same["hypothesis"]["explanations"][1]["contrast"] = {"direction": "above_threshold"}
        with self.assertRaises(ProtocolError) as same_direction:
            _proposal(same)
        self.assertEqual(str(same_direction.exception), "两条解释的方向必须相反")
        missing = json.loads(json.dumps(body))
        missing["hypothesis"]["explanations"][0]["contrast"] = {"direction": "above_threshold"}
        with self.assertRaises(ProtocolError) as one_sided:
            _proposal(missing)
        self.assertEqual(str(one_sided.exception), "两条解释的方向必须相反")

    def test_positive_claim_is_bound_even_when_it_is_second(self):
        explanations = _explanations()
        explanations[0]["contrast"] = {"direction": "not_above_threshold"}
        explanations[1]["contrast"] = {"direction": "above_threshold"}
        chosen = EvidenceDrivenPolicy().choose({
            "candidates": [{
                "hypothesis_id": "H-1", "status": "untested", "config": {"model": "linear"},
            }],
            "explanations": explanations,
        })
        self.assertEqual("missingness", chosen.explanation_id)
        wrong = ActionProposal(
            RUN_EXPERIMENT, "试一下", "H-1", explanation_id="linear-signal")
        with self.assertRaises(ProtocolError) as rejected:
            require_explanation_action(wrong, explanations)
        self.assertEqual(str(rejected.exception), "下一步实验必须指向可证伪的正预测")

    def test_model_cannot_name_the_killed_explanation_or_copy_a_statement(self):
        policy = DeepSeekResearchPolicy.__new__(DeepSeekResearchPolicy)
        policy.name = "deepseek_json_policy"
        policy.model = "fake"
        explanations = _explanations()
        with self.assertRaises(ProtocolError) as killed:
            policy._parse_choice(
                {"kind": "stop", "hypothesis_id": None, "rationale": "停",
                 "killed_explanation_id": "linear-signal"},
                ["stop"], [], [], explanations)
        self.assertEqual(str(killed.exception), "模型不能指定被杀掉的解释")

        def copying(_system, _payload):
            return {"revised_mechanism": explanations[0]["statement"]}

        policy._call = copying
        with self.assertRaises(ProtocolError) as copied:
            policy.revise_mechanism(
                killed_id="linear-signal", surviving_id="missingness", delta=0.0,
                threshold=0.1, explanations=explanations,
                parent_question="年龄与收入能否预测标签",
                parent_holdout_sha256="a" * 64)
        self.assertEqual(str(copied.exception), "后继机制必须改写")

        def naming(_system, _payload):
            return {"revised_mechanism": "改检验年龄的非线性项", "killed_id": "linear-signal"}

        policy._call = naming
        with self.assertRaises(ProtocolError) as named:
            policy.revise_mechanism(
                killed_id="linear-signal", surviving_id="missingness", delta=0.0,
                threshold=0.1, explanations=explanations,
                parent_question="年龄与收入能否预测标签",
                parent_holdout_sha256="a" * 64)
        self.assertEqual(str(named.exception), "模型不能指定被杀掉的解释")

    def test_failed_positive_claim_rewrites_the_mechanism(self):
        status, proposal, run_dir, root, spec = self._run_quadratic(
            explanations=_opposed(positive_second=True),
            candidates=[{"degree": 0}],
            min_improvement=1000000)
        self.assertEqual("concluded", status["phase"])
        self.assertEqual([], status["claims"])
        self.assertIsNone(proposal["surviving_explanation_id"])
        self.assertEqual("not_met", proposal["prediction_result"]["status"])
        revised = proposal["revised_mechanism"]
        self.assertNotEqual("二次项解释残差", revised)
        self.assertNotEqual("残差只是噪声", revised)
        self.assertNotEqual(spec["objective"], revised)
        stored = (run_dir / "successor-proposal.json").read_text(encoding="utf-8")
        parent_file_sha = read_json(run_dir / "research.json")["input_identity"][
            "input_hashes"][spec["test"]]
        parent_row_sha = confirmation_row_set_sha256(read_json(Path(spec["_project"]) / spec["test"]))
        self.assertNotIn(parent_file_sha, stored)
        self.assertNotIn(parent_row_sha, stored)
        fresh = [{"id": f"followup-{i}", "x": i / 5, "y": 0.2} for i in range(8)]
        fresh_path = root / "fresh.json"
        write_json(fresh_path, fresh)
        copied = root / "copied"
        with self.assertRaises(ProtocolError) as parent_question:
            prepare_followup_project(
                run_dir, copied, [{"degree": 2}], fresh_path, budget=1,
                objective=spec["objective"], min_improvement=1000000)
        self.assertEqual(str(parent_question.exception), "后继研究不能沿用父问题")
        self.assertFalse(copied.exists())
        omitted = root / "omitted"
        with self.assertRaises(ProtocolError):
            prepare_followup_project(
                run_dir, omitted, [{"degree": 2}], fresh_path, budget=1,
                min_improvement=1000000)
        self.assertFalse(omitted.exists())
        lowered = root / "lowered"
        with self.assertRaises(ProtocolError) as low:
            prepare_followup_project(
                run_dir, lowered, [{"degree": 2}], fresh_path, budget=1,
                objective=revised, min_improvement=0.1)
        self.assertEqual(str(low.exception), "后继研究不能降低最小有意义改善")
        self.assertFalse(lowered.exists())
        reused = root / "reused.json"
        reused.write_text(json.dumps(read_json(Path(spec["_project"]) / spec["test"]),
                                     ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")
        reused_project = root / "reused-project"
        with self.assertRaises(ProtocolError) as same_rows:
            prepare_followup_project(
                run_dir, reused_project, [{"degree": 2}], reused, budget=1,
                objective=revised, min_improvement=1000000)
        self.assertEqual(str(same_rows.exception), "后继研究不能复用父确认集")
        self.assertFalse(reused_project.exists())
        prepared = prepare_followup_project(
            run_dir, root / "kept", [{"degree": 2}], fresh_path, budget=1,
            objective=revised, min_improvement=1000000)
        kept = read_json(root / "kept" / "experiment.json")
        self.assertEqual(revised, kept["objective"])
        self.assertEqual(1000000, kept["min_improvement"])
        self.assertNotIn("label_stats", prepared)

    def test_supported_positive_claim_kills_the_negative_explanation(self):
        status, proposal, _run_dir, _root, _spec = self._run_quadratic(
            explanations=_opposed(positive_second=True),
            candidates=[{"degree": 2}],
            min_improvement=0.1)
        self.assertIsNone(proposal)
        self.assertEqual("ready_for_confirmation", status["phase"])
        self.assertEqual([], status["claims"])
        rationales = " ".join(item["rationale"] for item in status["decisions"])
        self.assertNotIn("杀掉解释", rationales)

    def _run_quadratic(self, *, explanations, candidates, min_improvement):
        import runpy
        import shutil

        from popper.core import digest, file_hash, initialize
        from popper.research.controller import ResearchController
        from popper.research.intake import IntakeLog

        example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        project = root / "project"
        project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(example / name, project / name)
        runpy.run_path(str(example / "generate_data.py"))["generate"](project)
        spec = read_json(project / "experiment.json")
        spec["candidates"] = candidates
        spec["budget"] = 1
        spec["seeds"] = [11]
        spec["min_improvement"] = min_improvement
        spec["objective"] = "二次项是否在开发集上超过线性基线"
        write_json(project / "experiment.json", spec)
        initialize(project)
        problem = {"hypothesis": {"explanations": explanations}, "question": spec["objective"]}
        problem_sha = digest(problem)
        write_json(project / ".popper-intake" / "problems" / f"{problem_sha}.json", problem)
        log = IntakeLog(project / ".popper-intake" / "intake.sqlite")
        try:
            log.append(problem_sha, "problem_approved", {
                "proposal_id": problem_sha[:16],
                "problem_sha256": problem_sha,
                "experiment_sha256": file_hash(project / "experiment.json"),
            })
            log.commit()
        finally:
            log.close()
        run_dir = root / "run"
        ResearchController.initialize(project, run_dir)
        controller = ResearchController(run_dir)
        try:
            status = controller.run(trusted_local=True)
        finally:
            controller.close()
        proposal_path = run_dir / "successor-proposal.json"
        proposal = read_json(proposal_path) if proposal_path.is_file() else None
        spec["_project"] = str(project)
        return status, proposal, run_dir, root, spec


def _opposed(positive_second):
    positive = {
        "id": "quadratic-signal", "statement": "二次项解释残差",
        "prediction": "二次候选的开发集误差小于线性基线",
        "contrast": {"direction": "above_threshold"},
    }
    negative = {
        "id": "noise", "statement": "残差只是噪声",
        "prediction": "二次候选的开发集误差不小于线性基线",
        "contrast": {"direction": "not_above_threshold"},
    }
    return [negative, positive] if positive_second else [positive, negative]


def _heart_card(top="oldpeak", second="thal", top_value=0.42, second_value=0.11):
    return {
        "target_column": "num",
        "raw_sha256": "ab" * 32,
        "seal_sha256": "cd" * 32,
        "columns": [
            {"name": "num", "type": "integer", "missing_rate": 0},
            {"name": top, "type": "number", "missing_rate": 0},
            {"name": second, "type": "integer", "missing_rate": 0},
        ],
        "holdout": {"n_rows": 12, "feature_types": {top: "number", second: "integer"}},
        "label_stats": {
            "scope": "train_dev",
            "distribution": [
                {"value": 0, "count": 40, "rate": 0.5},
                {"value": 1, "count": 20, "rate": 0.25},
                {"value": 2, "count": 20, "rate": 0.25},
            ],
            "associations": [
                {"feature": second, "method": "pearson", "n_pairs": 80, "value": second_value},
                {"feature": top, "method": "pearson", "n_pairs": 80, "value": top_value},
            ],
        },
    }


class CardProposerTests(unittest.TestCase):
    def test_card_proposer_uses_the_card_and_does_not_read_files(self):
        card = _heart_card()
        reads = []

        def record(self, *args, **kwargs):
            reads.append(str(self))
            raise AssertionError(f"不应读取 {self}")

        original_text, original_bytes, original_open = Path.read_text, Path.read_bytes, builtins.open

        def guarded_open(*args, **kwargs):
            reads.append(str(args[0]) if args else "?")
            raise AssertionError("不应读取文件")

        Path.read_text = record
        Path.read_bytes = record
        builtins.open = guarded_open
        try:
            proposal = CardProposer().propose(card, "只看数据卡", "ab" * 32)
            exposed = public_card(card)
        finally:
            Path.read_text = original_text
            Path.read_bytes = original_bytes
            builtins.open = original_open
        self.assertEqual([], reads)
        self.assertNotIn("raw_sha256", exposed)
        self.assertNotIn("seal_sha256", exposed)
        self.assertEqual("train_dev", exposed["label_stats"]["scope"])
        rows = proposal["hypothesis"]["explanations"]
        self.assertEqual("class-prior", rows[0]["id"])
        self.assertEqual("not_above_threshold", rows[0]["contrast"]["direction"])
        self.assertEqual("measured-signal", above_threshold_id(rows))
        self.assertEqual("above_threshold", rows[1]["contrast"]["direction"])
        self.assertIn("oldpeak", proposal["question"])
        self.assertIn("oldpeak", rows[1]["statement"])
        self.assertEqual("multiclass-macro-f1-v1", proposal["metric"])
        self.assertEqual(0.05, proposal["min_improvement"]["value"])
        other = CardProposer().propose(
            _heart_card(top="thal", second="oldpeak", top_value=0.9, second_value=0.2),
            "只看数据卡", "ab" * 32)
        self.assertIn("thal", other["question"])
        self.assertNotEqual(proposal["question"], other["question"])

    def test_card_proposer_rejects_holdout_labels_and_ungrounded_text(self):
        card = _heart_card()
        leaked = json.loads(json.dumps(card))
        leaked["holdout"] = {"labels": [0, 1, 2]}
        with self.assertRaises(ProtocolError) as labels:
            CardProposer().propose(leaked, "只看数据卡", "ab" * 32)
        self.assertEqual(str(labels.exception), "提案不能读取确认集标签")
        wrong_scope = json.loads(json.dumps(card))
        wrong_scope["label_stats"]["scope"] = "all"
        with self.assertRaises(ProtocolError) as scope:
            CardProposer().propose(wrong_scope, "只看数据卡", "ab" * 32)
        self.assertEqual(str(scope.exception), "提案只能使用训练和开发范围的数据卡")
        reply = {
            "question": "没有点名列",
            "explanations": CardProposer().propose(card, "只看数据卡", "ab" * 32)[
                "hypothesis"]["explanations"],
        }
        with self.assertRaises(ProtocolError):
            proposal_from_card_reply(card, reply, "ab" * 32)
        killed = {
            "killed_explanation_id": "class-prior",
            "question": "列 oldpeak 预测 num 是否达到 0.05",
            "explanations": reply["explanations"],
        }
        with self.assertRaises(ProtocolError) as named:
            proposal_from_card_reply(card, killed, "ab" * 32)
        self.assertEqual(str(named.exception), "模型不能指定被杀掉的解释")

    def test_next_mechanism_names_the_second_feature_and_keeps_the_threshold(self):
        proposal = CardProposer().propose(_heart_card(), "只看数据卡", "ab" * 32)
        text = next_mechanism("thal", 0.05, "num")
        accepted = accept_revised_mechanism(
            text, explanations=proposal["hypothesis"]["explanations"],
            parent_question=proposal["question"], parent_holdout_sha256="ef" * 32)
        self.assertEqual(text, accepted)
        self.assertIn("thal", accepted)
        self.assertIn("0.05", accepted)
        self.assertNotEqual(accepted, proposal["question"])
