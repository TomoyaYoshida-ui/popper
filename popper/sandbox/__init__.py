"""受限执行的唯一门面：能力探针 + 唯一入口 ``launch`` + 写作用域 / 读封禁操作。

- 隔离后端由 ``base.SandboxBackend`` 契约描述，注册表在下面。已注册实现：
  ``win_lowil``（Windows 低完整性）与 ``linux_bwrap``（Linux bubblewrap）；macOS
  seatbelt **尚未实现**，因此没有注册——``available()`` 只在存在可用后端时为真，
  无后端平台上 ``--sandbox`` 会显式报错并指向 ``--trusted-local`` 人工路径，而不是静默降级。
- trusted-local 的进程组执行不是后端（没有隔离主张），实现在
  ``base.launch_process_group``，任何平台都可用。
- 能力问题一律实算并缓存（后端探活），调用方（``popper experiment isolation``、``--sandbox`` 门禁）
  据此如实声明，不承诺不成立的前提。
"""
from __future__ import annotations

import contextlib
import os
import sys

from . import base, linux_bwrap, win_lowil
from .base import NO_WINDOW, force_kill_tree, terminate_tree
from .win_lowil import BOOTSTRAP, SITECUSTOMIZE, _make_python_copy, _netblock_rule_name

# 新增后端 = 实现 base.SandboxBackend + 在这里加一项（顺序即选择优先级）。
_BACKENDS = (win_lowil.BACKEND, linux_bwrap.BACKEND)

# 写进 WorkerReceipt / 外部确认契约的执行后端名（历史回执里的字符串不变，所以它是
# 稳定标识而不是类名的反射）。
_RECEIPT_BACKEND_NAMES = {
    "win_lowil": "windows_low_integrity",
    "linux_bwrap": "linux_bubblewrap",
}


def backends():
    """已注册的隔离后端。"""
    return _BACKENDS


def selected_backend():
    """当前平台可用的隔离后端；没有可用后端时返回 None。"""
    for backend in _BACKENDS:
        if backend.available():
            return backend
    return None


def no_backend_message():
    """无可用后端时的统一说明（含可执行指引）。

    `--sandbox` 的多条入口（``_require_backend``、research controller、``Experiment.evaluate``）
    共用这一段，避免各写一份而只有一处跟得上注册表的变化（曾经写过「只有 Windows」）。
    """
    message = (
        "本平台没有可用的沙箱后端：--sandbox 当前已注册 Windows 低完整性"
        "（win_lowil）与 Linux bubblewrap（linux_bwrap）；后者除安装 bwrap"
        "（apt install bubblewrap 或等价命令）外，还需内核允许非特权用户命名空间。"
        "macOS seatbelt 尚未实现。"
        "请改用 --trusted-local 人工路径，由你自己确认待执行代码可信，"
        "或在支持的平台上安装对应后端")
    if os.name != "nt" and sys.platform.startswith("linux"):
        detail = linux_bwrap.probe_detail()
        if detail:
            message += f"（bwrap 探活失败：{detail}；{linux_bwrap.APPARMOR_HINT}）"
    return message


def _require_backend():
    backend = selected_backend()
    if backend is None:
        raise OSError(no_backend_message())
    return backend


def selected_backend_name():
    """当前选中的后端名（无后端为 None）。"""
    backend = selected_backend()
    return backend.name if backend is not None else None


def execution_backend_name():
    """沙箱路径执行后端的稳定名称。

    进 WorkerReceipt 的 `execution_backend`：它必须描述**真实跑过的那个内核机制**，
    不能把 Linux bubblewrap 签成 Windows 低完整性。没有可用后端时返回 None。
    """
    backend = selected_backend()
    if backend is None:
        return None
    return _RECEIPT_BACKEND_NAMES.get(backend.name, backend.name)


def capability_notes():
    """当前后端的能力措辞（`popper experiment isolation` 用）；无后端时为空。"""
    backend = selected_backend()
    return backend.capability_notes() if backend is not None else {}


def available():
    """本平台是否有可用的隔离后端（决定 ``--sandbox`` 能否成立）。"""
    return selected_backend() is not None


def job_limits_available():
    """内核强制的资源限额是否可用（Windows 由 Job Object，Linux 由 POSIX rlimit；
    无后端即不可用）。"""
    backend = selected_backend()
    return backend is not None and backend.limits_available()


def process_tree_termination_available():
    """整棵进程树能否被**内核级**终止（不只是尽力而为的遍历杀）。

    - POSIX：``base.launch_process_group`` 用 ``start_new_session`` + ``killpg``，
      进程组是内核对象，整组终止成立，与是否有隔离后端无关。
    - Windows：只有 Job Object（``TerminateJobObject`` / ``KILL_ON_JOB_CLOSE``）提供内核
      保证，因此取决于当前后端；Job 不可用时降级为 ``taskkill /T``，计为未实现。
    """
    if os.name != "nt":
        return True
    backend = selected_backend()
    return backend is not None and backend.tree_termination_available()


def netblock_available():
    """内核级断网是否可用（Linux：bwrap 空 netns，无需提权；Windows：仅提权环境下
    的 WFP/netsh 规则；其余情况不可用）。"""
    backend = selected_backend()
    return backend is not None and backend.network_block_available()


def label_write_scope(path):
    """把写作用域目录标记为后端可写（仅目录本身，不递归）。"""
    _require_backend().label_write_scope(path)


def unlabel_write_scope(path):
    """撤销写作用域标记（尽力而为）。"""
    _require_backend().unlabel_write_scope(path)


def seal_read(path):
    """禁止**更低权限**主体读 path（内核强制，不影响同级或更高权限主体）。"""
    _require_backend().seal_read(path)


def unseal_read(path):
    """撤销 ``seal_read`` 的标记。"""
    _require_backend().unseal_read(path)


def seal_read_available():
    """本平台能否做内核级读隔离（决定 ``--sandbox`` 对保留集的承诺能否成立）。"""
    backend = selected_backend()
    return backend is not None and backend.read_seal_available()


@contextlib.contextmanager
def sealed_reads(paths):
    """上下文内把给定路径对更低权限主体封读，退出时恢复。

    没有可用后端的平台上是空操作——但那里的 ``--sandbox`` 本身就会显式失败
    （``available()`` 为假），因此不存在「以为隔离了、其实没有」的成功路径。
    """
    backend = selected_backend()
    sealed = []
    try:
        if backend is not None:
            for path in paths:
                backend.seal_read(path)
                sealed.append(path)
        yield
    finally:
        for path in sealed:
            try:
                backend.unseal_read(path)
            except OSError:
                pass


def launch(command, cwd, env, stdout, stderr, timeout_seconds, *,
           mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
           sandboxed=False, on_spawn=None):
    """**唯一的受限执行入口**：``sandboxed`` 决定是否进入隔离后端，其余语义两路共用。

    - ``sandboxed=True``：交给当前平台的隔离后端（能力与机制措辞由后端的
      ``capability_notes()`` 提供，均为尽力而为、如实标注）。
      Windows 低完整性：文件写作用域仅运行工作区可写；HTTP 代理拦截（无管理员则无
      内核级断网）；sitecustomize 命令拦截（Python 层，可绕过）；Job Object 资源限额
      （JobMemoryLimit / PerProcessUserTimeLimit / ActiveProcessLimit +
      KILL_ON_JOB_CLOSE），内核强制。
      Linux bubblewrap：mount namespace 内宿主根只读 + 仅运行工作区 `--bind` 可写；
      `--unshare-net` 空 netns 断网；POSIX rlimit 限额；同样经 sitecustomize 拦截命令。
      没有可用后端时直接报错，不降级为 trusted-local。
    - ``sandboxed=False``（trusted-local）：进程组（POSIX 新会话 / Windows 新进程组）
      + 强杀整树；POSIX 另用 RLIMIT_AS / RLIMIT_CPU，Windows 上强制不了的限额参数
      **显式报错**而不是静默丢弃。

    超时与非零退出都终止整棵树，语义与 ``subprocess`` 一致：成功返回 ``Popen`` 句柄
    （沙箱路径返回的是引导进程），失败抛 OSError / TimeoutExpired / CalledProcessError。
    """
    if sandboxed:
        return _require_backend().launch(
            command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
            timeout_seconds=timeout_seconds, mem_limit_mb=mem_limit_mb,
            cpu_time_seconds=cpu_time_seconds, max_processes=max_processes,
            on_spawn=on_spawn)
    return base.launch_process_group(command, cwd=cwd, env=env, stdout=stdout,
                                     stderr=stderr, timeout_seconds=timeout_seconds,
                                     mem_limit_mb=mem_limit_mb,
                                     cpu_time_seconds=cpu_time_seconds,
                                     on_spawn=on_spawn)
