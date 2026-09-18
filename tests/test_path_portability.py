"""跨平台路径守卫门禁：同一份提案字符串在 Windows 与 Linux 上必须得到同一结论。

起因是 Actions 第五跑 ubuntu 上的唯一红项：``tests/test_harness.py`` 断言 ``C:/abs.py``
被拒，在 Windows 成立、在 Linux 不成立。根因不是测试写错，而是守卫写成
``Path(x).is_absolute()``——它按**宿主**规则解释路径，于是同一份模型提案在两侧结论
相反，而 research artifact 最怕的就是「换台机器跑出不同结论」。

更糟的是落盘：Windows 上 ``/tmp/a.py`` 不算绝对路径，因此旧守卫放行，而
``root / "/tmp/a.py"`` 会把 base 的目录部分丢掉（盘根锚定）拼成 ``C:\\tmp\\a.py``，
即模型内容可驱动的写越界。这里把「判据必须平台中立」固定成门禁。
"""
from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from popper.core import ProtocolError, digest, inside, is_safe_relative, portable_path
from popper.reproduction import _relative as reproduction_relative
from popper.research.confirmation_bundle import _relative as bundle_relative
from popper.research.confirmation_contracts import safe_code_path
from popper.research.revisions import CodeEdit, RevisionStore
from popper.research.workers.base import InputArtifact, JobSpec
from popper.research.workers.local import LocalWorker

# 每条都带 .py 后缀：这样各站点报错只能来自「路径形状」守卫，而不是 .py 后缀规则。
# 其中 `C:/abs.py` 是旧守卫在 Linux 上放行的、`/tmp/abs.py` 是在 Windows 上放行的。
HOSTILE = ("/tmp/abs.py", "C:/abs.py", "C:abs-rel.py", "C:\\win.py",
           "\\\\server\\share\\x.py", "//server/share/x.py", "../up.py",
           "sub/../up.py", "./now.py", "a//b.py")
FRIENDLY = ("helper.py", "sub/pkg/model.py")
# 旧行为：Windows 归一、Linux 当成普通字符——同一份提案在两侧得到不同产物。
# 新行为：两侧统一按分隔符归一，所以这一组断言不挂任何平台守卫。
NORMALIZES = (("nested\\helper.py", "nested/helper.py"),
              ("sub\\pkg\\model.py", "sub/pkg/model.py"),
              ("helper.py", "helper.py"))


class HostDependenceTests(unittest.TestCase):
    """记录「为什么宿主相关的判据不可用」——两侧读法相互矛盾的两行。"""

    def test_the_two_rows_that_diverge_between_hosts(self):
        self.assertTrue(PureWindowsPath("C:/abs.py").is_absolute())
        self.assertFalse(PurePosixPath("C:/abs.py").is_absolute())
        self.assertFalse(PureWindowsPath("/tmp/abs.py").is_absolute())
        self.assertTrue(PurePosixPath("/tmp/abs.py").is_absolute())
        # 无论哪台宿主怎么说，两者都必须被拒。
        for raw in ("C:/abs.py", "/tmp/abs.py"):
            with self.subTest(path=raw):
                self.assertFalse(is_safe_relative(raw))

    def test_the_join_escaping_is_why_the_rejects_matter(self):
        """被旧守卫放行的形状，拼到 root 上确实会掉到 root 之外。"""
        win_root = PureWindowsPath("C:/rev/root")
        posix_root = PurePosixPath("/rev/root")
        self.assertFalse((win_root / "/tmp/abs.py").is_relative_to(win_root))
        self.assertFalse((win_root / "C:/abs.py").is_relative_to(win_root))
        # 镜像方向：Linux 上 `C:/abs.py` 会落在 root 内的嵌套目录，看起来「安全」，
        # 但同一份提案在 Windows 上就是越界——所以必须按「任一台宿主的读法」判。
        self.assertTrue((posix_root / "C:/abs.py").is_relative_to(posix_root))


class GuardSiteAgreementTests(unittest.TestCase):
    """所有站点必须共用同一判据：不允许再有第 8 处 `Path(...).is_absolute()`。"""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp()).resolve()
        for name in FRIENDLY:
            (cls.root / name).parent.mkdir(parents=True, exist_ok=True)
            (cls.root / name).write_text("x = 1\n", encoding="utf-8")
        cls.store = RevisionStore(cls.root / "revisions")
        cls.worker = LocalWorker(cls.root / "jobs", cls.store)

    def sites(self):
        def materialize(path):
            content = b"x = 1\n"
            identity = {"hypothesis_id": "H", "design_id": "D", "edits": [], "path": path}
            revision_id = "REV-" + digest(identity)[:24]
            manifest = {"schema_version": "1.0", "revision_id": revision_id,
                        "identity": identity, "status": "immutable",
                        "files": {path: hashlib.sha256(content).hexdigest()}}
            self.store.materialize(revision_id, manifest, {path: content})

        return {
            "core.inside": (lambda p: inside(self.root, p), "项目路径必须是相对路径"),
            "reproduction._relative": (
                lambda p: reproduction_relative(self.root, p), "复现任务路径必须是相对路径"),
            "CodeEdit": (lambda p: CodeEdit(p, "x = 1\n"), "相对 Python 路径"),
            "RevisionStore.materialize": (materialize, "materialize 代码路径不合法"),
            "InputArtifact.target": (
                lambda p: InputArtifact(str(self.root / "src"), p, "0" * 64, "code"),
                "安全相对路径"),
            "JobSpec.entrypoint": (
                lambda p: JobSpec("k", "REV-" + "0" * 24, "0" * 64, "DES", p),
                "身份或 entrypoint 不合法"),
            "JobSpec.outputs": (
                lambda p: replace(self.valid_spec(), outputs=(p,)), "output 必须是安全相对路径"),
            "confirmation_bundle._relative": (
                lambda p: bundle_relative(p), "确认包文件路径必须是规范安全相对路径"),
            "confirmation_contracts.safe_code_path": (
                lambda p: safe_code_path(p),
                r"Code paths must be portable relative POSIX paths|Unsafe code path"),
            "LocalWorker.read_workspace_file": (
                lambda p: self.worker.read_workspace_file("JOB-1", p), "workspace 相对路径不合法"),
        }

    @staticmethod
    def valid_spec():
        return JobSpec("k", "REV-" + "0" * 24, "0" * 64, "DES", "run.py")

    def test_every_guard_site_rejects_the_same_hostile_shapes(self):
        for label, (call, message) in self.sites().items():
            for raw in HOSTILE:
                with self.subTest(site=label, path=raw):
                    with self.assertRaisesRegex(ProtocolError, message):
                        call(raw)

    def test_every_guard_site_still_accepts_plain_relative_paths(self):
        """反向守门：不得把合法的相对路径误拒（否则修复就变成了新缺陷）。"""
        for label, (call, message) in self.sites().items():
            for raw in FRIENDLY:
                with self.subTest(site=label, path=raw):
                    try:
                        call(raw)
                    except ProtocolError as error:
                        # 只允许「文件不存在」这类与路径形状无关的后续拒绝
                        self.assertNotRegex(
                            str(error), message,
                            f"{label} 把合法相对路径 {raw!r} 判成了越界")

    def test_helper_rejects_non_strings_without_raising(self):
        for value in (None, 1, Path("a.py"), ["a.py"]):
            with self.subTest(value=value):
                self.assertFalse(is_safe_relative(value))

    def test_separator_forms_normalize_the_same_on_both_hosts(self):
        """归一不留平台守卫：两侧都必须把 `nested\\helper.py` 看成同一个路径。"""
        for raw, expected in NORMALIZES:
            with self.subTest(path=raw):
                self.assertEqual(expected, portable_path(raw))
                self.assertEqual(expected, CodeEdit(raw, "x = 1\n").path)


class NoHostDependentJudgmentTests(unittest.TestCase):
    """源码门禁：产品模块不得再用宿主相关的 `is_absolute()` 判「相对路径」。"""

    ALLOWED = {
        # 判据的唯一实现处。
        "popper/core.py",
        # 反方向用法（把相对路径补成绝对路径），且后续有 is_relative_to 兜底。
        "popper/vendors.py",
    }

    def test_only_the_shared_judgment_uses_is_absolute(self):
        package = Path(__file__).resolve().parents[1] / "popper"
        offenders = {path.relative_to(package.parent).as_posix()
                     for path in sorted(package.rglob("*.py"))
                     if "__pycache__" not in path.parts
                     and re.search(r"\.is_absolute\(\)", path.read_text(encoding="utf-8"))}
        self.assertEqual(set(), offenders - self.ALLOWED,
                         f"这些站点用回了宿主相关的 is_absolute()：{sorted(offenders - self.ALLOWED)}")


if __name__ == "__main__":
    unittest.main()
