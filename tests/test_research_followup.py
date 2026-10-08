import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.cli import main
from popper.core import Experiment, ProtocolError, file_hash, initialize, read_json, write_json
from popper.research.controller import ResearchController
from popper.research.followup import (create_followup_study, prepare_followup_project,
                                      resume_followup_study, validate_followup_lineage)
from popper.research.readout import read_research_state


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ode-convergence"


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.parent = self.root / "parent"
        self.parent.mkdir()
        for name in ("experiment.json", "model.py", "train.json", "dev.json", "test.json"):
            shutil.copy2(EXAMPLE / name, self.parent / name)
        initialize(self.parent)
        self.run_dir = self.root / "parent-run"
        ResearchController.initialize(self.parent, self.run_dir)
        self.new_test = self.root / "independent-test.json"
        write_json(self.new_test, [
            {"id": "followup-24", "steps": 24, "rate": 4.0, "t_end": 1.0},
            {"id": "followup-48", "steps": 48, "rate": 4.0, "t_end": 1.0},
            {"id": "followup-96", "steps": 96, "rate": 4.0, "t_end": 1.0},
        ])

    def tearDown(self):
        self.temp.cleanup()

    def test_non_ml_followup_is_new_preregistered_study(self):
        new_project = self.root / "followup"
        prepared = prepare_followup_project(
            self.run_dir, new_project, [{"scheme": "heun"}], self.new_test,
            budget=1, objective="检验新单步法在独立问题实例的收敛阶")
        self.assertEqual("searching", prepared["experiment_phase"])
        self.assertFalse(prepared["research_initialized"])
        spec = read_json(new_project / "experiment.json")
        self.assertEqual([{"scheme": "heun"}], spec["candidates"])
        self.assertEqual(1, spec["budget"])
        self.assertIn("followup-lineage.json", spec["provenance_files"])
        self.assertNotEqual(read_json(self.parent / "test.json"),
                            read_json(new_project / "test.json"))
        experiment = Experiment(new_project)
        try:
            self.assertEqual("searching", experiment.verify_inputs()["phase"])
        finally:
            experiment.close()
        new_run = self.root / "followup-run"
        status = ResearchController.initialize(new_project, new_run)
        self.assertNotEqual(read_json(self.run_dir / "research.json")["study_id"],
                            read_json(new_run / "research.json")["study_id"])
        self.assertEqual("researching", status["phase"])
        self.assertEqual("adaptive_followup", read_json(
            new_project / "followup-lineage.json")["kind"])
        self.assertEqual(read_json(self.run_dir / "research.json")["study_id"],
                         read_research_state(new_run)["manifest"][
                             "followup_lineage"]["parent_study_id"])

    def test_rejects_reused_or_overlapping_confirmation_set_without_output(self):
        target = self.root / "rejected"
        with self.assertRaisesRegex(ProtocolError, "复用"):
            prepare_followup_project(self.run_dir, target, [{"scheme": "heun"}],
                                     self.parent / "test.json", budget=1)
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ProtocolError, "新候选"):
            prepare_followup_project(self.run_dir, target, [{"scheme": "rk4"}],
                                     self.new_test, budget=1)
        self.assertFalse(target.exists())
        write_json(self.new_test, [
            {"id": "test-20", "steps": 24, "rate": 4.0, "t_end": 1.0},
            {"id": "followup-48", "steps": 48, "rate": 4.0, "t_end": 1.0},
        ])
        with self.assertRaisesRegex(ProtocolError, "样本重叠"):
            prepare_followup_project(self.run_dir, target, [{"scheme": "heun"}],
                                     self.new_test, budget=1)
        self.assertFalse(target.exists())

    def test_cli_creates_followup_study(self):
        candidates = self.root / "candidates.json"
        candidates.write_text(json.dumps([{"scheme": "heun"}]), encoding="utf-8")
        new_project = self.root / "cli-followup"
        new_run = self.root / "cli-followup-run"
        self.assertEqual(0, main([
            "research", "followup", str(self.run_dir), str(new_project), str(new_run),
            "--candidates-json", str(candidates), "--new-test", str(self.new_test),
            "--budget", "1", "--objective", "独立确认新方案",
        ]))
        self.assertTrue((new_project / ".popper" / "state.db").is_file())
        self.assertTrue((new_run / "research.sqlite").is_file())

    def test_lineage_rejects_changed_claim_scope_and_extra_fields(self):
        new_project = self.root / "lineage-check"
        prepare_followup_project(self.run_dir, new_project, [{"scheme": "heun"}],
                                 self.new_test, budget=1)
        spec = read_json(new_project / "experiment.json")
        lineage = read_json(new_project / "followup-lineage.json")
        test_sha = file_hash(new_project / "test.json")
        self.assertEqual(lineage, validate_followup_lineage(lineage, spec, test_sha))
        with self.assertRaises(ProtocolError):
            validate_followup_lineage({**lineage, "claim_scope": "original_parent_study"},
                                      spec, test_sha)
        with self.assertRaises(ProtocolError):
            validate_followup_lineage({**lineage, "unreviewed_claim": True}, spec, test_sha)

    def test_followup_creation_preflight_does_not_leave_partial_project(self):
        project = self.root / "preflight-project"
        run_dir = project / "nested-run"
        with self.assertRaisesRegex(ProtocolError, "不能重叠"):
            create_followup_study(self.run_dir, project, run_dir,
                                  [{"scheme": "heun"}], self.new_test, budget=1)
        self.assertFalse(project.exists())
        occupied = self.root / "occupied-run"
        occupied.mkdir()
        with self.assertRaisesRegex(ProtocolError, "尚不存在"):
            create_followup_study(self.run_dir, project, occupied,
                                  [{"scheme": "heun"}], self.new_test, budget=1)
        self.assertFalse(project.exists())

    def test_followup_study_init_failure_preserves_recoverable_project(self):
        project = self.root / "recoverable-project"
        run_dir = self.root / "recoverable-run"
        with patch.object(ResearchController, "initialize",
                          side_effect=ProtocolError("injected initialization fault")):
            with self.assertRaisesRegex(ProtocolError, "项目已建立"):
                create_followup_study(self.run_dir, project, run_dir,
                                      [{"scheme": "heun"}], self.new_test,
                                      budget=1)
        self.assertTrue((project / ".popper" / "state.db").is_file())
        self.assertTrue((project / "followup-lineage.json").is_file())
        self.assertFalse(run_dir.exists())
        recovered = ResearchController.initialize(project, run_dir)
        self.assertEqual("researching", recovered["phase"])

    def test_verified_partial_followup_can_resume_without_new_project(self):
        project = self.root / "resume-project"
        run_dir = self.root / "resume-run"
        prepare_followup_project(self.run_dir, project, [{"scheme": "heun"}],
                                 self.new_test, budget=1, objective="独立检验")
        with self.assertRaisesRegex(ProtocolError, "不一致"):
            resume_followup_study(self.run_dir, project, run_dir,
                                  [{"scheme": "euler"}], self.new_test,
                                  budget=1, objective="独立检验")
        self.assertFalse(run_dir.exists())
        with patch.object(ResearchController, "initialize",
                          side_effect=ProtocolError("retryable init fault")):
            with self.assertRaisesRegex(ProtocolError, "项目已验证"):
                resume_followup_study(self.run_dir, project, run_dir,
                                      [{"scheme": "heun"}], self.new_test,
                                      budget=1, objective="独立检验")
        self.assertFalse(run_dir.exists())
        recovered = resume_followup_study(
            self.run_dir, project, run_dir, [{"scheme": "heun"}],
            self.new_test, budget=1, objective="独立检验")
        self.assertTrue(recovered["replayed"])
        self.assertTrue((run_dir / "research.sqlite").is_file())
        replay = resume_followup_study(
            self.run_dir, project, run_dir, [{"scheme": "heun"}],
            self.new_test, budget=1, objective="独立检验")
        self.assertEqual(recovered["study_id"], replay["study_id"])

    def test_followup_without_successor_proposal_keeps_parent_threshold(self):
        self.assertFalse((self.run_dir / "successor-proposal.json").is_file())
        new_project = self.root / "kept-threshold"
        prepare_followup_project(
            self.run_dir, new_project, [{"scheme": "heun"}], self.new_test,
            budget=1, min_improvement=0.01)
        self.assertEqual(0.5, read_json(new_project / "experiment.json")["min_improvement"])

    def test_resume_never_adopts_or_initializes_unrelated_directory(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        with self.assertRaisesRegex(ProtocolError, "不是可验证"):
            resume_followup_study(self.run_dir, unrelated, self.root / "unused-run",
                                  [{"scheme": "heun"}], self.new_test, budget=1)
        self.assertFalse((unrelated / ".popper").exists())
