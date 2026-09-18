"""CI 上报仪表的门禁：它自己不许漏报、不许沉默、不许把 job 变成第二个红点。

起因是真实事故：run `35303020780` 的 ubuntu job 里，测试步骤非零退出、汇总步骤跑成功，
但 annotation 一条都没发——因为 `emit_annotations` 在「输出里没有 FAILED/ERROR 行」时
直接 return。免登录能看到的就只剩「Process completed with exit code 1」，等于仪表在最
需要它的分支上罢工。这类缺陷不会被任何单点测试发现，只能把「三种输入都必须开口」钉住。

同一个沉默的镜像分支是「绿跑」：旧版仪表挂在 `if: failure()` 上，job 绿时整步不执行，
而带原因的 skip 在外部与「真跑了」不可区分——「5 项真实沙箱用例」究竟跑了还是被跳过，
免登录侧永远看不出。本文件同时钉住新通道：绿跑必递 skip 名单、且不得误发 error。
"""
import os
import re
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
# run 35305181428 的 ubuntu：全量测试只有一个红项，而它是 `unittest.subTest` 里的
# 失败——pytest 短汇总打的是 SUBFAILED(...)，整份输出里一行 `FAILED ` 都没有。
SUBFAILED_SAMPLE = "\n".join([
    "....s" * 8,
    "SUBFAILED(row={'original_sha256': None, 'path': '/tmp/abs.py', 'replacement': 'x = 1\\n'}) "
    "tests/test_harness.py::RevisionProposalParsingTests::test_rejects_out_of_scope_paths",
    "SUBERROR(site='worker', path='a.py') tests/test_b.py::K2::teardown - ZeroDivisionError: 除零",
    "1 failed, 615 passed, 28 skipped, 8 warnings, 304 subtests passed in 222.22s"])
# 绿跑的真实形状（形状取自 .pytest_cache/scratch 对 pytest -q -rfEs 的实测输出）：
# 一行 `SKIPPED [N] 位置: 原因`，N 是 pytest 对同一（位置, 原因）的聚合次数。
GREEN_SAMPLE = "\n".join([
    "...." * 20,
    "SKIPPED [3] tests/test_research_confirmation_runner.py:41: 需要沙箱后端与 cryptography",
    "SKIPPED [2] .pytest_cache\\scratch\\test_skip_shapes.py:15: 集合字体渲染用例：宿主字体不同",
    "647 passed, 5 skipped in 284.35s"])
# 汇总行说跳过了、短汇总里却没有 `SKIPPED` 行：测试步骤忘了 `-rs`。
NO_RS_SAMPLE = "\n".join(["...." * 20, "642 passed, 5 skipped in 280.00s"])
# 跳过的原因多于 notice 上限（8 类）：名单不完整时必须自己说「还差多少」。
MANY_REASONS_SAMPLE = "\n".join(
    [f"SKIPPED [1] tests/test_x.py:{i + 10}: 原因编号 {i}" for i in range(7)]
    + ["640 passed, 7 skipped in 300.00s"])
TEN_REASONS_SAMPLE = "\n".join(
    [f"SKIPPED [1] tests/test_x.py:{i + 10}: 原因编号 {i}" for i in range(10)]
    + ["637 passed, 10 skipped in 300.00s"])


def sample_with_reason(text: str) -> str:
    return "\n".join([f"SKIPPED [1] tests/test_x.py:12: {text}", "647 passed, 1 skipped in 1.00s"])


def run_helper(root, output_text=None, status=None, summary=None, summary_env=True,
               outcome=None, extra_env=None):
    """在临时目录里跑一次上报脚本，返回 (returncode, stdout, 摘要文件内容)。"""
    target = Path(summary) if summary is not None else root / "summary.md"
    if output_text is not None:
        (root / "pytest-output.txt").write_text(output_text, encoding="utf-8")
    if status is not None:
        (root / "pytest-status.txt").write_text(f"{status}\n", encoding="utf-8")
    env = dict(os.environ, GITHUB_JOB="test-job")
    if summary_env:
        env["GITHUB_STEP_SUMMARY"] = str(target)
    else:
        env.pop("GITHUB_STEP_SUMMARY", None)
    if outcome is None:
        env.pop("TEST_STEP_OUTCOME", None)
    else:
        env["TEST_STEP_OUTCOME"] = outcome
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "pytest-output.txt"],
        cwd=str(root), capture_output=True, env=env)
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), text


def annotations(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("::error::")]


def warnings(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("::warning::")]


def notices(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("::notice::")]


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
        self.assertIn("no FAILED/ERROR/SUBFAILED/SUBERROR line", rows[0])
        self.assertIn("exited 1", rows[0], "不带退出码就分不开「测试红」与「进程没跑完」")
        self.assertIn("Fatal Python error", rows[0], "末尾输出是这一分支唯一的线索")
        self.assertIn("退出码 `1`", summary)

    def test_subtest_failures_are_not_read_as_no_failures(self):
        """只认 `FAILED`/`ERROR` 前缀会把子测试的红读成「没有失败行」。

        这是同一个漏报的第三个子形：旧版遇到 SUBFAILED_SAMPLE 会走「崩在采集前」分支，
        报一条带 tail 的 error，而 tail 与真实原因（哪个子测试红了）毫无关系。
        """
        code, out, summary = run_helper(self.root, SUBFAILED_SAMPLE, status=1)
        rows = annotations(out)
        self.assertEqual(0, code)
        self.assertTrue(rows, "子测试失败却一条 annotation 没发")
        joined = "\n".join(rows)
        self.assertNotIn("no FAILED/ERROR/SUBFAILED/SUBERROR line", joined,
                         "有子测试失败却报「没有失败行」= 漏报")
        self.assertIn(
            "tests/test_harness.py::RevisionProposalParsingTests::test_rejects_out_of_scope_paths",
            joined, "子测试行的测试名在参数化详情之后，名字得能提取出来")
        self.assertIn("tests/test_b.py::K2::teardown", joined)
        self.assertIn("根因聚类", rows[-1])
        self.assertIn("total=2", rows[-1])
        self.assertIn("除零", rows[-1], "SUBERROR 带异常摘要时按摘要聚类")
        self.assertIn("/tmp/abs.py", summary, "摘要里要能看出是哪个参数化子测试红了")

    def test_status_file_is_read_when_present_and_tolerated_when_absent(self):
        _, out, _ = run_helper(self.root, CRASH_SAMPLE)  # 没有 pytest-status.txt
        self.assertIn("exited ?", "\n".join(annotations(out)))

    def test_annotations_do_not_depend_on_the_summary_channel(self):
        """两条通道必须彼此独立：摘要写不成不能顺带让 annotation 闭嘴。

        旧写法里「没有 GITHUB_STEP_SUMMARY」与「写摘要抛 OSError」都在 emit_annotations
        之前 early-return——与「无 FAILED 行」是同一个缺陷的三个子形。
        """
        blocked = self.root / "blocked-dir"   # 对目录 open(..., "a") 必 OSError
        blocked.mkdir()
        for label, kwargs in (("写摘要失败", dict(summary=blocked)),
                              ("无环境变量", dict(summary_env=False))):
            with self.subTest(label):
                code, out, _ = run_helper(self.root, FAILED_SAMPLE, status=1, **kwargs)
                rows = annotations(out)
                self.assertEqual(0, code, f"{label}时 helper 不得自己变红")
                self.assertTrue(rows, f"{label}仍必须发 annotation，实际一条没发")
                self.assertIn("根因聚类", rows[-1])

    def test_green_run_lists_skips_without_error_annotations(self):
        """绿跑：skip 名单要递到免登录可读的两条通道，且不得误报一个不存在的故障。

        旧版把本输入（job 绿、一行失败也没有）读成「崩在采集前」，发一条误导的
        `::error::`——那就是仪表在绿跑上不可用的形状，也是当初它只挂在 `if: failure()`
        下的唯一理由。两个半边现在都得钉住。
        """
        code, out, summary = run_helper(self.root, GREEN_SAMPLE, status=0,
                                        outcome="success")
        self.assertEqual(0, code)
        self.assertEqual([], annotations(out), "绿跑不得发 ::error::")
        rows = notices(out)
        self.assertEqual(1, len(rows), f"skip 名单必须有一条 notice，实际 {rows}")
        self.assertIn("SKIP total=5", rows[0], "[3] + [2] 要按聚合次数相加，不是按行数")
        self.assertIn("需要沙箱后端与 cryptography", rows[0])
        self.assertIn("3x", rows[0], "按原因聚合才能看出「一大片 skip 是否同一根因」")
        self.assertIn("skip 名单", summary)
        self.assertIn("集合字体渲染用例：宿主字体不同", summary, "中文原因不得被洗成问号")
        self.assertIn("SKIPPED [2]", summary, "逐条清单要能定位到具体文件与行号")
        self.assertIn("647 passed, 5 skipped", summary, "末尾汇总行仍要递出来")

    def test_missing_rs_flag_is_reported_as_a_broken_instrument(self):
        """汇总行说跳过了 5 项却无 `SKIPPED` 行：测试步骤少了 `-rs`，名单正在静默丢失。"""
        code, out, summary = run_helper(self.root, NO_RS_SAMPLE, status=0, outcome="success")
        self.assertEqual(0, code)
        self.assertEqual([], annotations(out), "job 确实绿，报 error 是另一种漏报")
        self.assertEqual([], notices(out))
        rows = warnings(out)
        self.assertEqual(1, len(rows), f"仪表坏了必须吱声，实际 {rows}")
        self.assertIn("-rs", rows[0])
        self.assertIn("skip 名单不可读", rows[0])
        self.assertIn("测试步骤没带 `-rs`", summary)

    def test_partial_skip_list_declares_what_is_missing(self):
        """只报「前 N 类」而不说还差多少，就是仪表自己的漏报。

        不是假想：run `35312360291` 的 ubuntu 实到 `SKIP total=27`，当时的前 4 类只能
        对上 24 次，剩下 3 次归不入任何一类——读的人无从知道名单不完整。
        反方向也要卡住：全部列出时不得虚报「未列出」（那会被读成名单仍有隐情）。
        """
        _, out, _ = run_helper(self.root, TEN_REASONS_SAMPLE, status=0, outcome="success")
        rows = notices(out)
        self.assertEqual(1, len(rows))
        self.assertIn("SKIP total=10", rows[0])
        self.assertIn("列出 8/10 类", rows[0])
        self.assertIn("另有 2 类 / 2 次未列出", rows[0],
                      "未列出的量必须自己报出来，不然「前 N 类」会被当成全量")
        self.assertIn("job summary", rows[0], "得指出去哪看逐条名单")

        _, out, _ = run_helper(self.root, MANY_REASONS_SAMPLE, status=0, outcome="success")
        rows = notices(out)
        self.assertIn("列出 7/7 类", rows[0])
        self.assertNotIn("未列出", rows[0], "全部列出时不得再说「还有没列出的」")

    def test_long_skip_reasons_are_cut_to_keep_the_notice_readable(self):
        """单条原因过长会顶爆 notice（零依赖 job 实测有 12 类）：按字截断，total 仍完整。"""
        long_reason = "集合字体渲染用例：宿主字体差异" * 12          # 远超 70 字
        _, out, _ = run_helper(self.root, sample_with_reason(long_reason), status=0,
                               outcome="success")
        rows = notices(out)
        self.assertEqual(1, len(rows), f"应当只发一条 skip notice，实际 {rows}")
        self.assertIn("SKIP total=1", rows[0])
        self.assertIn(long_reason[:70], rows[0], "前 70 字是判读依据，不得被剪掉")
        self.assertNotIn(long_reason[:90], rows[0], "超出 70 字的部分应被截断")
        self.assertIn(long_reason[:70] + "…", rows[0],
                      "截断必须留记，不然剪过的原因会被当成完整原因读")

        _, out, _ = run_helper(self.root, sample_with_reason("需要沙箱后端"), status=0,
                               outcome="success")
        self.assertNotIn("…", notices(out)[0], "没被截断的原因不得虚带截断记")

    def test_truncated_skip_list_says_it_was_truncated(self):
        """逐条清单被上限切断时，摘要里得写明“共 N 行、列了前 M 行”。"""
        code, _, summary = run_helper(self.root, MANY_REASONS_SAMPLE, status=0,
                                       outcome="success",
                                       extra_env={"CI_SUMMARY_MAX_SKIP_LINES": "3"})
        self.assertEqual(0, code)
        self.assertIn("最多显示 3 行", summary)
        self.assertIn("另有 4 行未列出", summary)
        self.assertIn("共 7 行", summary)

    def test_green_but_inconsistent_output_is_reported(self):
        """outcome=success 与失败行/非零退出码不能共存：共存说明上报链路坏了。"""
        for label, kwargs in (("带失败行", dict(output_text=FAILED_SAMPLE, status=1)),
                              ("退出码非零", dict(output_text=GREEN_SAMPLE, status=2))):
            with self.subTest(label):
                code, out, _ = run_helper(self.root, outcome="success", **kwargs)
                rows = annotations(out)
                self.assertEqual(0, code, "helper 自身仍不得变红")
                self.assertEqual(1, len(rows), f"{label}时应报一条不自洽，实际 {rows}")
                self.assertIn("不自洽", rows[0])

    def test_skips_are_reported_on_red_runs_too(self):
        """红跑的 skip 名单不能丢：同一个 job 里「红」与「空转」常常同时发生。"""
        red = ("FAILED tests/test_a.py::K::t - ProtocolError: 沙箱不可用\n"
               "SKIPPED [5] tests/test_b.py:9: 缺可选依赖 orchestration\n"
               "1 failed, 640 passed, 5 skipped in 300.00s")
        code, out, summary = run_helper(self.root, red, status=1, outcome="failure")
        rows = annotations(out)
        self.assertEqual(0, code)
        self.assertTrue(rows, "红跑仍必须发失败 annotation")
        self.assertIn("根因聚类", rows[-1], "失败结论仍是最后一条（不会被顶掉）")
        self.assertIn("SKIP total=5", "\n".join(notices(out)),
                      "红跑也要发 skip 的 notice（它排在失败项之前，被 10 条上限顶掉的是它）")
        self.assertIn("缺可选依赖 orchestration", summary,
                      "就算 notice 被顶掉，摘要里的逐条 skip 名单不得丢")

    def test_skip_lines_are_not_counted_as_failures(self):
        """`SKIPPED` 不得被计入失败条目，否则绿跑会被读成「有 N 项红」。

        注：「永不沉默」那条 error 会把末尾几行当 tail 打出来，tail 里出现 `SKIPPED`
        是故意的（它是当时唯一的线索），所以这里卡的是摘要里的失败计数。
        """
        code, out, summary = run_helper(self.root, GREEN_SAMPLE, status=0, outcome="failure")
        self.assertEqual(0, code)
        joined = "\n".join(annotations(out))
        self.assertIn("no FAILED/ERROR/SUBFAILED/SUBERROR line", joined,
                      "outcome 非绿且无失败行时，「永不沉默」分支仍要照旧发 error")
        self.assertIn("失败/错误条目 0 项", summary, "3+2 次 skip 不得被计成失败")
        self.assertIn("跳过（skip）条目", summary)


STEP = re.compile(r"(?ms)^      - name: (?P<name>.+?)\n(?P<body>.*?)(?=^      - |\Z)")


def reporter_steps(text):
    """所有调用了上报脚本的步骤：[(展示名, 步骤体)]。

    为什么按步骤看而不是比全局计数：旧写法拿 `count(ci_failure_summary.py)` == `count(if:
    always())` 当判据，本质是赌「全仓只有这一种仪表、且每个 always() 都是上报步骤」。端到端
    job 的兜底仪表也是上报步骤、却不调这个 helper，赌注当场输——门禁红在一处本来正确的改动上。
    按步骤判才能既拦住「仪表挂在 failure()」，也不惩罚新增的第二套仪表。
    """
    return [(match.group("name").strip(), match.group("body"))
            for match in STEP.finditer(text)
            if "ci_failure_summary.py" in match.group("body")]


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

    def test_the_reporter_runs_on_green_jobs_too(self):
        """挂在 `if: failure()` 上的仪表，在绿跑上等于不存在——skip 名单就是给绿跑的。"""
        text = WORKFLOW.read_text(encoding="utf-8")
        steps = reporter_steps(text)
        self.assertGreater(len(steps), 0, "ci.yml 里已不再使用上报脚本，本用例该删")
        silent = [name for name, body in steps if "if: always()" not in body]
        self.assertEqual([], silent,
                         f"这些上报步骤不是 always()，绿跑上不会执行：{silent}")
        self.assertNotIn("if: failure()", text,
                         "已无需要「只在红时跑」的上报步骤，留着就会制造沉默分支")

    def test_that_contract_bites(self):
        """反向验证：把某一步的 `always()` 换成别的条件，上面那条必须拓出来。"""
        text = WORKFLOW.read_text(encoding="utf-8")
        steps = reporter_steps(text)
        assert len(steps) > 1, "只有一处上报步骤时本反向验证拓不出东西"
        name, body = steps[0]
        assert "if: always()" in body
        broken = text.replace(body, body.replace("if: always()", "if: success()", 1), 1)
        self.assertTrue(broken != text, "反向验证的样本没改动任何东西")
        self.assertEqual([name], [n for n, b in reporter_steps(broken) if "if: always()" not in b],
                         "改掉的那一步没被识别成沉默仪表：门禁不拦东西")
        self.assertEqual(len(steps), len(reporter_steps(broken)),
                         "上报步骤的识别数量变了：按步骤扫描的形状不稳")

    def test_outcome_is_passed_to_every_reporter(self):
        """没有 TEST_STEP_OUTCOME，绿跑会被「永不沉默」分支误报成 error。"""
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count("ci_failure_summary.py"),
                         text.count("TEST_STEP_OUTCOME: ${{ steps.pytest.outcome }}"),
                         "每处调用都要拿到测试步骤的 outcome（来自主步骤的 id: pytest）")
        self.assertEqual(text.count("ci_failure_summary.py"), text.count("id: pytest"))

    def test_pytest_invocations_ask_for_the_skip_list(self):
        """逐行检查：写 pytest-output.txt 的命令必须带 `-rs`，否则名单从源头就没落盘。"""
        text = WORKFLOW.read_text(encoding="utf-8")
        calls = [line for line in text.splitlines() if "-m pytest" in line]
        self.assertGreater(len(calls), 0, "ci.yml 里已不跑 pytest，本用例该删")
        for line in calls:
            self.assertIn("-rfEs", line,
                          f"该行未带 `-rfEs`，skip 名单不会出现在输出里：{line.strip()}")


if __name__ == "__main__":
    unittest.main()
