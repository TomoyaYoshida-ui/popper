"""OS 沙箱 · 文件写作用域 + HTTP 代理拦截测试（Windows 低完整性 + Linux bubblewrap 后端）。"""
import contextlib
import glob
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from popper import sandbox
from popper.sandbox import base, linux_bwrap, win_lowil


def _is_linux():
    return os.name == "posix" and sys.platform.startswith("linux")


def _backend_available_on_this_platform():
    """Windows 总是有 win_lowil；Linux 上看 bwrap 是否真的能建命名空间。"""
    if os.name == "nt":
        return True
    if _is_linux():
        return sandbox.available()  # 已是探活后的缓存
    return False


def _require_backend_in_ci():
    """CI 上（POPPER_REQUIRE_SANDBOX=1）后端不可用要**红**，不能静默 skip。

    「Linux job 是硬门禁」只有在后端真被跑过时才成立；否则 bwrap 装了但不能用
    （非特权命名空间被阻）时测试全部 skip，绿得毫无意义。
    """
    if sandbox.available():
        return
    if os.environ.get("POPPER_REQUIRE_SANDBOX"):
        raise AssertionError(
            "POPPER_REQUIRE_SANDBOX 已设置但本平台沙箱后端不可用："
            f"{linux_bwrap.probe_detail() or sandbox.selected_backend_name()}")
    raise unittest.SkipTest("本平台没有可用的沙箱后端")


@contextlib.contextmanager
def _pretend_linux():
    """打开 Linux 后端的平台闸门，让 argv 构造与环境装配这类纯逻辑在本机也能被测。

    只 patch 闸门函数，不去改全局 `os.name` / `sys.platform`：后者会让 pathlib、
    tempfile 等标准库行为失真，测到的就不是真实代码路径。真正的 bwrap 执行验证留在
    Linux 专用用例里。"""
    with patch.object(linux_bwrap, "_is_linux_platform", return_value=True):
        yield


@contextlib.contextmanager
def _clean_backend_state():
    """隔离对模块级 scope/seal 集合与探活缓存的修改。"""
    scopes, seals = set(linux_bwrap._SCOPE_PATHS), set(linux_bwrap._SEALED_PATHS)
    available, detail = linux_bwrap._AVAILABLE, linux_bwrap._PROBE_DETAIL
    try:
        linux_bwrap._SCOPE_PATHS.clear()
        linux_bwrap._SEALED_PATHS.clear()
        yield
    finally:
        linux_bwrap._SCOPE_PATHS.clear()
        linux_bwrap._SEALED_PATHS.clear()
        linux_bwrap._SCOPE_PATHS.update(scopes)
        linux_bwrap._SEALED_PATHS.update(seals)
        linux_bwrap._AVAILABLE, linux_bwrap._PROBE_DETAIL = available, detail


def _index_of(argv, *tokens):
    """返回子序列 `tokens` 在 argv 中首次出现的位置，找不到返回 -1。"""
    for i in range(len(argv) - len(tokens) + 1):
        if tuple(argv[i:i + len(tokens)]) == tokens:
            return i
    return -1


def run_sandboxed(command, *args, **kwargs):
    """测试内别名：唯一执行入口 ``sandbox.launch`` 的沙箱分支（降完整性）。"""
    return sandbox.launch(command, *args, sandboxed=True, **kwargs)


class SandboxAvailabilityTests(unittest.TestCase):
    def test_command_guard_preserves_popen_subclassing(self):
        code = sandbox.SITECUSTOMIZE + '''
import asyncio
class Derived(subprocess.Popen):
    pass
try:
    Derived(["powershell", "-Command", "exit"])
except PermissionError:
    print("guarded")
else:
    raise AssertionError("command guard bypassed")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("guarded", result.stdout.strip())

    def test_available_matches_platform(self):
        self.assertIsInstance(sandbox.available(), bool)
        # Windows 总有 win_lowil；Linux 看 bwrap 是否安装；其它平台无后端
        self.assertEqual(sandbox.available(), _backend_available_on_this_platform())

    def test_write_scope_label_rejects_unsupported_platform(self):
        # 只在「既非 Windows、又非 Linux 或 Linux 上 bwrap 不可用」时断言 OSError
        if _backend_available_on_this_platform():
            self.skipTest("本平台有可用后端，label_write_scope 不会抛 OSError")
        with self.assertRaises(OSError):
            sandbox.label_write_scope(Path(tempfile.gettempdir()))
        with self.assertRaises(OSError):
            run_sandboxed([sys.executable, "-c", "pass"], ".", {}, None, None, 1)


@unittest.skipUnless(os.name == "nt", "本用例验证 Windows 低完整性文件写作用域")
class SandboxWriteScopeTests(unittest.TestCase):
    def test_write_scope_is_enforced(self):
        root = Path(tempfile.mkdtemp(prefix="popper-sbx-"))
        try:
            scope, outside = root / "scope", root / "outside"
            scope.mkdir(), outside.mkdir()
            (scope / "locked.txt").write_text("original", encoding="utf-8")
            (scope / "target.py").write_text(
                "import sys, json, pathlib\n"
                "scope, outside, locked = sys.argv[1], sys.argv[2], sys.argv[3]\n"
                "res = {}\n"
                "try:\n"
                "    pathlib.Path(scope, 'new.txt').write_text('ok'); res['new_ok'] = True\n"
                "except OSError:\n"
                "    res['new_ok'] = False\n"
                "try:\n"
                "    pathlib.Path(locked).write_text('tampered'); res['locked_ok'] = True\n"
                "except OSError:\n"
                "    res['locked_ok'] = False\n"
                "try:\n"
                "    pathlib.Path(outside, 'out.txt').write_text('x'); res['outside_ok'] = True\n"
                "except OSError:\n"
                "    res['outside_ok'] = False\n"
                "sys.stdout.write(json.dumps(res))\n",
                encoding="utf-8")
            sandbox.label_write_scope(scope)
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            with (scope / "stdout.log").open("wb") as so, (scope / "stderr.log").open("wb") as se:
                run_sandboxed(
                    [sys.executable, str(scope / "target.py"),
                     str(scope), str(outside), str(scope / "locked.txt")],
                    cwd=str(scope), env=env, stdout=so, stderr=se, timeout_seconds=30)
            res = json.loads((scope / "stdout.log").read_text(encoding="utf-8"))
            # 作用域内可新建输出；既有 Medium 输入不可篡改；作用域外不可写。
            self.assertTrue(res["new_ok"])
            self.assertFalse(res["locked_ok"])
            self.assertFalse(res["outside_ok"])
            self.assertEqual((scope / "locked.txt").read_text(encoding="utf-8"), "original")
            self.assertFalse((outside / "out.txt").exists())
        finally:
            try:
                sandbox.unlabel_write_scope(scope)
            except Exception:
                pass
            shutil.rmtree(root, ignore_errors=True)

    def test_proxy_block_blocks_http_clients(self):
        """沙箱内环境变量代理指向死端口：本可直连的本地 HTTP 服务被拦截（受控对照）。"""
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        script = ("import sys, urllib.request\n"
                  "try:\n"
                  "    urllib.request.urlopen('http://127.0.0.1:%d/x', timeout=4)\n"
                  "    sys.stdout.write('CONNECTED')\n"
                  "except Exception:\n"
                  "    sys.stdout.write('BLOCKED')\n") % port
        try:
            # 对照组：无沙箱、无代理环境 → 可直连本地服务。
            direct = subprocess.run([sys.executable, "-c", script],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual("CONNECTED", direct.stdout.strip())
            # 沙箱组：代理指向死端口 → HTTP 客户端被拦截。
            root = Path(tempfile.mkdtemp(prefix="popper-sbx-"))
            try:
                scope = root / "scope"
                scope.mkdir()
                (scope / "target.py").write_text(script, encoding="utf-8")
                sandbox.label_write_scope(scope)
                env = {k: v for k, v in os.environ.items()
                       if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
                with (scope / "stdout.log").open("wb") as so, \
                        (scope / "stderr.log").open("wb") as se:
                    run_sandboxed([sys.executable, str(scope / "target.py")],
                                          cwd=str(scope), env=env, stdout=so, stderr=se,
                                          timeout_seconds=30)
                out = (scope / "stdout.log").read_text(encoding="utf-8")
                self.assertEqual("BLOCKED", out)
            finally:
                try:
                    sandbox.unlabel_write_scope(scope)
                except Exception:
                    pass
                shutil.rmtree(root, ignore_errors=True)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_command_interception_blocks_high_risk_commands(self):
        """沙箱内 Python 层命令拦截：高危命令被拒，白名单命令（python）放行。"""
        script = ("import subprocess, sys\n"
                  "out = []\n"
                  "try:\n"
                  "    subprocess.run(['curl', '--version'], capture_output=True)\n"
                  "    out.append('curl:ALLOWED')\n"
                  "except PermissionError:\n"
                  "    out.append('curl:BLOCKED')\n"
                  "try:\n"
                  "    subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True)\n"
                  "    out.append('python:ALLOWED')\n"
                  "except PermissionError:\n"
                  "    out.append('python:BLOCKED')\n"
                  "sys.stdout.write('|'.join(out))\n")
        root = Path(tempfile.mkdtemp(prefix="popper-sbx-"))
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "popper-cmdshim-*")))
        try:
            scope = root / "scope"
            scope.mkdir()
            (scope / "target.py").write_text(script, encoding="utf-8")
            sandbox.label_write_scope(scope)
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            with (scope / "stdout.log").open("wb") as so, (scope / "stderr.log").open("wb") as se:
                run_sandboxed([sys.executable, str(scope / "target.py")],
                                      cwd=str(scope), env=env, stdout=so, stderr=se,
                                      timeout_seconds=30)
            out = (scope / "stdout.log").read_text(encoding="utf-8")
            self.assertEqual("curl:BLOCKED|python:ALLOWED", out)
            # 拦截注入目录在运行后被清理
            leftover = set(glob.glob(os.path.join(tempfile.gettempdir(), "popper-cmdshim-*")))
            self.assertEqual(before, leftover)
        finally:
            try:
                sandbox.unlabel_write_scope(scope)
            except Exception:
                pass
            shutil.rmtree(root, ignore_errors=True)

    def test_memory_limit_is_enforced(self):
        """Job 内存限额：分配超限的沙箱子进程被拒（MemoryError → 退出码 3）。"""
        script = ("import sys\n"
                  "try:\n"
                  "    data = [b'x' * 1024 * 1024 for _ in range(60)]\n"
                  "    sys.stdout.write('ALLOC_OK')\n"
                  "except MemoryError:\n"
                  "    sys.stdout.write('MEMORY_ERROR')\n"
                  "    sys.exit(3)\n")
        root = Path(tempfile.mkdtemp(prefix="popper-sbx-"))
        try:
            scope = root / "scope"
            scope.mkdir()
            (scope / "target.py").write_text(script, encoding="utf-8")
            sandbox.label_write_scope(scope)
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            with (scope / "stdout.log").open("wb") as so, (scope / "stderr.log").open("wb") as se:
                with self.assertRaises(subprocess.CalledProcessError) as ctx:
                    run_sandboxed([sys.executable, str(scope / "target.py")],
                                          cwd=str(scope), env=env, stdout=so, stderr=se,
                                          timeout_seconds=30, mem_limit_mb=24,
                                          cpu_time_seconds=30)
            self.assertEqual(3, ctx.exception.returncode)
            self.assertEqual("MEMORY_ERROR", (scope / "stdout.log").read_text(encoding="utf-8"))
            # 对照：同样脚本无限额时可正常完成
            with (scope / "stdout2.log").open("wb") as so2, (scope / "stderr2.log").open("wb") as se2:
                run_sandboxed([sys.executable, str(scope / "target.py")],
                                      cwd=str(scope), env=env, stdout=so2, stderr=se2,
                                      timeout_seconds=60)
            self.assertEqual("ALLOC_OK", (scope / "stdout2.log").read_text(encoding="utf-8"))
        finally:
            try:
                sandbox.unlabel_write_scope(scope)
            except Exception:
                pass
            shutil.rmtree(root, ignore_errors=True)


    def test_privilege_stripping(self):
        """低权限进程：沙箱子进程降级为 Low IL 后令牌被锁，无法再以 TOKEN_ADJUST_PRIVILEGES
        打开自身令牌（受控对照：外部进程可打开）。"""
        check = (
            "import ctypes, sys\n"
            "from ctypes import wintypes\n"
            "adv = ctypes.WinDLL('advapi32', use_last_error=True)\n"
            "ker = ctypes.WinDLL('kernel32', use_last_error=True)\n"
            "adv.OpenProcessToken.argtypes=[wintypes.HANDLE, wintypes.DWORD, "
            "ctypes.POINTER(wintypes.HANDLE)]\n"
            "adv.OpenProcessToken.restype=wintypes.BOOL\n"
            "ker.GetCurrentProcess.restype=wintypes.HANDLE\n"
            "ker.CloseHandle.argtypes=[wintypes.HANDLE]; ker.CloseHandle.restype=wintypes.BOOL\n"
            "h=wintypes.HANDLE()\n"
            "ok=adv.OpenProcessToken(ker.GetCurrentProcess(), 0x0008 | 0x0020, ctypes.byref(h))\n"
            "if ok:\n"
            "    ker.CloseHandle(h)\n"
            "sys.stdout.write('CAN_ADJUST' if ok else 'LOCKED')\n")
        # 对照组：未沙箱进程可打开自身令牌调整特权。
        direct = subprocess.run([sys.executable, "-c", check],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual("CAN_ADJUST", direct.stdout.strip())
        # 沙箱组：Low IL 令牌被锁。
        root = Path(tempfile.mkdtemp(prefix="popper-sbx-"))
        try:
            scope = root / "scope"
            scope.mkdir()
            (scope / "target.py").write_text(check, encoding="utf-8")
            sandbox.label_write_scope(scope)
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            with (scope / "stdout.log").open("wb") as so, (scope / "stderr.log").open("wb") as se:
                run_sandboxed([sys.executable, str(scope / "target.py")],
                                      cwd=str(scope), env=env, stdout=so, stderr=se,
                                      timeout_seconds=30)
            self.assertEqual("LOCKED", (scope / "stdout.log").read_text(encoding="utf-8"))
        finally:
            try:
                sandbox.unlabel_write_scope(scope)
            except Exception:
                pass
            shutil.rmtree(root, ignore_errors=True)

    def test_python_copy_launches(self):
        """专用 python 副本可独立运行，且在 venv 里仍能看到真解释器与 venv 三方库路径。

        不只断言退出码：venv 下的副本曾经是个跑不起来的壳（退出码 106），而“能跑”也不
        等于“能用的还是那个解释器环境”——所以额外钉住三件事：跑的就是副本本身、
        标准库能 import、PYTHONPATH 里挂的 venv site-packages 真的进了 sys.path。
        """
        copy_dir, home = sandbox._make_python_copy()
        probe = ("import json, sys\n"
                 "mods = []\n"
                 "for name in ('json', 'sqlite3', 'hashlib'):\n"
                 "    __import__(name)\n"
                 "    mods.append(name)\n"
                 "print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix,\n"
                 "                   'path': sys.path, 'stdlib': mods}))\n")
        try:
            r = subprocess.run([str(copy_dir / "python.exe"), "-c", probe],
                               capture_output=True, text=True,
                               env={**os.environ, **home}, timeout=60)
            self.assertEqual(0, r.returncode, f"副本启动失败：{r.stderr}")
            info = json.loads(r.stdout)
            self.assertEqual(copy_dir.resolve(), Path(info["executable"]).parent.resolve())
            self.assertEqual(["json", "sqlite3", "hashlib"], info["stdlib"])
            self.assertEqual(Path(home["PYTHONHOME"]).resolve(), Path(info["prefix"]).resolve())
            if home.get("PYTHONPATH"):
                self.assertIn(str(Path(home["PYTHONPATH"]).resolve()),
                              [str(Path(entry).resolve()) for entry in info["path"]
                               if entry])
        finally:
            shutil.rmtree(copy_dir, ignore_errors=True)

    def test_netblock_rule_name_format(self):
        self.assertIsInstance(sandbox.netblock_available(), bool)
        name = sandbox._netblock_rule_name()
        self.assertTrue(name.startswith("popper-sandbox-block-"))


class SandboxBackendContractTests(unittest.TestCase):
    """隔离后端只在协议层抽象：注册表项满足 ``base.SandboxBackend``，非后端路径不受影响。"""

    def test_registry_entries_satisfy_the_protocol(self):
        registered = sandbox.backends()
        # 两个后端都应在注册表中
        self.assertIn(win_lowil.BACKEND, registered)
        self.assertIn(linux_bwrap.BACKEND, registered)
        self.assertEqual("win_lowil", win_lowil.BACKEND.name)
        self.assertEqual("linux_bwrap", linux_bwrap.BACKEND.name)
        for backend in registered:
            self.assertIsInstance(backend, base.SandboxBackend)
            self.assertTrue(backend.capability_notes())

    def test_execution_backend_names_are_stable_and_distinct(self):
        """WorkerReceipt 的后端名必须区分两个内核机制，不能把 bwrap 签成 Low IL。"""
        with patch.object(sandbox, "selected_backend", return_value=win_lowil.BACKEND):
            self.assertEqual("windows_low_integrity", sandbox.execution_backend_name())
        with patch.object(sandbox, "selected_backend", return_value=linux_bwrap.BACKEND):
            self.assertEqual("linux_bubblewrap", sandbox.execution_backend_name())
        with patch.object(sandbox, "selected_backend", return_value=None):
            self.assertIsNone(sandbox.execution_backend_name())

    def test_no_backend_message_names_both_registered_backends(self):
        message = sandbox.no_backend_message()
        self.assertIn("win_lowil", message)
        self.assertIn("linux_bwrap", message)
        self.assertIn("--trusted-local", message)
        # 旧的「只有 Windows」描述不能回来：那句话在 Linux 上会把人引向错误结论。
        self.assertNotIn("只有 Windows", message)

    def test_selected_backend_matches_availability(self):
        selected = sandbox.selected_backend()
        self.assertEqual(sandbox.available(), selected is not None)
        if os.name == "nt":
            self.assertEqual("win_lowil", selected.name)
        elif _is_linux() and sandbox.available():
            self.assertEqual("linux_bwrap", selected.name)
        else:
            self.assertIsNone(selected)

    def test_sandboxed_launch_without_a_backend_points_at_trusted_local(self):
        with patch.object(sandbox, "selected_backend", return_value=None):
            with self.assertRaises(OSError) as ctx:
                sandbox.launch([sys.executable, "-c", "pass"], ".", {}, None, None, 1,
                               sandboxed=True)
        self.assertIn("--trusted-local", str(ctx.exception))

    def test_trusted_local_launch_does_not_need_a_backend(self):
        """trusted-local 的进程组执行不是后端，没有可用后端时仍必须能跑。"""
        with patch.object(sandbox, "selected_backend", return_value=None):
            with tempfile.TemporaryDirectory(prefix="popper-tl-") as tmp:
                out, err = Path(tmp) / "o.log", Path(tmp) / "e.log"
                with out.open("wb") as so, err.open("wb") as se:
                    sandbox.launch(
                        [sys.executable, "-c", "import sys; sys.stdout.write('LOCAL')"],
                        tmp, dict(os.environ), so, se, 60)
                self.assertEqual("LOCAL", out.read_text(encoding="utf-8"))


class LinuxBubblewrapArgvConstructionTests(unittest.TestCase):
    """bwrap argv 的**挂载叠放顺序**就是语义：用「假装 Linux」在 Windows 上锁定。

    这些断言防的是静默失效那类 bug：`--tmpfs /tmp` 会盖住命令拦截 shim 目录、禁读
    文件的挂载点在只读根上凭空造不出来、scope 叠在父目录只读之前会把工作区留在只读
    视图上。顺序一变，沙箱就变成「看起来在用、其实没约束」。
    """

    SHIM = "/tmp/popper-cmdshim-4242-abcd1234"
    SCOPE = "/work/run-1"
    SECRET = "/work/run-1/holdout/test.json"

    def _build(self, sealed=(SECRET,), scopes=(SCOPE,), ro=(SHIM,)):
        return linux_bwrap._build_bwrap_argv(set(scopes), set(sealed), set(ro))

    def test_namespace_isolation_and_parent_attachment_come_first(self):
        argv = self._build()
        self.assertEqual("bwrap", argv[0])
        self.assertEqual(1, _index_of(argv, "--unshare-all"))
        self.assertEqual(2, _index_of(argv, "--die-with-parent"))
        self.assertEqual("--", argv[-1])
        # 所有挂载声明都必须在 `--` 之前，否则会被当成候选命令的参数
        self.assertLess(_index_of(argv, "--ro-bind", "/", "/"), len(argv) - 1)

    def test_shim_dir_is_remounted_after_the_tmpfs_that_would_hide_it(self):
        argv = self._build()
        tmpfs = _index_of(argv, "--tmpfs", "/tmp")
        shim = _index_of(argv, "--ro-bind", self.SHIM, self.SHIM)
        self.assertGreater(tmpfs, 0)
        self.assertGreater(shim, tmpfs,
                           "shim 目录落在 /tmp 下，早于 --tmpfs /tmp 挂载就会被整体遮掉，"
                           "命令拦截会静默失效")

    def test_sealed_parent_is_mounted_read_only_before_the_mask(self):
        argv = self._build()
        parent = os.path.dirname(self.SECRET)
        parent_bind = _index_of(argv, "--ro-bind", parent, parent)
        mask = _index_of(argv, "--ro-bind", "/dev/null", self.SECRET)
        self.assertGreater(parent_bind, _index_of(argv, "--ro-bind", "/", "/"),
                           "父目录只读挂载要在宿主根之后，才谈得上覆盖")
        self.assertGreater(mask, parent_bind,
                           "/dev/null 遮蔽必须叠在父目录视图之上，否则读到的还是真实数据")

    def test_scope_bind_sits_after_the_parent_read_only_mount_and_masks_are_last(self):
        argv = self._build()
        parent = os.path.dirname(self.SECRET)
        scope_bind = _index_of(argv, "--bind", self.SCOPE, self.SCOPE)
        self.assertGreater(scope_bind, _index_of(argv, "--ro-bind", parent, parent),
                           "禁读文件在工作区内时，工作区必须仍被挂回可写，不能留在只读视图")
        last_scope_token = max(i for i in range(len(argv))
                               if argv[i] == "--bind")
        first_mask = min(i for i in range(len(argv))
                         if argv[i:i + 2] == ["--ro-bind", "/dev/null"])
        self.assertGreater(first_mask, last_scope_token,
                           "遮蔽路径最深，必须最后叠放才能盖住读写视图")

    def test_a_seal_at_the_root_does_not_remount_the_whole_root_twice(self):
        """禁读文件直接挂在根下时，父目录就是已只读挂载的 /，不能重复挂。"""
        argv = self._build(sealed=("/etc-hosts",), scopes=(), ro=())
        self.assertEqual(2, argv.count("/"), "--ro-bind / / 只应出现一次")
        self.assertGreater(_index_of(argv, "--ro-bind", "/dev/null", "/etc-hosts"), 0)

    def test_scope_and_seal_registration_absolutizes_relative_paths(self):
        with _pretend_linux(), _clean_backend_state():
            linux_bwrap.label_write_scope("relative-scope")
            self.assertEqual(1, len(linux_bwrap._SCOPE_PATHS))
            recorded = next(iter(linux_bwrap._SCOPE_PATHS))
            self.assertTrue(os.path.isabs(recorded), recorded)
            self.assertEqual(os.path.abspath("relative-scope"), recorded)

    def test_sealing_a_missing_target_fails_instead_of_pretending(self):
        """登记不存在的路径 = 「以为封了读、其实什么都没挡」，必须显式失败。"""
        with _pretend_linux(), _clean_backend_state():
            missing = os.path.join(tempfile.gettempdir(),
                                   "popper-seal-missing-{}.json".format(os.getpid()))
            self.assertFalse(os.path.exists(missing))
            with self.assertRaises(OSError):
                linux_bwrap.seal_read(missing)
            self.assertEqual(set(), linux_bwrap._SEALED_PATHS)

    def test_sealing_an_existing_target_is_recorded(self):
        with _pretend_linux(), _clean_backend_state():
            with tempfile.TemporaryDirectory(prefix="popper-seal-") as tmp:
                secret = os.path.join(tmp, "test.json")
                with open(secret, "w", encoding="utf-8") as handle:
                    handle.write("[]")
                linux_bwrap.seal_read(secret)
                self.assertEqual({os.path.abspath(secret)}, linux_bwrap._SEALED_PATHS)
                linux_bwrap.unseal_read(secret)
                self.assertEqual(set(), linux_bwrap._SEALED_PATHS)

    def test_nproc_ceiling_is_the_current_uid_process_count_plus_the_declared_budget(self):
        """RLIMIT_NPROC 按真实 UID 计数：直接写声明值会让候选一个子进程都起不来。"""
        with patch.object(linux_bwrap, "_uid_process_count", return_value=137):
            self.assertEqual(141, linux_bwrap._nproc_ceiling(4))
        with patch.object(linux_bwrap, "_uid_process_count", return_value=None):
            self.assertEqual(4 + linux_bwrap._NPROC_UNKNOWN_HEADROOM,
                             linux_bwrap._nproc_ceiling(4))


class LinuxBubblewrapProbeTests(unittest.TestCase):
    """可用性探活必须**跑一次真沙箱**：`bwrap --version` 在 AppArmor 拒绝非特权用户
    命名空间时仍返回 0，会把硬门禁建立在假前提上。"""

    def _probe_with(self, run_result=None, run_error=None):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if run_error is not None:
                raise run_error
            return run_result

        with _pretend_linux(), _clean_backend_state():
            with patch.object(linux_bwrap.subprocess, "run", side_effect=fake_run):
                first = linux_bwrap.available()
                detail = linux_bwrap.probe_detail()
            # 结果缓存：第二次探活不应再起进程
            with patch.object(linux_bwrap.subprocess, "run",
                              side_effect=AssertionError("探活结果未缓存")):
                self.assertEqual(first, linux_bwrap.available())
        return first, detail, calls

    def test_probe_uses_the_real_sandbox_shape_not_a_version_check(self):
        available, _, calls = self._probe_with(
            subprocess.CompletedProcess([], 0, b"", b""))
        self.assertTrue(available)
        self.assertEqual(1, len(calls))
        argv = calls[0]
        self.assertEqual("bwrap", argv[0])
        self.assertIn("--unshare-all", argv)
        self.assertNotIn("--version", argv)
        self.assertIn("--", argv)

    def test_missing_bwrap_is_reported_as_not_installed(self):
        available, detail, _ = self._probe_with(run_error=FileNotFoundError())
        self.assertFalse(available)
        self.assertIn("未安装", detail)

    def test_denied_user_namespace_is_reported_with_the_apparmor_hint(self):
        failure = subprocess.CompletedProcess(
            [], 1, b"", b"bwrap: No permissions to create new namespace")
        available, detail, _ = self._probe_with(run_result=failure)
        self.assertFalse(available)
        self.assertIn("namespace", detail)
        with _pretend_linux(), _clean_backend_state():
            with patch.object(linux_bwrap.subprocess, "run", return_value=failure):
                with self.assertRaises(OSError) as ctx:
                    linux_bwrap._require_bwrap()
        message = str(ctx.exception)
        self.assertIn("apparmor", message.lower())
        self.assertIn("--trusted-local", message)

    def test_non_linux_platforms_never_probe(self):
        with _clean_backend_state():
            with patch.object(linux_bwrap.subprocess, "run",
                              side_effect=AssertionError("非 Linux 不应探活")):
                self.assertFalse(linux_bwrap.available())


class _FakePopen:
    """满足 ``base.reap`` 所需的最小 Popen 形状。"""

    pid = 4242
    returncode = 0

    def __init__(self):
        self.kwargs = None

    def communicate(self, timeout=None):
        return b"", b""

    def wait(self, timeout=None):
        return self.returncode


class LinuxBubblewrapLaunchTests(unittest.TestCase):
    """launch 侧的接线：探活通过后才启动，限额经 env 透传给沙箱内 bootstrap。"""

    def _launch(self, **kwargs):
        popen_calls = {}

        def fake_popen(argv, **call_kwargs):
            popen_calls["argv"] = argv
            popen_calls["kwargs"] = call_kwargs
            return _FakePopen()

        with _pretend_linux(), _clean_backend_state():
            with tempfile.TemporaryDirectory(prefix="popper-sbx-") as tmp:
                scope = os.path.join(tmp, "run-1")
                os.makedirs(scope)
                secret = os.path.join(tmp, "test.json")
                with open(secret, "w", encoding="utf-8") as handle:
                    handle.write("[]")
                linux_bwrap.label_write_scope(scope)
                linux_bwrap.seal_read(secret)
                env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "/keep/me"}
                probe = subprocess.CompletedProcess([], 0, b"", b"")
                with patch.object(linux_bwrap.subprocess, "run", return_value=probe), \
                        patch.object(linux_bwrap.subprocess, "Popen", side_effect=fake_popen), \
                        patch.object(linux_bwrap, "_uid_process_count", return_value=100):
                    linux_bwrap.launch_bwrap([sys.executable, "candidate.py"], scope, env,
                                             subprocess.DEVNULL, subprocess.DEVNULL, 30,
                                             mem_limit_mb=64, cpu_time_seconds=20,
                                             max_processes=4)
                popen_calls["scope"] = scope
                popen_calls["secret"] = secret
        return popen_calls

    def test_launch_mounts_shim_scope_and_mask_then_runs_the_bootstrap(self):
        calls = self._launch()
        argv = calls["argv"]
        sep = argv.index("--")
        self.assertGreater(sep, 1)
        self.assertEqual([sys.executable, "-c", linux_bwrap.BOOTSTRAP, "candidate.py"],
                         argv[sep + 1:])
        # 命令拦截 shim 必须可见：挂在 --tmpfs /tmp 之后
        shim = calls["kwargs"]["env"]["POPPER_SANDBOX_SHIMDIR"]
        self.assertGreater(_index_of(argv, "--ro-bind", shim, shim),
                           _index_of(argv, "--tmpfs", "/tmp"))
        # 运行工作区可写、禁读文件被遮蔽
        self.assertGreater(_index_of(argv, "--bind", calls["scope"], calls["scope"]), 0)
        self.assertGreater(_index_of(argv, "--ro-bind", "/dev/null", calls["secret"]), 0)

    def test_launch_passes_limits_through_the_environment_and_blocks_network(self):
        calls = self._launch()
        env = calls["kwargs"]["env"]
        self.assertEqual("64", env["POPPER_MEM_MB"])
        self.assertEqual("20", env["POPPER_CPU_SECONDS"])
        self.assertEqual("104", env["POPPER_NPROC_CEILING"])
        self.assertEqual("http://127.0.0.1:9", env["HTTP_PROXY"])
        self.assertIn("/keep/me", env["PYTHONPATH"])
        self.assertTrue(calls["kwargs"]["start_new_session"],
                        "整树终止依赖新会话，否则 killpg 会打到宿主进程组")

    def test_launch_fails_loudly_when_the_probe_denies_the_backend(self):
        failure = subprocess.CompletedProcess([], 1, b"", b"bwrap: user namespaces disabled")
        with _pretend_linux(), _clean_backend_state():
            with patch.object(linux_bwrap.subprocess, "run", return_value=failure), \
                    patch.object(linux_bwrap.subprocess, "Popen",
                                 side_effect=AssertionError("探活失败就不该启动")):
                with self.assertRaises(OSError) as ctx:
                    linux_bwrap.launch_bwrap([sys.executable, "c.py"], ".", {}, None, None, 5)
        self.assertIn("bubblewrap", str(ctx.exception))


class ReadSealAvailabilityTests(unittest.TestCase):
    def test_seal_read_available_matches_platform(self):
        self.assertIsInstance(sandbox.seal_read_available(), bool)
        # Windows 总有 MIC；Linux 看 bwrap；其它平台无后端
        self.assertEqual(sandbox.seal_read_available(), _backend_available_on_this_platform())

    def test_seal_read_rejects_unsupported_platform(self):
        if _backend_available_on_this_platform():
            self.skipTest("本平台有可用后端，seal_read 不会抛 OSError")
        with self.assertRaises(OSError):
            sandbox.seal_read(Path(tempfile.gettempdir()))
        with self.assertRaises(OSError):
            sandbox.unseal_read(Path(tempfile.gettempdir()))


@unittest.skipUnless(os.name == "nt", "本用例验证 Windows 强制完整性标签禁读")
class ReadSealEnforcementTests(unittest.TestCase):
    """保留集读隔离：内核强制，只拦更低完整性主体，不影响同级/更高主体。"""

    SCRIPT = ("import json, pathlib, sys\n"
              "res = {}\n"
              "try:\n"
              "    res['read'] = pathlib.Path(sys.argv[1]).read_text().strip()\n"
              "except OSError as error:\n"
              "    res['error'] = type(error).__name__\n"
              "sys.stdout.write(json.dumps(res))\n")

    def _sandboxed_read(self, scope, env, secret):
        """让 Low IL 沙箱进程尝试读取 secret，返回它看到的结果字典。"""
        out, err = scope / "o.log", scope / "e.log"
        with out.open("wb") as so, err.open("wb") as se:
            run_sandboxed([sys.executable, str(scope / "target.py"), str(secret)],
                          cwd=str(scope), env=env, stdout=so, stderr=se, timeout_seconds=30)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_sealed_labels_are_unreadable_for_the_sandboxed_candidate(self):
        root = Path(tempfile.mkdtemp(prefix="popper-seal-"))
        try:
            scope, holdout = root / "scope", root / "holdout"
            scope.mkdir(), holdout.mkdir()
            secret = holdout / "test.json"
            secret.write_text('[{"id": "a", "x": 1, "y": 2}]', encoding="utf-8")
            (scope / "target.py").write_text(self.SCRIPT, encoding="utf-8")
            sandbox.label_write_scope(scope)
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            # 受控对照：未封读时沙箱进程可以读到保留集。
            self.assertIn("read", self._sandboxed_read(scope, env, secret))
            with sandbox.sealed_reads([secret]):
                self.assertEqual("PermissionError", self._sandboxed_read(scope, env, secret)["error"])
                # 用户在窗口内仍能读（封读只针对更低完整性主体）。
                self.assertIn("x", secret.read_text(encoding="utf-8"))
            # 窗口结束后标签恢复，沙箱进程重新可读。
            self.assertIn("read", self._sandboxed_read(scope, env, secret))
        finally:
            try:
                sandbox.unlabel_write_scope(scope)
            except Exception:
                pass
            shutil.rmtree(root, ignore_errors=True)

    def test_seal_is_restored_even_when_the_window_raises(self):
        root = Path(tempfile.mkdtemp(prefix="popper-seal-"))
        try:
            secret = root / "test.json"
            secret.write_text("[]", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                with sandbox.sealed_reads([secret]):
                    raise RuntimeError("执行失败")
            # 恢复后可重新封读，说明标签没有被留在禁读状态。
            with sandbox.sealed_reads([secret]):
                self.assertTrue(secret.is_file())
        finally:
            shutil.rmtree(root, ignore_errors=True)


@unittest.skipUnless(_is_linux(), "Linux bubblewrap mount namespace 屏蔽仅支持 Linux")
class LinuxBubblewrapReadSealEnforcementTests(unittest.TestCase):
    """保留集读隔离（Linux 路径）：bwrap `--ro-bind /dev/null <path>` 屏蔽。

    与 Windows 的 PermissionError 不同，Linux 候选读到的是空字符串（/dev/null EOF），
    两者都是「内核强制、候选无法读出真实数据」。
    """

    SCRIPT = ("import json, pathlib, sys\n"
              "res = {}\n"
              "try:\n"
              "    res['read'] = pathlib.Path(sys.argv[1]).read_text().strip()\n"
              "except OSError as error:\n"
              "    res['error'] = type(error).__name__\n"
              "sys.stdout.write(json.dumps(res))\n")

    def setUp(self):
        # CI 的 linux job 设了 POPPER_REQUIRE_SANDBOX=1：bwrap 装了却不能用时要红。
        _require_backend_in_ci()

    def _sandboxed_read(self, scope, env, secret):
        out, err = scope / "o.log", scope / "e.log"
        with out.open("wb") as so, err.open("wb") as se:
            run_sandboxed([sys.executable, str(scope / "target.py"), str(secret)],
                          cwd=str(scope), env=env, stdout=so, stderr=se, timeout_seconds=30)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_sealed_paths_are_masked_for_the_sandboxed_candidate(self):
        root = Path(tempfile.mkdtemp(prefix="popper-seal-"))
        try:
            scope, holdout = root / "scope", root / "holdout"
            scope.mkdir(), holdout.mkdir()
            secret = holdout / "test.json"
            secret.write_text('[{"id": "a", "x": 1, "y": 2}]', encoding="utf-8")
            (scope / "target.py").write_text(self.SCRIPT, encoding="utf-8")
            sandbox.label_write_scope(scope)
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
            # 受控对照：未封读时沙箱进程可以读到保留集真实内容
            self.assertIn("a", self._sandboxed_read(scope, env, secret)["read"])
            with sandbox.sealed_reads([secret]):
                # Linux 路径：/dev/null 屏蔽 → 候选读到空字符串，不是真实数据
                self.assertEqual("", self._sandboxed_read(scope, env, secret).get("read", ""))
                # 用户在窗口内仍能读（封读只针对沙箱候选进程，不影响宿主）
                self.assertIn("x", secret.read_text(encoding="utf-8"))
            # 窗口结束后沙箱进程重新可读真实内容
            self.assertIn("a", self._sandboxed_read(scope, env, secret)["read"])
        finally:
            try:
                sandbox.unlabel_write_scope(scope)
            except Exception:
                pass
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
