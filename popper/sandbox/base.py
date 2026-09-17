"""沙箱后端协议，以及所有后端共用的执行 / 终止部件。

本模块只定义**契约**，不提供任何隔离实现：

- ``SandboxBackend`` 描述一个隔离后端必须回答的能力问题与必须提供的操作。能力问题
  （``available`` / ``limits_available`` / ...）必须是**实算**的，不能硬编码 True：调用方
  （``popper experiment isolation``、``--sandbox`` 门禁）据此如实声明，而不是承诺一个不成立的前提。
- trusted-local 的进程组执行（``launch_process_group``）**不是**后端：它没有隔离主张，
  只是"不沙箱"时的受限执行方式，因此任何平台都必须可用。
- 新增后端 = 实现 ``SandboxBackend`` + 在 ``popper/sandbox/__init__.py`` 的注册表加一项。
  已登记的后端：Windows 低完整性（``win_lowil``）与 Linux bubblewrap（``linux_bwrap``，
  需 bwrap 可建非特权命名空间）；macOS seatbelt **尚未实现**，故未注册：无可用后端的
  平台上 ``--sandbox`` 会显式报错而非静默降级。
"""
from __future__ import annotations

import os
import signal
import subprocess
from typing import Protocol, runtime_checkable

# 无控制台父进程（GUI/宿主环境）下不给 taskkill 及受限子进程弹可见控制台窗口；
# 输出均已被捕获或重定向，不受影响。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@runtime_checkable
class SandboxBackend(Protocol):
    """隔离执行后端的契约。

    ``launch`` 的异常语义与 ``subprocess`` 一致：成功返回 ``Popen`` 句柄（沙箱路径
    返回的是引导进程），超时抛 ``TimeoutExpired``，非零退出抛 ``CalledProcessError``，
    后端不可用时抛 ``OSError``。超时与非零退出都必须终止整棵进程树。
    """

    name: str

    def available(self) -> bool:
        """隔离能力在当前系统是否真的可用（必须探活，不能假设）。"""

    def limits_available(self) -> bool:
        """内核强制的资源限额（内存 / CPU 时间 / 进程数）是否可用。"""

    def tree_termination_available(self) -> bool:
        """整棵进程树能否被**内核级**终止，而不只是尽力而为的遍历杀。"""

    def network_block_available(self) -> bool:
        """内核级断网是否可用。"""

    def read_seal_available(self) -> bool:
        """能否禁止更低权限主体读取指定对象（读隔离）。"""

    def label_write_scope(self, path) -> None:
        """把 path 标记为本后端可写的运行工作区（仅目录本身，不递归）。"""

    def unlabel_write_scope(self, path) -> None:
        """撤销 ``label_write_scope`` 的标记（尽力而为）。"""

    def seal_read(self, path) -> None:
        """禁止更低权限主体读取 path。"""

    def unseal_read(self, path) -> None:
        """撤销 ``seal_read`` 的标记。"""

    def launch(self, command, cwd, env, stdout, stderr, timeout_seconds, *,
               mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
               on_spawn=None):
        """在本后端的隔离下执行 command，并返回 Popen 句柄。"""

    def capability_notes(self) -> dict:
        """能力清单（``popper experiment isolation``）里本后端的机制描述。

        返回 ``{item_id: note}``，只覆盖平台相关的条目；未覆盖的条目用 ``isolation.py``
        里的平台中立默认措辞。措辞跟着后端走，是为了避免在 Linux 上打印只有 Windows
        才成立的机制描述（文档承诺 > 实现的那类漂移）。
        """


def proxy_block_env(env):
    """把 HTTP(S) 代理指向死端口，拦截遵循代理的客户端（urllib/requests/pip 等）。

    平台中立：Windows 非提权环境下它是唯一的网络拦截手段；Linux bubblewrap 已有内核级
    空 netns，这一步只作为纵深防御保留。
    """
    env = dict(env)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        env[name] = "http://127.0.0.1:9"
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    return env


def reap(command, proc, timeout_seconds, kill):
    """唯一的等待/终止实现：超时或非零退出都终止整棵树，异常语义与 subprocess 一致。"""
    try:
        proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        kill(proc.pid)
        try:
            proc.wait(timeout=5)  # 回收进程句柄，避免僵尸/ResourceWarning
        except Exception:
            pass
        raise
    if proc.returncode != 0:
        kill(proc.pid)
        raise subprocess.CalledProcessError(proc.returncode, command)


def launch_process_group(command, cwd, env, stdout, stderr, timeout_seconds,
                         mem_limit_mb, cpu_time_seconds, on_spawn=None):
    """trusted-local 路径：进程组 + 尽力而为的整树强杀（无隔离主张）。

    Windows 上没有 Job Object 就无法内核级限额，用户声明的约束宁可报错也不静默失效。
    ``on_spawn`` 在 Popen 之后、reap 之前调用，使分离式 supervisor 的取消观察线程
    能拿到进程树根 PID（与沙箱后端路径的 ``on_spawn`` 语义一致）。
    """
    kwargs, flags, preexec = {}, 0, None
    if os.name == "nt":
        if mem_limit_mb:
            raise OSError(
                "Windows 非沙箱路径无法强制 mem_limit_mb：内存限额只由 Job Object 提供，"
                "而 Job 仅在 --sandbox 下创建。请改用 --sandbox，或去掉该实验声明")
        if cpu_time_seconds and float(cpu_time_seconds) < float(timeout_seconds):
            raise OSError(
                "Windows 非沙箱路径无法强制比 wall-clock 更紧的 cpu_time_seconds："
                "CPU 时间限额只由 Job Object 提供。请改用 --sandbox，"
                "或把 cpu_time_seconds 调到不小于 timeout_seconds")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
        if mem_limit_mb or cpu_time_seconds:
            import resource

            def preexec():
                if cpu_time_seconds:
                    limit = int(cpu_time_seconds)
                    resource.setrlimit(resource.RLIMIT_CPU, (limit, limit))
                if mem_limit_mb:
                    cap = int(mem_limit_mb) * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=stdout, stderr=stderr, creationflags=flags,
                            preexec_fn=preexec, **kwargs)
    if on_spawn is not None:
        on_spawn(proc)
    reap(command, proc, timeout_seconds, force_kill_tree)
    return proc


def _taskkill_tree(pid):
    """Windows 强杀整棵树（按父子关系遍历，孙进程被重新挂靠后可能逃逸）。"""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, check=False, creationflags=NO_WINDOW)
    except Exception:
        pass


def force_kill_tree(pid):
    """强杀整棵树：Windows job/taskkill /F，POSIX SIGKILL 进程组（回退单进程）。"""
    if os.name == "nt":
        _taskkill_tree(pid)
        return
    try:
        os.killpg(int(pid), signal.SIGKILL)
        return
    except Exception:
        pass
    try:
        os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass


def terminate_tree(pid):
    """向任务树根 PID 发温和终止信号（跨进程取消入口；供 supervisor 使用）。"""
    if os.name == "nt":
        _taskkill_tree(pid)
    else:
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception:
                pass
