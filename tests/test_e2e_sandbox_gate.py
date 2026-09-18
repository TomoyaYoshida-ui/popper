"""端到端取证脚本（C7 验证项 3）的门禁：它的判据不许松、不许假绿、不许拿平台差异造假红。

为什么单测不去真跑一遍端到端（那是 `linux-sandbox-e2e` job 的活），却仍要在这里测它：
取证脚本本身是**门禁**，而门禁最典型的失效不是报错，是「该红的时候绿」。`gate()` 被刻意
写成纯函数（只吃一个 context，不碰磁盘不碰进程），所以这里能喂假 fixture 逐条验它咬不咬。

三类用例缺一不可：
1. 干净 fixture 必须**全绿**——否则下面的逐条判红可能是顺带红，测不出东西；
2. 每种坏形状各红一次，且红在预期的那条标签上——写不出这一类，就等着 `gate` 慢慢变成
   一个「只要跑完就绿」的装饰物；
3. 平台差异**不得**判红（能力清单说不主张的东西，观察值再奇怪也只记录）。
外加 ci.yml 的接线契约：脚本存在但没被 job 调用、或被调用却没钉住后端、或仪表能在脚本
崩掉时沉默，都是这文件该拦的形状。
"""
import copy
import importlib.util
import io
import contextlib
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "e2e_sandbox_experiment.py"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
SANDBOX_ACTION = REPO / ".github" / "actions" / "sandbox-env" / "action.yml"

_spec = importlib.util.spec_from_file_location("e2e_sandbox_experiment", SCRIPT)
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)

LINUX = "linux_bubblewrap"
SANDBOX_TRUST = e2e.SANDBOX_TRUST


# --------------------------------------------------------------------------- fixture
def witness(**over):
    """一次执行的完整见证行（Linux/bwrap 的真实形状）。

    缺任何一行都会被 `witness-incomplete` 判红，所以这里必须齐 —— 少一行而门禁照样绿，
    意味着候选根本没跑到见证代码。
    """
    base = {"pid": "7",                       # 新 pid namespace 内必然是个位数
            "scope_write": "GRANTED",         # 正向对照：作用域内必须写得动
            "escape_write_project": "DENIED:OSError",   # --ro-bind / 下 EROFS
            "escape_write_outside": "DENIED:OSError",
            "holdout_read": "BLOCKED_EMPTY",  # /dev/null 屏蔽 ⇒ 读到空
            "command_block": "DENIED:PermissionError",
            "net_raw": "DENIED:OSError",      # 空 netns
            "rlimit_cpu": "30",
            "shim_visible": "GRANTED"}
    base.update(over)
    return base


def run(rid, split, scheme, mean, *, trust=SANDBOX_TRUST, n_seeds=3, units=3,
        missing_results=False, logs=None):
    if missing_results:
        return {"id": rid, "missing_results": True, "logs": {}}
    if logs is None:
        logs = {f"stderr-seed-{i}": witness() for i in range(units)}
    return {"id": rid, "logs": logs,
            "payload": {"split": split, "trust": trust, "n_seeds": n_seeds,
                        "mean": mean, "config": {"scheme": scheme}}}


def good_runs():
    """dev：基线 euler + 候选 midpoint/rk4；test：基线 euler + 选中 rk4（实测值取自本机跑通的那次）。

    基线是 **Linux/bwrap 形状**：test 划分里保留集必须读到封读后的形状（BLOCKED_*）。
    dev 划分保留集可读是已登记的设计内边界（封读窗口只覆盖 test 消费阶段）。
    """
    return [run("r-dev-euler", "dev", "euler", 1.0295,
                logs={f"stderr-seed-{i}": witness(holdout_read="READABLE") for i in range(3)}),
            run("r-dev-midpoint", "dev", "midpoint", 2.0518,
                logs={f"stderr-seed-{i}": witness(holdout_read="READABLE") for i in range(3)}),
            run("r-dev-rk4", "dev", "rk4", 4.0565,
                logs={f"stderr-seed-{i}": witness(holdout_read="READABLE") for i in range(3)}),
            run("r-test-euler", "test", "euler", 1.0350),
            run("r-test-rk4", "test", "rk4", 4.0670)]


def good_context(**over):
    context = {"backend": LINUX, "expect_backend": LINUX,
               "platform_claims": {"namespace_pid": True, "kernel_net": True,
                                   "observable_rlimits": True},
               "runs": good_runs(), "canaries": [],
               "status": {"phase": "completed",
                          "claim": {"status": "supports_threshold"},
                          "selected": {"scheme": "rk4"}},
               "replay": {"status": "verified", "runs_recomputed": 5, "claim_recomputed": True},
               "isolation": {"backend": LINUX},
               "report_text": "执行模式：OS 沙箱 5 次（写作用域限定运行工作区）"}
    context.update(over)
    return copy.deepcopy(context)


def labels(context):
    return {label for label, _ in e2e.gate(context)}


def corrupted(*mutators):
    context = good_context()
    for mutator in mutators:
        mutator(context)
    return context


def set_witness(**over):
    """改所有 run 的所有见证行。"""
    def apply(context):
        for item in context["runs"]:
            for unit in item["logs"]:
                item["logs"][unit].update(over)
    return apply


def set_witness_in(split, **over):
    """只改某个 split 的见证行。"""
    def apply(context):
        for item in context["runs"]:
            if (item.get("payload") or {}).get("split") != split:
                continue
            for unit in item["logs"]:
                item["logs"][unit].update(over)
    return apply


def drop(key):
    def apply(context):
        for item in context["runs"]:
            for unit in item["logs"]:
                item["logs"][unit].pop(key, None)
    return apply


def first_run(**over):
    def apply(context):
        item = context["runs"][0]
        payload = item.setdefault("payload", {})
        for key, value in over.items():
            if "." in key:                      # 形如 config.scheme
                head, tail = key.split(".", 1)
                payload.setdefault(head, {})[tail] = value
            else:
                payload[key] = value
    return apply


# --------------------------------------------------------------------------- 干净输入
class CleanContextTests(unittest.TestCase):
    def test_the_happy_path_is_actually_green(self):
        """反证基线：干净 fixture 必须一条都不红。

        这条一红，下面所有「坏形状判红」的断言都失去意义（红可能是 fixture 顺带触发的）。
        """
        self.assertEqual([], e2e.gate(good_context()))

    def test_windows_shape_is_green_too(self):
        """Windows 形状：dev 阶段保留集可读、无 resource 模块、pid 是宿主四位数、net 靠代理层。

        同一份 `gate()` 两平台都要能绿——它只能在**本平台主张的**约束上判红。
        """
        context = good_context(backend="windows_low_integrity", expect_backend="windows_low_integrity",
                               platform_claims={"namespace_pid": False, "kernel_net": False,
                                                "observable_rlimits": False},
                               isolation={"backend": "windows_low_integrity"})
        for item in context["runs"]:
            for unit in item["logs"]:
                item["logs"][unit].update(pid="18336", rlimit_cpu="NA:<class 'ImportError'>",
                                          net_raw="GRANTED",
                                          escape_write_project="DENIED:PermissionError",
                                          escape_write_outside="DENIED:PermissionError",
                                          holdout_read="READABLE"
                                          if item["payload"]["split"] == "dev"
                                          else "BLOCKED:PermissionError")
        self.assertEqual([], e2e.gate(context))


# --------------------------------------------------------------------------- 坏形状必须红
class GateBitesTests(unittest.TestCase):
    CASES = [
        # (标签, 变异, 额外说明)
        ("no-backend", lambda c: c.update(backend=None, expect_backend=None,
                                          isolation={"backend": None}),
         "没有可用后端时，端到端只是「在宿主上跑了一遍」"),
        ("backend-mismatch", lambda c: c.update(expect_backend="windows_low_integrity"),
         "实际后端与钉住的期望后端不一致"),
        ("phase", lambda c: c["status"].update(phase="searching"), "生命周期没走完"),
        ("claim", lambda c: c["status"]["claim"].update(status="insufficient"), "结论不达阈值"),
        ("selected", lambda c: c["status"]["selected"].update(scheme="midpoint"), "选中候选不对"),
        ("run-shape", lambda c: c.update(runs=c["runs"][:4]), "run 数不等于 dev3+test2"),
        ("run-shape", lambda c: c.update(runs=[r for r in c["runs"]
                                               if r["payload"]["split"] != "dev"][:2]
                                         + c["runs"][3:]), "dev/test 配比不对"),
        ("missing-results", lambda c: c["runs"].__setitem__(
            0, run("r-broken", "dev", "euler", 1.0, missing_results=True)), "缺 results.json"),
        ("trust", lambda c: [c["runs"][i]["payload"].update(trust="controller_scored_trusted_local")
                             for i in range(5)], "沙箱路径被静默降级为 trusted-local"),
        ("n_seeds", first_run(n_seeds=1), "重复数与注册配置不符"),
        ("unknown-scheme", first_run(**{"config.scheme": "adam"}), "没登记过的方案"),
        ("metric-truth", first_run(mean=0.42), "实测阶偏离数学真值 ⇒ 执行被挡坏了"),
        ("no-witness-log", lambda c: c["runs"][0]["logs"].clear(), "该 run 没有任何见证"),
        ("witness-incomplete", drop("scope_write"), "少了正向对照那一行"),
        ("scope-control", set_witness(scope_write="DENIED:OSError"),
         "作用域内写不动：两条 DENIED 就可能是假通过"),
        ("escape-write", set_witness(escape_write_outside="GRANTED"), "作用域外可写"),
        ("command-block", set_witness(command_block="GRANTED"), "高危命令直接跑通了"),
        ("shim-visible", set_witness(shim_visible="HIDDEN"), "拦截 shim 被 --tmpfs /tmp 遮住"),
        ("holdout-seal", set_witness_in("test", holdout_read="READABLE"), "test 划分里保留集仍可读"),
        ("namespace-pid", set_witness(pid="41234"), "pid 不像 namespace 内的进程"),
        ("rlimit-claim", set_witness(rlimit_cpu="NA"), "主张内核强制 rlimit 却读不到"),
        ("rlimit-claim", set_witness(rlimit_cpu="99999"), "RLIMIT_CPU 未收紧"),
        ("net-block", set_witness(net_raw="GRANTED"), "主张空 netns 断网却连得上"),
        ("canary-on-host", lambda c: c.update(canaries=["/tmp/x/e2e-escape-outside.txt"]),
         "宿主侧独立复核发现越界写真的落了盘"),
        ("replay", lambda c: c["replay"].update(status="unverified"), "证据链重算未通过"),
        ("replay-scope", lambda c: c["replay"].update(runs_recomputed=2), "只重算了部分 run"),
        ("replay-claim", lambda c: c["replay"].update(claim_recomputed=False), "结论未能重算"),
        ("isolation-backend", lambda c: c.update(isolation={"backend": "win_lowil"}),
         "能力清单签成内部模块名（C7.1 已立规矩：回执必须用稳定名）"),
        ("report-trust-text", lambda c: c.update(report_text="执行模式：可信本地代码"),
         "report.md 的产物口径与实际执行路径漂移"),
    ]

    def test_each_broken_shape_is_caught(self):
        for expected, mutator, why in self.CASES:
            with self.subTest(label=expected, why=why):
                found = labels(corrupted(mutator))
                self.assertIn(expected, found,
                              f"这种坏形状没被拦下（期望标签 {expected}）：{why}")

    def test_corruption_list_covers_every_label_the_gate_can_emit(self):
        """门禁能报的每条标签都必须有人测到。

        防的是「gate 里新增一条判据却没人验它咬不咬」——那和没写判据等价，但看起来更可信。
        """
        emitted = set(re.findall(r'failures\.append\(\("([a-z_-]+)"',
                                 SCRIPT.read_text(encoding="utf-8")))
        tested = {expected for expected, _, _ in self.CASES}
        self.assertEqual(set(), emitted - tested,
                         f"这些判据没有任何用例覆盖：{sorted(emitted - tested)}")
        self.assertEqual(set(), tested - emitted,
                         f"用例在期待已不存在的标签（判据被删了却留着测试）：{sorted(tested - emitted)}")


class CommandBlockPrecisionTests(unittest.TestCase):
    """`command_block` 只接受 `DENIED:PermissionError`，不接受任何 DENIED。

    这是本机跑通后复查出来的口子：ubuntu runner 不一定装 curl，缺二进制时
    `FileNotFoundError` 也是 `DENIED`，而它恰恰意味着拦截层没参与。
    """

    def test_missing_binary_is_not_counted_as_interception(self):
        context = corrupted(set_witness(command_block="DENIED:FileNotFoundError"))
        self.assertIn("command-block", labels(context))

    def test_other_denial_reason_is_not_counted_as_interception(self):
        context = corrupted(set_witness(command_block="DENIED:OSError"))
        self.assertIn("command-block", labels(context))

    def test_permission_error_is_the_accepted_shape(self):
        self.assertTrue(e2e.blocked_by_interception("DENIED:PermissionError"))
        self.assertFalse(e2e.blocked_by_interception("GRANTED"))
        self.assertFalse(e2e.blocked_by_interception(""))


class PlatformDifferencesAreNotFalseRedsTests(unittest.TestCase):
    """能力清单不主张的东西，观察值再离谱也只记录、不判红。

    否则这条 job 会在「平台差异」上红，而红一次的成本是一整轮 CI + 一次 API 取证预算。
    """

    def test_unclaimed_capabilities_do_not_turn_red(self):
        context = good_context(platform_claims={"namespace_pid": False, "kernel_net": False,
                                                "observable_rlimits": False})
        for item in context["runs"]:
            for unit in item["logs"]:
                item["logs"][unit].update(pid="18336", net_raw="GRANTED", rlimit_cpu="NA")
        self.assertEqual(set(), labels(context))

    def test_holdout_window_is_scoped_to_the_test_split(self):
        """同一份 `READABLE` 观察值：只许点名 test 划分的 run，dev 划分是已登记的边界。

        只测「dev 不红」是空测（干净 fixture 里 dev 本来就是 READABLE）；把两边一并翻成
        READABLE，才能同时钉住「窗口别搞错」与「判红范围别扩大化」。
        """
        context = corrupted(set_witness(holdout_read="READABLE"))
        named = {detail.split("/")[0]
                 for label, detail in e2e.gate(context) if label == "holdout-seal"}
        self.assertEqual({"r-test-euler", "r-test-rk4"}, named,
                         "保留集封读的判红范围漂了：它只覆盖 test 消费阶段")


# --------------------------------------------------------------------------- 见证行解析
class WitnessParsingTests(unittest.TestCase):
    def test_parses_only_witness_lines(self):
        text = "\n".join(["随便一行训练输出 step=3 loss=0.5",
                          "POPPER-E2E-WITNESS pid=7",
                          "POPPER-E2E-WITNESS scope_write=GRANTED",
                          "Warning: POPPER-E2E-WITNESS 不该出现在行中间"])
        seen = e2e.parse_witness(text)
        self.assertEqual({"pid": "7", "scope_write": "GRANTED"}, seen)

    def test_value_may_contain_equals_sign(self):
        seen = e2e.parse_witness("POPPER-E2E-WITNESS note=a=b=c")
        self.assertEqual("a=b=c", seen["note"])

    def test_empty_log_yields_empty_dict(self):
        self.assertEqual({}, e2e.parse_witness(""))

    def test_the_injected_snippet_carries_every_required_key(self):
        """注入的见证代码与 `WITNESS_KEYS` 必须同步：少打一行 = 该 run 直接判红。

        这条是防「改了 WITNESS_KEYS 忘了改 snippet」（反之亦然）的漂移门禁——snippet 跑在
        候选进程里，任何语法/名字错误都只会以「缺观察行」的形状暴露，很难查。
        """
        snippet = e2e.WITNESS_SNIPPET
        for key in e2e.WITNESS_KEYS:
            # 两类输出途径都算：`line(...)` 直接报，`probe(...)` 跑一个动作后报 GRANTED/DENIED
            self.assertRegex(snippet, r'(?:line|probe)\(\s*"%s"' % key,
                             f"见证代码没有输出 {key}，但它被列进 WITNESS_KEYS")
        # 候选进程拿不到本模块的名字，所以 snippet 只能自包含
        for forbidden in ("BLOCKED_PREFIX", "EXPECTED_ORDER", "WITNESS_PREFIX =", "SANDBOX_TRUST"):
            self.assertNotIn(forbidden, snippet,
                             f"见证代码引用了驱动模块的常量 {forbidden}，候选进程里必然 NameError")


# --------------------------------------------------------------------------- 上报预算
class InstrumentationBudgetTests(unittest.TestCase):
    def test_annotations_stay_inside_the_github_budget(self):
        """GitHub 每 check run 只留最近 10 条 annotation（系统自己占 2 条）。

        端到端 job 与单测 job 拆开就是为了这条预算不互相顶掉；这里钉住自己那一侧不超。
        """
        context = good_context()
        failures = [("label-%d" % i, "detail") for i in range(30)]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            e2e.emit("summary text", failures)
        lines = [line for line in buffer.getvalue().splitlines() if line.startswith("::")]
        self.assertLessEqual(len(lines), e2e.MAX_ANNOTATIONS + 1,
                             f"发了 {len(lines)} 条，超出预算会把关键结论挤出可见范围")
        self.assertTrue(any("FAILED total=30" in line for line in lines),
                        "红跑必须自报失败总数与标签集合")

    def test_green_run_speaks_once_and_sends_no_error(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            e2e.emit("summary text", [])
        out = buffer.getvalue()
        self.assertIn("::notice::", out)
        self.assertNotIn("::error::", out)

    def test_summary_names_the_known_truth_for_each_scheme(self):
        """绿跑的 job summary 也要能自证「数值对得上数学真值」，不是只说「跑完了」。"""
        summary = e2e.summarize(good_context(), [])
        for scheme, truth in e2e.EXPECTED_ORDER.items():
            self.assertIn(scheme, summary)
            self.assertIn(f"{truth:.0f}" if float(truth).is_integer() else str(truth), summary)
        self.assertIn("通过", summary)


# --------------------------------------------------------------------------- CI 接线契约
JOB_BLOCK = re.compile(r"(?ms)^  (?P<name>linux-sandbox-e2e):\n.*?(?=^  [A-Za-z0-9_-]+:|\Z)")


def e2e_job_text():
    match = JOB_BLOCK.search(WORKFLOW.read_text(encoding="utf-8"))
    return match.group(0) if match else None


class WorkflowWiringTests(unittest.TestCase):
    def test_the_e2e_job_exists_and_calls_the_driver(self):
        block = e2e_job_text()
        assert block, "ci.yml 里没有 linux-sandbox-e2e job：C7 验证项 3 没有真实平台验证点"
        self.assertIn(".github/scripts/e2e_sandbox_experiment.py", block,
                      "job 没真调用取证脚本，门禁等于不存在")
        self.assertIn("--expect-backend linux_bubblewrap", block,
                      "没钉住期望后端：换成另一个机制也能绿")

    def test_the_e2e_job_installs_no_extras(self):
        """这条 job 同时是「核心零运行时依赖」在真实执行路径上的验证点。"""
        block = e2e_job_text()
        assert block
        self.assertIn("pip install -e .\n", block + "\n",
                      "端到端 job 应该只装 `-e .`，装 extras 会让零依赖主张失去这个验证点")
        self.assertNotIn("-e \".[", block, "端到端 job 里不该装任何 extra")

    def test_exit_code_survives_the_pipeline_and_the_instrument_cannot_silence(self):
        block = e2e_job_text()
        assert block
        self.assertIn("e2e-status.txt", block, "退出码没落盘（管道会把它洗白）")
        self.assertRegex(block, r"if: always\(\)",
                         "兜底仪表必须挂在 always()：脚本在能递结论之前就崩掉时也要开口")
        self.assertIn("[e2e-sandbox]", block,
                      "兜底步骤必须按脚本的自报前缀判断有没有递出结论")

    def test_environment_prep_has_one_source_for_both_ubuntu_jobs(self):
        """两个 ubuntu job 的沙箱环境准备必须共用一个 composite action。

        两份抄本迟早漂成「一个 job 修了、另一个还红着（或更糟：还绿着）」——而 bwrap 的
        AppArmor userns 坑正是那种「修过一次就忘了第二处」的东西。
        """
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(2, len(re.findall(r"uses: \./\.github/actions/sandbox-env", text)),
                         "两个 ubuntu job 都该用同一个 action 准备沙箱环境")
        self.assertNotIn("apt-get install -y bubblewrap", text,
                         "ci.yml 里不该再留一份 bubblewrap 安装（漂移源）")
        self.assertTrue(SANDBOX_ACTION.is_file(), "sandbox-env action 被删了")

    def test_the_wiring_assertions_themselves_bite(self):
        """反向验证：把契约项抽掉，上面的断言必须红——否则这些测试只是装饰。"""
        block = e2e_job_text()
        assert block
        stripped = block.replace("--expect-backend linux_bubblewrap ", "")
        self.assertTrue(stripped != block)
        self.assertNotIn("--expect-backend linux_bubblewrap", stripped)
        self.assertIn("always()", block)
        self.assertNotIn("always()", block.replace("if: always()", "if: success()"))


# --------------------------------------------------------------------------- 被端到端暴露的产品口径
class ProductReceiptConsistencyTests(unittest.TestCase):
    """这两处是端到端**第一次真跑**才暴露出来的：单测层看不见（它们只测单条断言）。"""

    def test_isolation_report_uses_the_stable_backend_name(self):
        from popper import sandbox
        from popper.isolation import isolation_status
        from popper.sandbox import _RECEIPT_BACKEND_NAMES

        status = isolation_status(REPO)
        backend = status["backend"]
        self.assertEqual(sandbox.execution_backend_name(), backend)
        self.assertNotIn(backend, set(_RECEIPT_BACKEND_NAMES),
                         f"能力清单里漏出内部模块名 {backend!r}：对外凭据必须用稳定名")
        if backend is not None:
            self.assertIn(backend, set(_RECEIPT_BACKEND_NAMES.values()))

    def test_execution_mode_line_follows_the_actual_trust_field(self):
        from popper.core import execution_mode_line

        sandboxed = execution_mode_line([{"trust": SANDBOX_TRUST}] * 3 + [{"trust": "x"}])
        self.assertIn("OS 沙箱 3 次", sandboxed)
        self.assertNotIn("可信本地", sandboxed)

        trusted = execution_mode_line([{"trust": "controller_scored_trusted_local"}] * 2)
        self.assertIn("可信本地代码 2 次", trusted)
        self.assertNotIn("OS 沙箱", trusted)

        mixed = execution_mode_line([{"trust": SANDBOX_TRUST},
                                     {"trust": "controller_scored_trusted_local"}])
        self.assertIn("OS 沙箱 1 次", mixed)
        self.assertIn("可信本地代码 1 次", mixed)

        self.assertIn("尚无执行记录", execution_mode_line([]))

    def test_the_retired_hardcoded_sentence_cannot_come_back(self):
        """被端到端拦下的那句假陈述不许回来：它曾写死在 report() 里，与 results.json 矛盾。"""
        from popper.core import execution_mode_line

        for results in ([{"trust": SANDBOX_TRUST}], [{"trust": "controller_scored_trusted_local"}], []):
            self.assertNotIn("尚未提供容器安全隔离", execution_mode_line(results))
        # 「执行模式」这个文案只能有一个生产者：再出现一处字面量，就是有人又把口径写死了
        text = (REPO / "popper" / "core.py").read_text(encoding="utf-8")
        self.assertEqual(1, text.count('"执行模式："'),
                         "报告里的「执行模式」行出现了第二个写死的出处，口径会再次漂移")


if __name__ == "__main__":
    unittest.main()
