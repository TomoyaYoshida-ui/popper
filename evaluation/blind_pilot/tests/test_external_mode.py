import shutil
import tempfile
import unittest
from pathlib import Path

from popper.capabilities import extra_available

from evaluation.blind_pilot.audit import audit_cell
from evaluation.blind_pilot.matrix import build_matrix, run_matrix


@unittest.skipUnless(extra_available("ml"),
                     "ml extra 未安装（scikit-learn）：外部确认用例依赖真模型跑完一个 cell")
class ExternalModeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="blind-external-test-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_registered_comparator_uses_external_confirmation_and_finishes(self):
        cell = build_matrix(["T01-wdbc-positive"], ["fixed_registered_search"], 1)[0]
        summary = run_matrix([cell], trial_root=self.root, confirm_mode="external")[0]

        self.assertEqual("external", summary["requested_confirm_mode"])
        self.assertEqual("external", summary["confirm_mode"])
        self.assertTrue(summary["test_exposure"]["consumed"])
        self.assertTrue(summary["evidence_replay"]["ok"], summary)
        self.assertTrue(summary["valid_loop"], summary)
        self.assertTrue(summary["conclusion_matched"], summary)

    def test_prediction_tamper_fails_posthoc_replay(self):
        cell = build_matrix(["T06-ts-negative"], ["same_model_same_tools_fixed_plan"], 1)[0]
        summary = run_matrix([cell], trial_root=self.root, confirm_mode="none")[0]
        cell_dir = self.root / f"{cell['task_id']}-{cell['comparator']}-run1"
        request = next((cell_dir / "research" / "evaluations").glob("*/request.json"))
        import json
        body = json.loads(request.read_text(encoding="utf-8"))
        prediction = Path(body["predictions"][0]["path"])
        prediction.write_text("[]", encoding="utf-8")

        audit = audit_cell(cell_dir, self.root / "tasks" / cell["task_id"] / "gold")

        self.assertFalse(audit["ok"])
        self.assertTrue(any("artifact changed" in error or "制品摘要" in error
                            for error in audit["errors"]), audit)


if __name__ == "__main__":
    unittest.main()
