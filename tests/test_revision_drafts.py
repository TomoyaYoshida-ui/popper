import runpy
import shutil
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, initialize
from popper.research import ResearchController
from popper.research.readout import read_research_state
from popper.research.revision_drafts import (preview_initial_revision,
                                             submit_initial_revision)
from popper.research.revisions import RevisionStore
from popper.workspace_files import ProjectFiles


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


class RevisionDraftTests(unittest.TestCase):
    def test_reviewed_initial_revision_is_immutable_unexecuted_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, run_dir = root / "project", root / "research"
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(EXAMPLE / name, project / name)
            runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](project)
            initialize(project)
            ResearchController.initialize(project, run_dir)
            state = read_research_state(run_dir)
            hypothesis = next(row for row in state["hypotheses"]
                              if row["status"] == "untested"
                              and not row["hypothesis_id"].startswith("H-control-"))
            design = next(row for row in state["designs"]
                          if row["hypothesis_id"] == hypothesis["hypothesis_id"])
            source = ProjectFiles(project).read("model.py")
            spec = {"study_id": state["manifest"]["study_id"],
                    "expected_state_version": state["state_version"],
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "design_id": design["design_id"], "path": "model.py",
                    "expected_sha256": source["sha256"],
                    "replacement": source["content"] + "\n# reviewed revision\n"}
            preview = preview_initial_revision(run_dir, project, spec)
            self.assertIn("+\n", preview["diff"])
            self.assertEqual([], list((run_dir / "revisions").glob("REV-*")))
            with self.assertRaisesRegex(ProtocolError, "提案已变化"):
                submit_initial_revision(run_dir, project, spec, "0" * 64)
            created = submit_initial_revision(
                run_dir, project, spec, preview["proposal_sha256"])
            self.assertEqual("immutable_unexecuted", created["status"])
            self.assertEqual(created, submit_initial_revision(
                run_dir, project, spec, preview["proposal_sha256"]))
            verified = RevisionStore(run_dir / "revisions").verify(created["revision_id"])
            self.assertEqual(design["design_id"], verified["identity"]["design_id"])
            self.assertEqual(source["sha256"], ProjectFiles(project).read("model.py")["sha256"])
            after = read_research_state(run_dir)
            self.assertTrue(after["experiment_graph"]["integrity"]["ok"])
            self.assertEqual([], after["observations"])
