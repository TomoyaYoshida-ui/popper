import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popper.core import read_json

from evaluation.blind_pilot.matrix import build_matrix, cell_done, run_matrix
from evaluation.blind_pilot.report import build_report, is_valid_loop, classify_failure


class SmokeMatrixTests(unittest.TestCase):
    def setUp(self):
        self.trial_root = Path(tempfile.mkdtemp(prefix="blind-matrix-test-"))

    def tearDown(self):
        shutil.rmtree(self.trial_root, ignore_errors=True)

    def test_fixed_search_wdbc_positive_local_confirms(self):
        """T01 positive：证据驱动固定搜索应达到阈值并消费本地 test → valid。"""
        cells = build_matrix(["T01-wdbc-positive"], ["fixed_registered_search"], 1)
        summaries = run_matrix(cells, trial_root=self.trial_root, confirm_mode="local")
        self.assertEqual(1, len(summaries))
        summary = summaries[0]
        self.assertEqual("completed", summary["status"], summary)
        self.assertTrue(summary["integrity_ok"])
        self.assertTrue(summary["test_exposure"]["consumed"])
        self.assertTrue(is_valid_loop(summary), summary)

    def test_fixed_plan_negative_no_confirm(self):
        """T06 negative：固定计划证据盲 → 计划耗尽 concluded，不消费 test。"""
        cells = build_matrix(["T06-ts-negative"], ["same_model_same_tools_fixed_plan"], 1)
        summaries = run_matrix(cells, trial_root=self.trial_root, confirm_mode="local")
        summary = summaries[0]
        self.assertEqual("completed", summary["status"], summary)
        self.assertTrue(summary["integrity_ok"])
        # 负结果不请求确认，test 未消费
        self.assertFalse(summary["test_exposure"]["consumed"])
        self.assertEqual(False, summary["test_exposure"]["provided_to_worker"])
        self.assertTrue(is_valid_loop(summary), summary)

    def test_resume_skips_completed_cells(self):
        cells = build_matrix(["T01-wdbc-positive"], ["fixed_registered_search"], 1)
        run_matrix(cells, trial_root=self.trial_root, confirm_mode="local")
        self.assertTrue(cell_done(self.trial_root, cells[0]))
        # resume 会跳过已完成 cell，不重复执行
        resume = run_matrix(cells, trial_root=self.trial_root, confirm_mode="local", resume=True)
        self.assertEqual(1, len(resume))
        self.assertEqual("completed", resume[0]["status"])

    def test_report_aggregation(self):
        cells = build_matrix(["T01-wdbc-positive", "T06-ts-negative"],
                             ["fixed_registered_search", "same_model_same_tools_fixed_plan"], 1)
        summaries = run_matrix(cells, trial_root=self.trial_root, confirm_mode="local")
        report = build_report(summaries, planned_trajectories=len(cells))
        self.assertEqual(len(cells), report["planned_trajectories"])
        self.assertIn("by_comparator", report)
        self.assertIn("by_condition", report)
        self.assertIn("by_family", report)
        self.assertEqual(False, report["claims"]["adaptive_gain_claimed"])

    def test_failure_classification(self):
        self.assertEqual(None, classify_failure({"status": "completed"}))
        self.assertEqual("infra", classify_failure({"status": "failed", "error_type": "OSError"}))
        self.assertEqual("protocol_error",
                         classify_failure({"status": "failed", "error_type": "ProtocolError"}))


if __name__ == "__main__":
    unittest.main()