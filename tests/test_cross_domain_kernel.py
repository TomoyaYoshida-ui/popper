"""Release acceptance: distinct scientific domains use one Research Kernel."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from popper.core import initialize
from popper.research.controller import ResearchController
from popper.research.readout import read_research_state


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def copy_registered_project(source, target):
    target.mkdir()
    spec = json.loads((source / "experiment.json").read_text(encoding="utf-8"))
    names = {"experiment.json", spec["train"], spec["dev"], spec["test"],
             *spec["code_files"], *spec.get("provenance_files", [])}
    for name in names:
        shutil.copyfile(source / name, target / name)


class CrossDomainResearchKernelAcceptanceTests(unittest.TestCase):
    def run_case(self, example, expected_repeat_unit, max_steps):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, run_dir = root / "project", root / "research"
            copy_registered_project(EXAMPLES / example, project)
            initialize(project)
            ResearchController.initialize(project, run_dir)
            controller = ResearchController(run_dir)
            try:
                self.assertEqual(expected_repeat_unit, controller.manifest["repeat_unit"])
                development = controller.run(trusted_local=True, max_steps=max_steps)
                self.assertEqual("ready_for_confirmation", development["phase"])
                self.assertTrue(all(row["scope"] == "dev"
                                    for row in development["observations"]))
                final = controller.confirm(trusted_local=True)
                self.assertEqual("concluded", final["phase"])
                self.assertTrue(any(row["scope"] == "confirmation"
                                    for row in final["observations"]))
                self.assertEqual(1, len(final["claims"]))
                self.assertEqual("registered_independent_confirmation",
                                 final["claims"][0]["scope"])
                self.assertEqual(2, len(final["claims"][0]["observation_refs"]))
                self.assertEqual(0.0, final["budget"]["reserved"])
            finally:
                controller.close()
            projection = read_research_state(run_dir)
            self.assertEqual(expected_repeat_unit, projection["manifest"]["repeat_unit"])
            self.assertTrue(projection["manifest"]["repeat_values"])
            graph = projection["experiment_graph"]
            self.assertTrue(graph["integrity"]["ok"])
            kinds = {node["kind"] for node in graph["nodes"]}
            self.assertTrue({"hypothesis", "design", "code_revision",
                             "run", "observation", "decision", "claim"} <= kinds)
            self.assertTrue(all(run.get("revision_id") for run in projection["runs"]))

    def test_numerical_ode_series_measurements(self):
        self.run_case("ode-convergence", "independent_run", 2)

    def test_public_wisconsin_data_driven_study(self):
        self.run_case("breast-cancer-wisconsin", "train_seed", 4)


if __name__ == "__main__":
    unittest.main()
