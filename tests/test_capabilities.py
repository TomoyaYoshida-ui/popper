"""可选依赖声明的一致性门禁：pyproject extras ↔ 探测表 ↔ CI 安装清单。

起因是真实事故：pyproject 声明了 4 个 extra，CI 的「装齐 extras」job 只装了 3 个
（漏了 orchestration），于是 langgraph 相关用例在整个 CI 上从没真跑过，而本地全绿——
这种漂移不会被任何单点测试发现，只能靠「三处清单必须互相吻合」的门禁。

三处清单：
1. `pyproject.toml` 的 `[project.optional-dependencies]`（权威声明）
2. `popper.capabilities.EXTRA_IMPORTS`（运行时/测试探测用的导入名）
3. `.github/workflows/ci.yml` 里 `-e ".[...]"` 的安装清单（谁真的被装起来跑过）
"""
import re
import unittest
from pathlib import Path

from popper.capabilities import EXTRA_IMPORTS, install_hint, missing_imports

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

EXTRA_BLOCK = re.compile(r"(?ms)^\[project\.optional-dependencies\]\s*\n(.*?)(?=^\[|\Z)")
EXTRA_LINE = re.compile(r"^([A-Za-z0-9_-]+)\s*=\s*\[", re.M)
INSTALL_EXTRAS = re.compile(r'pip install -e\s+"\.\[([a-z0-9,_ -]+)\]"')
INSTALL_BARE = re.compile(r"pip install -e \.(?!=)")
JOB_BLOCK = re.compile(r"(?ms)^  (?P<name>[A-Za-z0-9_-]+):\n.*?(?=^  [A-Za-z0-9_-]+:|\Z)")


def declared_extras():
    block = EXTRA_BLOCK.search(PYPROJECT.read_text(encoding="utf-8"))
    assert block, "pyproject.toml 里没有 [project.optional-dependencies] 段"
    return {name.lower() for name in EXTRA_LINE.findall(block.group(1))}


def extras_installed_by_ci(text=None):
    text = WORKFLOW.read_text(encoding="utf-8") if text is None else text
    return {item.strip().lower() for spec in INSTALL_EXTRAS.findall(text) for item in spec.split(",")}


def job_block(name):
    text = WORKFLOW.read_text(encoding="utf-8")
    for match in JOB_BLOCK.finditer(text):
        if match.group("name") == name:
            return match.group(0)
    return None


class ExtraDeclarationConsistencyTests(unittest.TestCase):
    def test_probe_table_covers_exactly_the_declared_extras(self):
        """探测表与 pyproject 必须一一对应：漏一个 extra，跳过判定就会假报「可用」。"""
        declared = declared_extras()
        self.assertEqual(declared, {name.lower() for name in EXTRA_IMPORTS},
                         "pyproject 声明的 extra 与 capabilities.EXTRA_IMPORTS 不一致；"
                         "新增/改名 extra 时必须同步 popper/capabilities.py")

    def test_ci_installs_every_declared_extra_somewhere(self):
        """每个 extra 都要有 job 真装真跑，否则那个 extra 对应的用例永远没被执行过。"""
        installed = extras_installed_by_ci()
        self.assertTrue(installed,
                        f"{WORKFLOW.relative_to(REPO).as_posix()} 里找不到带 extras 的安装步骤")
        missing = declared_extras() - installed
        self.assertEqual(set(), missing,
                         f"这些 extra 从未在任何 CI job 里被安装，对应用例等于没跑：{sorted(missing)}")

    def test_ci_keeps_one_zero_dependency_job(self):
        """"核心零运行时依赖"是对外主张，必须有一个只装 `-e .` 的 job 为它背书。"""
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertTrue(INSTALL_BARE.search(text),
                        "CI 里没有任何「不带 extras」的安装步骤，零依赖主张失去验证点")

    def test_linux_sandbox_gate_installs_every_extra(self):
        """沙箱硬门禁不得在「对应用例被 skip」的状态下发绿。

        真实沙箱 worker 用例同时要求沙箱后端与 cryptography；Linux job 少装任何一个
        extra，POPPER_REQUIRE_SANDBOX=1 保护的就不止是沙箱用例而是整个门禁的可信度。
        """
        block = job_block("linux")
        assert block, "ci.yml 里没有 linux job，Linux 沙箱硬门禁无从谈起"
        self.assertIn('POPPER_REQUIRE_SANDBOX: "1"', block,
                      "linux job 必须设 POPPER_REQUIRE_SANDBOX=1，否则沙箱不可用只会静默 skip")
        self.assertEqual(declared_extras(), extras_installed_by_ci(block),
                         "linux job 必须装齐所有声明的 extra，让需要它们的沙箱/评估用例真跑")

    def test_unknown_extra_is_an_error_not_a_silent_false(self):
        with self.assertRaises(KeyError):
            missing_imports("definitely-not-an-extra")

    def test_install_hint_names_the_right_extra(self):
        for extra in declared_extras():
            self.assertIn(f"[{extra}]", install_hint(extra))


STEP = re.compile(r"(?ms)^      - name: (?P<name>.+?)\n(?P<body>.*?)(?=^      - |\Z)")
# 装依赖的那一步是例外：它挂了什么都跑不了，再跑下去也只会有噪声。匹的是原名（带括号），
# 因为下面的展示名为了可读会把“（”后的修饰剪掉。
DEPENDENCY_STEP = re.compile(r"^安装（")
# 两个 ubuntu job 各自的「主门禁步骤」：扫描到它就停，它之前的才是环境/诊断步骤。
TEST_STEP = re.compile(r"全量测试|端到端实验")
LOCAL_ACTION = re.compile(r"^(\s*)(?:-\s+)?uses: (\./\S+)\s*$", re.M)


ACTION_STEPS = re.compile(r"(?ms)^\s*steps:\s*\n(?P<body>.*?)(?=^\S|\Z)")


def expand_local_actions(block):
    """把 job 里的本地 action 就地展开成它自己的步骤（按 ci.yml 的步骤排版对齐：名 6 / 正文 8）。

    为什么必须展开：下面的扫描只看 `run:` 步骤，把环境准备抽进 composite action 就等于把它
    挪出门禁的视野——「诊断步骤不得当门禁」会当场变成假绿（它还在跑，只是查不到东西了）。
    两个 ubuntu job 共用同一个 action，展开后两侧都被同一条门禁看着。

    匹配两种写法：内联的 `- uses: ./x` 与本仓库用的「先 `- name:` 再另起一行 `uses:`」。
    只写前者会默默匹配不到任何行，展开函数变成空操作，下面的扫描依旧看不见被抽走的步骤，
    而两条门禁用例看起来全绿。

    为什么要把缩进归一化：`STEP` 与反向验证都按 ci.yml 的排版钉死（步骤名 6 空格、正文 8
    空格），而 action.yml 自己的排版是 4/6。直接拼固定偏移，等于把门禁能不能看见这些步骤
    建在对方文件的缩进巧合上——action 内部重排一次就静默变成假绿。所以先按 body 自身的最小
    缩进平移到 0，再整块递到 6 空格，action 里怎么写都不影响扫描结果。
    """
    out = []
    for line in block.splitlines():
        match = LOCAL_ACTION.match(line)
        if not match:
            out.append(line)
            continue
        action = REPO / match.group(2) / "action.yml"
        text = action.read_text(encoding="utf-8")
        steps = ACTION_STEPS.search(text)
        assert steps, f"{action.relative_to(REPO).as_posix()} 找不到 steps: 段（展开函数无法取到它的步骤）"
        body = steps.group("body")
        # 归一化：把 body 自身的最小缩进平移到 0，再整块递到 6 空格——步骤名落在 6、
        # 正文落在 8，与 ci.yml 里手写的步骤同形，下面的扫描才能一律对待它们。
        pad = min((len(raw) - len(raw.lstrip()) for raw in body.splitlines() if raw.strip()), default=0)
        out.extend("      " + raw[pad:] if raw.strip() else raw
                   for raw in body.rstrip("\n").splitlines())
    return "\n".join(out) + "\n"


def run_steps_before_tests(block):
    """返回测试步骤之前所有带 `run:` 的 (展示名, 原名, 是否 continue-on-error)。"""
    steps = []
    for match in STEP.finditer(block):
        body = match.group("body")
        if "run:" not in body:
            continue
        raw = match.group("name").strip()
        name = raw.split("（")[0].strip()
        if TEST_STEP.search(name):
            break
        steps.append((name, raw, "continue-on-error: true" in body))
    return steps


def hard_pre_test_steps(block):
    """测试前那些「一红就把全量测试整步 skip」的硬门禁步骤（装依赖那步除外）。"""
    return [name for name, raw, tolerant in run_steps_before_tests(block)
            if not tolerant and not DEPENDENCY_STEP.match(raw)]


class PreTestStepIsolationTests(unittest.TestCase):
    """环境准备/诊断类步骤不得成为门禁。

    真实事故（run 35300378254）：新加的「安装中文字体」步骤里那句自证退出 1，GitHub
    就把后面的全量测试整步 skip——Ubuntu job 一个用例都没跑，Linux 侧五处修复的信号
    被一个诊断步骤遮掉了。门禁必须是测试本身（沙箱类用例由 POPPER_REQUIRE_SANDBOX=1
    保证该红就红），而不是一步环境脚本。
    """

    def test_environment_steps_cannot_mask_the_test_signal(self):
        block = expand_local_actions(job_block("linux"))
        assert block, "ci.yml 里没有 linux job，无从校验步骤隔离"
        self.assertEqual([], hard_pre_test_steps(block),
                         "这些环境/诊断步骤会把全量测试遮掉，必须改 continue-on-error："
                         "实际门禁在测试本身")

    def test_environment_steps_cannot_mask_the_e2e_signal(self):
        """同一条性质也要盖端到端 job：它的「环境准备」一硬，取证步骤就会被整步 skip，
        于是免登录侧只能看到「一个红步骤」而拿不到端到端结论（与单测侧是同一个缺陷形状）。"""
        block = expand_local_actions(job_block("linux-sandbox-e2e"))
        assert block, "ci.yml 里没有 linux-sandbox-e2e job，无从校验步骤隔离"
        self.assertEqual([], hard_pre_test_steps(block),
                         "e2e job 里的环境/诊断步骤会把端到端取证遮掉，必须 continue-on-error")

    def test_the_isolation_check_itself_bites(self):
        """反向验证：抽掉一个 continue-on-error，上面那条门禁必须能拦住。

        样本是**展开后**的文本：被抽的那一步现在住在 composite action 里，不展开就改不动它，
        这条反向验证也会因为「样本没改动任何东西」而红——那正是门禁失去牙的信号。
        """
        block = expand_local_actions(job_block("linux"))
        assert block, "ci.yml 里没有 linux job，无从校验步骤隔离"
        self.assertIn("- name: 安装 bubblewrap", block,
                      "展开没生效：composite action 里的步骤没被挪进 job 视野，\n"
                      "上面两条「环境步骤不得当门禁」就都是假绿（扫不到东西）")
        broken = re.sub(r"(安装 bubblewrap\n)(?:        # [^\n]*\n)*        continue-on-error: true\n",
                        r"\1", block, count=1)
        self.assertTrue(broken != block, "反向验证的样本没改动任何东西")
        self.assertIn("安装 bubblewrap", hard_pre_test_steps(broken),
                      "抽掉 continue-on-error 后仍未被识别为硬门禁，说明这份门禁不拦东西")


if __name__ == "__main__":
    unittest.main()
