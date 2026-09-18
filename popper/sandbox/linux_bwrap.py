"""Linux bubblewrap 隔离后端（``SandboxBackend`` 协议的 Linux 实现）。

执行边界 = 写作用域限定运行工作区（run_dir）：
- 候选进程经 **bubblewrap** 启动到新的 mount/pid/ipc/uts/net/user namespace：
  ``--unshare-all`` 让候选看不到宿主的进程、网络、IPC；``--ro-bind / /`` 让候选只读
  整个宿主根（含项目代码、Python 解释器、stdlib 等），``--bind <scope> <scope>``
  让候选只在运行工作区可写。无需 root（unprivileged user namespaces）。
- 运行工作区经 `label_write_scope` 记录路径，launch 时构造对应 `--bind`；本身不
  修改宿主权限位（与 Windows Low IL 的 icacls 不同，Linux 无对应内核标签语义）。
- 诚实边界：本模块约束"写"（mount namespace 只挂载 scope 为 rw）、对显式
  `seal_read` 的对象用 `--ro-bind /dev/null <path>` 屏蔽（候选读不到真实数据：
  简单挂载形状下读得空串 EOF，多挂载叠放的真实端到端在 GitHub runner 上实测为
  PermissionError/EACCES；两种形状都内核强制、绕不过，实施记录 run 35335109099
  有原始观察表）、`--unshare-net` 内核级断网（空 netns）、POSIX
  RLIMIT_AS / RLIMIT_CPU 内核强制的资源限额；RLIMIT_NPROC 按真实 UID 计数而不是
  按进程树，因此只能给天花板兜底，不是 Windows Job Object 那种按作业精确计数。
- 平台差异：Windows 候选读保留集得 PermissionError（MIC 强制标签 NO_READ_UP）；
  Linux 候选读保留集得空串或 EACCES（/dev/null 屏蔽，具体形状随挂载叠放变化）。
  两者都是「内核强制、候选无法读出真实数据」，门禁按 BLOCKED 前缀两种都收，单测
  对简单形状断言空串、端到端对真实形状断言 BLOCKED*。
- 只支持 Linux + bwrap 真的能建命名空间：`available()` 用**真实沙箱形状探活**，不只看
  版本号——Ubuntu 24.04+ 的 AppArmor 策略会在 bwrap 已安装时仍拒绝非特权用户命名空间。
  探活失败时 `--sandbox` 显式失败并给出修复指引，而不是跑到一半才炸。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .base import force_kill_tree, proxy_block_env, reap
from .win_lowil import SITECUSTOMIZE

# bubblewrap bootstrap：在沙箱内 Python 入口运行前设置 rlimit + sitecustomize 拦截。
# 与 Windows BOOTSTRAP 同思路：先约束自身（资源限额 + 命令拦截），再 runpy 真实入口。
BOOTSTRAP = r'''
import os, resource, runpy, sys

def _apply_limits(mem_mb, cpu_seconds, nproc_ceiling):
    if mem_mb:
        cap = int(mem_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    if cpu_seconds:
        limit = int(cpu_seconds)
        resource.setrlimit(resource.RLIMIT_CPU, (limit, limit))
    if nproc_ceiling:
        # RLIMIT_NPROC 按**真实 UID** 计数，不是按进程树；父进程已把「宿主上本 UID
        # 当前进程数 + 声明的进程数」合成天花板传进来，避免候选一个子进程都起不来。
        ceiling = int(nproc_ceiling)
        resource.setrlimit(resource.RLIMIT_NPROC, (ceiling, ceiling))

_apply_limits(os.environ.get("POPPER_MEM_MB"),
              os.environ.get("POPPER_CPU_SECONDS"),
              os.environ.get("POPPER_NPROC_CEILING"))

entrypoint = sys.argv[1] if len(sys.argv) > 1 else None
if entrypoint is None:
    sys.exit(0)
sys.argv = [entrypoint] + sys.argv[2:]
sys.path.insert(0, os.path.dirname(entrypoint) or ".")
runpy.run_path(entrypoint, run_name="__main__")
'''

_AVAILABLE = None
_PROBE_DETAIL = ""
_SCOPE_PATHS = set()
_SEALED_PATHS = set()

# 探活用的最小沙箱形状，与 launch 实际使用的一致：只 `bwrap --version` 会把「装了
# 就等于能用」的假前提带进来，非特权用户命名空间被禁时版本探活依然返回 0。
_PROBE_ARGV = ("--unshare-all", "--ro-bind", "/", "/", "--dev", "/dev",
               "--proc", "/proc", "--tmpfs", "/tmp")

APPARMOR_HINT = ("Ubuntu 23.10+/24.04+ 默认由 AppArmor 拒绝非特权用户命名空间：需在 "
                 "/etc/apparmor.d/bwrap 给 /usr/bin/bwrap 开 userns 例外并 reload apparmor"
                 "（CI 的做法见 .github/workflows/ci.yml 的 linux job）")

# RLIMIT_NPROC 读不到宿主当前进程数时用的保守余量。
_NPROC_UNKNOWN_HEADROOM = 512


def _is_linux_platform():
    """本后端在当前平台**能否被调用**（不同于 ``available()``：那只回答「bwrap 能不能
    建命名空间」）。集中成一个函数，是为了让 argv 构造这类纯逻辑可在其它平台被测试。"""
    return os.name == "posix" and sys.platform.startswith("linux")


def _run_probe():
    """跑一次真实的最小沙箱（用当前解释器），返回 (是否可用, 失败详情)。"""
    argv = ["bwrap", *_PROBE_ARGV, "--", sys.executable, "-c", "pass"]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=60)
    except FileNotFoundError:
        return False, "bwrap 未安装：apt install bubblewrap 或等价命令"
    except subprocess.TimeoutExpired:
        return False, "bwrap 探活超时（60s）"
    except OSError as error:
        return False, f"bwrap 无法执行：{type(error).__name__}: {error}"
    if proc.returncode == 0:
        return True, ""
    detail = ((proc.stderr or b"").decode("utf-8", "replace").strip()
              or (proc.stdout or b"").decode("utf-8", "replace").strip())
    return False, f"bwrap 退出码 {proc.returncode}: {detail or '无输出'}"


def available():
    """bubblewrap 后端是否真的可用（Linux + 真实沙箱探活，结果缓存）。"""
    global _AVAILABLE, _PROBE_DETAIL
    if _AVAILABLE is None:
        if not _is_linux_platform():
            _AVAILABLE, _PROBE_DETAIL = False, "非 Linux 平台"
        else:
            _AVAILABLE, _PROBE_DETAIL = _run_probe()
    return _AVAILABLE


def probe_detail():
    """最近一次探活的失败原因（成功时为空串）。"""
    available()
    return _PROBE_DETAIL


def _require_bwrap():
    """不可用就显式失败，并给出可执行指引——绝不静默降级为 trusted-local。"""
    if not available():
        raise OSError(f"bubblewrap 沙箱后端不可用：{probe_detail() or '未知原因'}。"
                      f"{APPARMOR_HINT}。请修复后重试，或改用 --trusted-local 人工路径")


def limits_available():
    """POSIX RLIMIT 在 Linux 内核强制，始终可用（资源限额无 False 路径）。"""
    return True


def tree_termination_available():
    """整棵进程树能否被内核级终止：POSIX 进程组 + bubblewrap PID 1 namespace。"""
    return True


def network_block_available():
    """内核级断网：bwrap `--unshare-net` 创建空 netns，候选无任何网络访问。"""
    return True


def seal_read_available():
    """mount namespace per-launch 屏蔽：始终可用（bwrap `--ro-bind /dev/null <path>`）。"""
    return True


def _abs(path):
    """bwrap 的挂载参数必须是绝对路径（namespace 内没有「相对宿主 cwd」的概念）。"""
    return os.path.abspath(os.fspath(path))


def label_write_scope(path):
    """记录写作用域路径，供 launch 构造 `--bind <path> <path>`（rw）。

    与 Windows Low IL 的 icacls 不同，Linux 无对应内核标签语义；scope 仅作为
    mount namespace 内的 rw 挂载点，无需修改宿主权限位。
    """
    if not _is_linux_platform():
        raise OSError("本函数只实现 Linux bubblewrap 写作用域"
                      "（Windows 由低完整性标记提供）")
    _SCOPE_PATHS.add(_abs(path))


def unlabel_write_scope(path):
    """撤销 `label_write_scope` 的记录（尽力而为）。"""
    if not _is_linux_platform():
        raise OSError("本函数只实现 Linux bubblewrap 写作用域"
                      "（Windows 由低完整性标记提供）")
    _SCOPE_PATHS.discard(_abs(path))


def seal_read(path):
    """把 path 加入进程级 sealed 集合，下次 launch 时用 `--ro-bind /dev/null` 屏蔽。

    与 Windows MIC 强制标签的内核级 toggle 不同，Linux 后端的 seal 是 per-launch
    的状态记录：bubblewrap 的 mount namespace 在 launch 时固定，无法运行时改；
    因此 seal_read 只标记，launch 时按集合构造 bwrap argv。

    候选读不到真实数据：简单挂载形状下读到 /dev/null 的 EOF（空字符串）；多挂载
    叠放的真实端到端在 GitHub runner 上实测也会是 PermissionError/EACCES（见实施
    记录 run 35335109099 的观察表）。两种形状都内核强制、候选无法绕过；调用方
    判断封读是否成立应按「读不到真值」而不是按具体错误名。
    """
    if not _is_linux_platform():
        raise OSError("本函数只实现 Linux mount namespace 屏蔽禁读"
                      "（Windows 由强制完整性标签 NO_READ_UP 提供）")
    target = _abs(path)
    if not os.path.exists(target):
        # 登记一个不存在的路径 = 「以为封了读、其实什么都没挡」。宿主根只读时 bwrap
        # 也无法凭空造出挂载点，因此这里显式失败而不是静默跳过。
        raise OSError(f"内核级禁读要求目标已存在：{target}")
    _SEALED_PATHS.add(target)


def unseal_read(path):
    """撤销 `seal_read` 的记录。"""
    if not _is_linux_platform():
        raise OSError("本函数只实现 Linux mount namespace 屏蔽禁读"
                      "（Windows 由强制完整性标签 NO_READ_UP 提供）")
    _SEALED_PATHS.discard(_abs(path))


def _command_interception_env(env):
    """把 sitecustomize.py 放进 PYTHONPATH 前缀目录，使沙箱进程树内所有 Python
    进程在启动时安装命令拦截。返回写入 POPPER_SANDBOX_SHIMDIR 的环境副本。"""
    shim_dir = Path(tempfile.gettempdir()) / f"popper-cmdshim-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    shim_dir.mkdir(parents=True, exist_ok=True)
    (shim_dir / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    env = dict(env)
    env["POPPER_SANDBOX_SHIMDIR"] = str(shim_dir)
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(shim_dir) + (os.pathsep + old if old else "")
    return env


def _cleanup_interception_env(env):
    shim_dir = env.get("POPPER_SANDBOX_SHIMDIR")
    if not shim_dir:
        return
    try:
        shutil.rmtree(shim_dir, ignore_errors=True)
    except OSError:
        pass


def _uid_process_count():
    """宿主上本 UID 的进程数（尽力而为，读不到返回 None）。"""
    try:
        uid = os.getuid()
    except AttributeError:
        return None
    count = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Uid:"):
                        if int(line.split()[1]) == uid:
                            count += 1
                        break
        except (OSError, ValueError, IndexError):
            continue
    return count


def _nproc_ceiling(max_processes):
    """把「候选最多 N 个进程」换算成 RLIMIT_NPROC 能用的天花板。

    RLIMIT_NPROC 按真实 UID 计数（整个宿主会话都算），直接写声明值会把用户已有的
    进程数算进去，候选可能连一个子进程都起不来。因此取「当前本 UID 进程数 + 声明值」：
    仍是内核强制的 fork 炸弹兜底，但不是 Windows Job Object 那种按作业精确计数，
    这一点在能力清单里如实标注。
    """
    current = _uid_process_count()
    if current is None:
        return int(max_processes) + _NPROC_UNKNOWN_HEADROOM
    return current + int(max_processes)


def _build_bwrap_argv(scope_paths, sealed_paths, ro_paths=()):
    """构造 bubblewrap 的 argv：unshare-all + ro-bind / + bind scope + 屏蔽 sealed。

    挂载的**叠放顺序**就是语义，不要随意调换：

    1. ``--ro-bind / /``：宿主根只读（项目代码、解释器、stdlib 可见但不可写）。
    2. ``--dev`` / ``--proc`` / ``--tmpfs /tmp``：新的空 /tmp 会**遮住宿主 /tmp**，所以任何
       落在 /tmp 下的必需路径（命令拦截 shim 目录、临时工作区）都必须在其后显式再挂，
       否则「以为挂了、其实被 tmpfs 盖住」。
    3. ``ro_paths``：命令拦截 shim 目录（sitecustomize 注入依赖它在沙箱内可见）。
    4. 每个禁读路径的**父目录**只读挂载：保证 ``/dev/null`` 的遮蔽落在真实存在的文件上，
       而不是让 bwrap 在只读根上猜一个挂载点。
    5. ``--bind <scope> <scope>``：运行工作区可写。放在父目录只读挂载之后，避免「禁读
       文件正好在工作区内」时把整个工作区留在只读视图上。
    6. ``--ro-bind /dev/null <sealed>``：最后叠放，路径最深，一定盖在上述读写视图之上。
    """
    argv = ["bwrap", "--unshare-all", "--die-with-parent",
            "--ro-bind", "/", "/",             # 宿主根只读
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp"]                 # 候选的临时文件只存在于 namespace 内
    ro_paths = set(ro_paths)
    for path in sorted(ro_paths):
        argv += ["--ro-bind", path, path]
    parents = {os.path.dirname(p) for p in sealed_paths}
    # 禁读文件直接在根下时，父目录就是已经只读挂载的 /（两种写法都算），不能重复挂。
    for parent in sorted(p for p in parents
                         if p and p not in ("/", os.sep) and p not in ro_paths):
        argv += ["--ro-bind", parent, parent]
    for scope in sorted(scope_paths):
        argv += ["--bind", scope, scope]
    for sealed in sorted(sealed_paths):
        argv += ["--ro-bind", "/dev/null", sealed]
    argv.append("--")
    return argv


def launch_bwrap(command, cwd, env, stdout, stderr, timeout_seconds,
                 mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
                 on_spawn=None):
    """沙箱路径：bubblewrap 启动到新 namespace，bootstrap 设置 rlimit 后运行真实入口。"""
    if not _is_linux_platform():
        raise OSError("bubblewrap 后端只实现于 Linux（沙箱路径另有 Windows 低完整性后端）")
    _require_bwrap()
    # 空 netns 已内核级断网；环境变量代理作为纵深防御保留（能力清单里该条目的措辞
    # 依赖它真的被设置）。
    env = proxy_block_env(env)
    env = _command_interception_env(env)
    # 把限额参数透传给 bootstrap（在沙箱内 Python 入口启动时设置 rlimit）：preexec 只能
    # 作用在 Popen 直接启动的 bwrap 进程上，影响不到 namespace 内的 python，故必须在沙箱内设。
    if mem_limit_mb:
        env["POPPER_MEM_MB"] = str(int(mem_limit_mb))
    if cpu_time_seconds:
        env["POPPER_CPU_SECONDS"] = str(int(float(cpu_time_seconds)))
    if max_processes:
        env["POPPER_NPROC_CEILING"] = str(_nproc_ceiling(max_processes))
    # cwd 必须在 mount namespace 内可见且可写：把它加入 scope_paths（如果尚未加入）
    scope_paths = set(_SCOPE_PATHS)
    if cwd:
        scope_paths.add(_abs(cwd))
    # 命令拦截 shim 目录常落在 /tmp，会被 `--tmpfs /tmp` 遮住，必须显式挂回去
    ro_paths = {env["POPPER_SANDBOX_SHIMDIR"]} if env.get("POPPER_SANDBOX_SHIMDIR") else set()
    bwrap_argv = _build_bwrap_argv(scope_paths, set(_SEALED_PATHS), ro_paths)
    # 真实命令拼在 -- 之后：bwrap 启动 Python 解释器执行 bootstrap，bootstrap 再 runpy 真实入口
    bootstrap_cmd = [sys.executable, "-c", BOOTSTRAP] + [str(arg) for arg in command[1:]]
    full_cmd = bwrap_argv + bootstrap_cmd
    try:
        # bwrap 是新 pid namespace 的 1 号：杀它 = 内核回收整个 namespace，整树不留存。
        # `--die-with-parent` 再加上父进程被 SIGKILL 时沙箱也跟随退出。
        proc = subprocess.Popen(full_cmd, cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, stdout=stdout,
                                stderr=stderr, start_new_session=True)
        if on_spawn is not None:
            on_spawn(proc)
        reap(command, proc, timeout_seconds, force_kill_tree)
        return proc
    finally:
        _cleanup_interception_env(env)


class LinuxBubblewrapBackend:
    """``SandboxBackend`` 的 Linux bubblewrap 实现。

    与 `WindowsLowIntegrityBackend` 同样的薄适配：实现留在模块级函数里，便于
    `popper experiment isolation` 与测试直接引用单个能力探针，不必先构造后端对象。
    """

    name = "linux_bwrap"

    def available(self):
        return available()

    def limits_available(self):
        return limits_available()

    def tree_termination_available(self):
        return tree_termination_available()

    def network_block_available(self):
        return network_block_available()

    def read_seal_available(self):
        return seal_read_available()

    def label_write_scope(self, path):
        return label_write_scope(path)

    def unlabel_write_scope(self, path):
        return unlabel_write_scope(path)

    def seal_read(self, path):
        return seal_read(path)

    def unseal_read(self, path):
        return unseal_read(path)

    def launch(self, command, cwd, env, stdout, stderr, timeout_seconds, *,
               mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
               on_spawn=None):
        return launch_bwrap(command, cwd, env, stdout, stderr, timeout_seconds,
                            mem_limit_mb=mem_limit_mb,
                            cpu_time_seconds=cpu_time_seconds,
                            max_processes=max_processes, on_spawn=on_spawn)

    def capability_notes(self):
        return CAPABILITY_NOTES


# 能力清单（popper experiment isolation）里平台相关条目的措辞由后端自己提供，
# 避免 Linux 上打印出只有 Windows 才成立的机制描述。
CAPABILITY_NOTES = {
    "os_sandbox_filesystem_scope":
        "Linux mount namespace 写作用域：`--ro-bind / /` 把宿主根整体只读，只对运行工作区"
        "`--bind` 为可读写；新 /tmp 仅存在于 namespace 内。候选仍可读作用域外的非禁读文件",
    "network_default_off":
        "内核级断网（无需提权）：`--unshare-net` 给候选一个空 netns，除 loopback 外无任何"
        "网卡/DNS/路由，比 Windows 非提权环境下的代理拦截更强",
    "http_proxy_interception":
        "网络已由空 netns 内核级阻断；环境变量代理指向死端口作为纵深防御保留",
    "low_privilege_process":
        "候选跑在新 mount/pid/ipc/uts/net/user namespace 内（`--unshare-all`）：看不到宿主"
        "进程表、不能写未绑定目录；namespace 内的 root 对外部主体无特权",
    "command_interception":
        "Python 层命令拦截（经 PYTHONPATH 注入 sitecustomize，shim 目录已显式挂回 namespace，"
        "否则会被 `--tmpfs /tmp` 遮住）：高危命令直接拒绝；可用原生 syscall 绕过，非内核级",
    "resource_limits":
        "POSIX rlimit 内核强制（沙箱内入口启动前设置）：RLIMIT_AS 内存、RLIMIT_CPU CPU 时间；"
        "RLIMIT_NPROC 按真实 UID 计数而非按进程树，因此是「宿主当前本 UID 进程数 + 声明值」"
        "的天花板（fork 炸弹兜底），不是 Windows Job Object 那种按作业精确计数",
    "process_tree_termination":
        "bwrap 是新 pid namespace 的 1 号：杀它即由内核回收整个 namespace 内所有进程；"
        "另用 `start_new_session` + killpg(SIGKILL) 与 `--die-with-parent` 兜底",
    "heldout_sealed_channel":
        "mount namespace 屏蔽：消费测试集期间对已登记的测试数据文件 `--ro-bind /dev/null "
        "<path>`，候选读不到真实数据（简单形状下读到空内容 EOF；多挂载叠放的真实端到端"
        "实测也可能直接得 EACCES，run 35335109099 观察表为证），控制器与用户（namespace 外）"
        "不受影响。与 Windows 的 NO_READ_UP（读它得 EACCES）不同：两个平台都是内核强制、候选"
        "都读不出真实数据，Linux 侧错误形状随挂载叠放变化。屏蔽在 launch 时固定，无法运行时"
        "切换，因此只在 `sealed_reads` 包住 launch 的用法下成立；需要账户级/主机级隔离的正式"
        "确认仍应用外部评估服务",
}


BACKEND = LinuxBubblewrapBackend()
