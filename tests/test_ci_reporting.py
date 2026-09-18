"""CI 失败上报仪表的门禁：它自己不许漏报、不许沉默、不许把 job 变成第二个红点。

起因是真实事故：run `35303020780` 的 ubuntu job 里，测试步骤非零退出、汇总步骤跑成功，
但 annotation 一条都没发——因为 `emit_annotations` 在「输出里没有 FAILED/ERROR 行」时
直接 return。免登录能看到的就只剩「Process completed with exit code 1」，等于仪表在最
需要它的分支上罢工。这类缺陷不会被任何单点测试发现，只能把「三种输入都必须开口」钉住。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "ci_failure_summary.py"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
MAX_ANNOTATIONS = 8  # GitHub 对 push run 每 check run 只留最近 10 条，系统自己占 2 条

FAILED_SAMPLE = "\n".join(
    ["....F" * 8]
    + [f"FAILED tests/test_a.py::K::t{i} - AssertionError: 中文字体缺失" for i in range(3)]
    + ["FAILED tests/test_b.py::K2::other - ProtocolError: 沙箱不可用",
       "1 failed, 633 passed in 300.00s"])
CRASH_SAMPLE = "\n".join([
    "..........",
    "Fatal Python error: Aborted",
    "Current thread 0x0000000c (most recent call first):",
    "  <no Python frame>",
])


def run_helper(root, output_text=None, status=None):
    """在临时目录里跑一次上报脚本，返回 (returncode, stdout, 摘要文件内容)。"""
    summary = root / "summary.md"
    if output_text is not None:
        (root / "pytest-output.txt").write_text(output_text, encoding="utf-8")
    if status is not None:
        (root / "pytest-status.txt").write_text(f"{status}\n", encoding="utf-8")
    env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary), GITHUB_JOB="test-job")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "pytest-output.txt"],
        cwd=str(root), capture_output=True, env=env)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), summary.read_text(encoding="utf-8")


def annotations(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("::error::")]


class CiFailureSummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_output_still_reports(self):
        code, out, summary = run_helper(self.root)
        self.assertEqual(0, code, "诊断步骤自己不得非零退出")
        self.assertIn("BEFORE the test step", "\n".join(annotations(out)))
        self.assertIn("测试步骤之前", summary)

    def test_failure_lines_are_aggregated_within_the_annotation_budget(self):
        code, out, summary = run_helper(self.root, FAILED_SAMPLE, status=1)
        rows = annotations(out)
        self.assertEqual(0, code)
        self.assertTrue(rows, "有失败清单却一条 annotation 都没发")
        self.assertLessEqual(len(rows), MAX_ANNOTATIONS,
                             f"超过 {MAX_ANNOTATIONS} 条会被 GitHub 截断，等于漏报")
        # 根因聚类必须是最后一条：被挤掉时先没的是细节不是结论
        self.assertIn("根因聚类", rows[-1])
        self.assertIn("AssertionError", rows[-1], "聚类保留的是异常消息，中文原文不得丢")
        self.assertIn("tests/test_b.py::K2::other", "\n".join(rows))

    def test_no_failure_lines_must_not_be_silent(self):
        """这条就是 run 35303020780 的 ubuntu：非零退出 + 没有 FAILED 行 → 过去会沉默。"""
        code, out, summary = run_helper(self.root, CRASH_SAMPLE, status=1)
        rows = annotations(out)
        self.assertEqual(0, code)
        self.assertEqual(1, len(rows), f"该分支必须至少报一条，实际 {rows}")
        self.assertIn("no FAILED/ERROR line", rows[0])
        self.assertIn("exited 1", rows[0], "不带退出码就分不开「测试红」与「进程没跑完」")
        self.assertIn("Fatal Python error", rows[0], "末尾输出是这一分支唯一的线索")
        self.assertIn("退出码 `1`", summary)

    def test_status_file_is_read_when_present_and_tolerated_when_absent(self):
        _, out, _ = run_helper(self.root, CRASH_SAMPLE)  # 没有 pytest-status.txt
        self.assertIn("exited ?", "\n".join(annotations(out)))


class WorkflowReportingContractTests(unittest.TestCase):
    """ci.yml 与上报脚本的契约：每个用 helper 的 job 都必须把退出码落盘。"""

    def test_every_job_using_the_helper_records_the_exit_code(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        users = text.count("ci_failure_summary.py")
        self.assertGreater(users, 0, "ci.yml 里已不再使用上报脚本，本用例该删")
        writers = text.count("pytest-status.txt")
        self.assertGreaterEqual(writers, users,
                                f"{users} 处调用上报脚本，却只有 {writers} 处涉及 pytest-status.txt："
                                "helper 分不开「测试红」与「崩在采集前」")


if __name__ == "__main__":
    unittest.main()
