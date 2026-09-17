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


if __name__ == "__main__":
    unittest.main()
