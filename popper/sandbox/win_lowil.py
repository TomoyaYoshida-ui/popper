"""Windows 低完整性隔离后端（``SandboxBackend`` 协议的 Windows 实现）。

执行边界 = 写作用域限定运行工作区（run_dir）：
- 候选进程以 **自降级完整性（self-lowering）** 启动：引导代码先把自己的
  Integrity Level 降到 Low（S-1-16-4096），再运行真实入口。无需管理员权限
  或 SeImpersonatePrivilege（本机账户无该权限，故不用 CreateProcessWithTokenW）。
  Windows 默认强制完整性策略：Low 主体可读 Medium 对象，但不能写 Medium/High 对象。
- 运行工作区经 `icacls /setintegritylevel Low` 标记：候选进程只能在该目录内
  新建/写入；复制进工作区的既有文件（Medium）与项目其他目录对候选只读。
- 诚实边界：本模块约束"写"、对显式 `seal_read` 的对象禁止更低完整性主体"读"，
  以及尽力而为的 HTTP 代理拦截；其余读取不受限，裸 socket/DNS 不受限
  （网络要素在当前无管理员环境下无法做到内核级断网）。
- 只支持 Windows：非 Windows 上 ``available()`` 为假，``--sandbox`` 因此显式失败。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path

from .base import NO_WINDOW, force_kill_tree, proxy_block_env, reap

# 子进程命令拦截：注入 sitecustomize.py（经 PYTHONPATH），在 Python 层包装
# subprocess.Popen / os.system，高危命令直接拒绝。候选程序均为 Python，故该层
# 能覆盖其绝大多数子进程调用；可用 ctypes/CreateProcess 等原生 API 绕过，
# 如实标注为"Python 层拦截，非内核级安全边界"。
SITECUSTOMIZE = r'''
"""Popper 沙箱 · 命令拦截（Python 层，尽力而为）。"""
import os
import subprocess

_DENY = frozenset({
    "rm", "rmdir", "del", "erase", "format", "diskpart", "cleanmgr",
    "curl", "wget", "certutil", "bitsadmin",
    "powershell", "pwsh", "cmd", "cscript", "wscript", "mshta",
    "runas", "schtasks", "reg", "regedit", "sc", "net", "wmic",
    "shutdown", "taskkill", "psexec", "attrib", "cacls", "icacls",
})


def _blocked(prog):
    base = os.path.basename(os.fspath(prog)).lower()
    return base in _DENY or os.path.splitext(base)[0] in _DENY


def _first_token(command):
    if isinstance(command, bytes):
        command = command.decode("utf-8", "replace")
    if not isinstance(command, str):
        return None
    return command.strip().split()[0] if command.strip() else None


def _check(args):
    token = _first_token(args) if isinstance(args, (str, bytes, bytearray)) else \
        (str(args[0]) if args else None)
    if token and _blocked(token):
        raise PermissionError("Popper sandbox: blocked command: " + token)


_orig_popen = subprocess.Popen


class _popen(_orig_popen):
    def __init__(self, args, *a, **kw):
        _check(args)
        super().__init__(args, *a, **kw)


subprocess.Popen = _popen

_orig_system = os.system


def _system(command):
    _check(command)
    return _orig_system(command)


os.system = _system
'''

# 子进程引导代码：自降级到 Low 完整性后运行真实入口。独立可执行，不 import 本模块。
BOOTSTRAP = r'''
import ctypes, os, runpy, sys
from ctypes import wintypes

adv = ctypes.WinDLL("advapi32", use_last_error=True)
ker = ctypes.WinDLL("kernel32", use_last_error=True)

class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]

adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.HANDLE)]
adv.OpenProcessToken.restype = wintypes.BOOL
adv.SetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD]
adv.SetTokenInformation.restype = wintypes.BOOL
adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
adv.ConvertStringSidToSidW.restype = wintypes.BOOL
adv.GetLengthSid.argtypes = [ctypes.c_void_p]
adv.GetLengthSid.restype = wintypes.DWORD
ker.GetCurrentProcess.restype = wintypes.HANDLE
ker.LocalFree.argtypes = [wintypes.HANDLE]
ker.LocalFree.restype = wintypes.HANDLE
ker.CloseHandle.argtypes = [wintypes.HANDLE]
ker.CloseHandle.restype = wintypes.BOOL

def _lower_self():
    h = wintypes.HANDLE()
    if not adv.OpenProcessToken(ker.GetCurrentProcess(), 0x0008 | 0x0080, ctypes.byref(h)):
        sys.exit(91)
    try:
        psid = ctypes.c_void_p()
        if not adv.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(psid)):
            sys.exit(92)
        try:
            sid_len = int(adv.GetLengthSid(psid))
            total = ctypes.sizeof(TOKEN_MANDATORY_LABEL) + sid_len
            buf = ctypes.create_string_buffer(total)
            label = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
            label.Label.Sid = ctypes.addressof(buf) + ctypes.sizeof(TOKEN_MANDATORY_LABEL)
            label.Label.Attributes = 0x20  # SE_GROUP_INTEGRITY
            ctypes.memmove(ctypes.c_void_p(label.Label.Sid), ctypes.c_void_p(psid.value), sid_len)
            if not adv.SetTokenInformation(h, 25, buf, total):  # TokenIntegrityLevel
                sys.exit(93)
        finally:
            ker.LocalFree(psid)
    finally:
        ker.CloseHandle(h)

def _strip_privileges():
    """剥离危险特权（低权限进程）：SE_PRIVILEGE_REMOVED，只对令牌已持有的生效，
    缺失特权返回 1300(NOT_ALL_ASSIGNED) 属正常。保留 SeChangeNotifyPrivilege。"""
    class _LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_long)]
    class _LA(ctypes.Structure):
        _fields_ = [("Luid", _LUID), ("Attributes", wintypes.DWORD)]
    class _TP(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", _LA * 1)]
    adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                          ctypes.POINTER(_LUID)]
    adv.LookupPrivilegeValueW.restype = wintypes.BOOL
    adv.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    adv.AdjustTokenPrivileges.restype = wintypes.BOOL
    h = wintypes.HANDLE()
    if not adv.OpenProcessToken(ker.GetCurrentProcess(), 0x0008 | 0x0020, ctypes.byref(h)):
        return
    try:
        for name in ("SeDebugPrivilege", "SeShutdownPrivilege", "SeTakeOwnershipPrivilege",
                     "SeRestorePrivilege", "SeBackupPrivilege", "SeLoadDriverPrivilege",
                     "SeAssignPrimaryTokenPrivilege", "SeTcbPrivilege", "SeImpersonatePrivilege",
                     "SeCreateTokenPrivilege", "SeManageVolumePrivilege", "SeIncreaseQuotaPrivilege",
                     "SeLockMemoryPrivilege", "SeSecurityPrivilege", "SeSystemEnvironmentPrivilege",
                     "SeProfileSingleProcessPrivilege", "SeSystemtimePrivilege"):
            luid = _LUID()
            if not adv.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                continue
            tp = _TP()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = 0x40000000  # SE_PRIVILEGE_REMOVED
            adv.AdjustTokenPrivileges(h, False, ctypes.byref(tp), 0, None, None)
    finally:
        ker.CloseHandle(h)

# 先剥离特权再降级完整性：Low IL 进程无法以 TOKEN_ADJUST_PRIVILEGES 打开自身令牌（实证）。
_strip_privileges()
_lower_self()
entrypoint = sys.argv[1] if len(sys.argv) > 1 else None
if entrypoint is None:
    sys.exit(0)
sys.argv = [entrypoint] + sys.argv[2:]
sys.path.insert(0, os.path.dirname(entrypoint) or ".")
runpy.run_path(entrypoint, run_name="__main__")
'''

# ---- Job Object 资源限额（Windows 专用） ----
# 经实证验证（本机）：JOB_OBJECT_LIMIT_JOB_MEMORY 拒绝超额分配、
# JOB_OBJECT_LIMIT_PROCESS_TIME 终止超 CPU 时间进程，均生效（与网络速率控制不同）。
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_EXTENDED_LIMIT_INFO_CLASS = 9  # JobObjectExtendedLimitInformation

_CREATE_SUSPENDED = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004

# 强制标签（MIC）：对象标签 ACE 的掩码决定**完整性更低**的主体能做什么。
_MANDATORY_LABEL_NO_WRITE_UP = 0x1
_MANDATORY_LABEL_NO_READ_UP = 0x2
_MEDIUM_SID = "S-1-16-8192"

if os.name == "nt":

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    class _LARGE_INTEGER(ctypes.Structure):
        _fields_ = [("QuadPart", ctypes.c_int64)]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64)]

    class _JOB_BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", _LARGE_INTEGER),
                    ("PerJobUserTimeLimit", _LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _JOB_EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOB_BASIC_LIMIT),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG),
                    ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    kernel32.Thread32Next.restype = wintypes.BOOL

    def _create_limited_job(mem_limit_mb, cpu_time_seconds, max_processes):
        """创建带资源限额的 Job Object（内存/进程 CPU 时间/进程数 + 关闭即杀树）。"""
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error() or "CreateJobObjectW 失败")
        info = _JOB_EXTENDED_LIMIT()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if mem_limit_mb:
            flags |= _JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = int(mem_limit_mb * 1024 * 1024)
        if cpu_time_seconds:
            flags |= _JOB_OBJECT_LIMIT_PROCESS_TIME
            info.BasicLimitInformation.PerProcessUserTimeLimit = _LARGE_INTEGER(
                int(float(cpu_time_seconds) * 10_000_000))  # 100ns 单位
        if max_processes:
            flags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = int(max_processes)
        info.BasicLimitInformation.LimitFlags = flags
        if not kernel32.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT_INFO_CLASS,
                                                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            raise OSError(ctypes.get_last_error() or "SetInformationJobObject 失败")
        return job

    def _assign_to_job(job, pid):
        h = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not h:
            raise OSError(ctypes.get_last_error() or "OpenProcess 失败")
        try:
            if not kernel32.AssignProcessToJobObject(job, h):
                raise OSError(ctypes.get_last_error() or "AssignProcessToJobObject 失败")
        finally:
            kernel32.CloseHandle(h)

    def _resume_process(pid):
        """恢复 CREATE_SUSPENDED 启动的主线程（入组完成后才放行）。

        ``OpenThread`` 要的是线程 ID 而不是进程 ID，而 ``subprocess`` 不暴露创建时返回的
        线程句柄；新建的挂起进程只有主线程一个线程，故按属主进程枚举即可唯一确定。
        """
        owner = _thread_id_of(pid)
        thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, owner)
        if not thread:
            raise OSError(ctypes.get_last_error() or "OpenThread 失败")
        try:
            if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                raise OSError(ctypes.get_last_error() or "ResumeThread 失败")
        finally:
            kernel32.CloseHandle(thread)

    def _thread_id_of(pid):
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or int(snapshot) == -1:
            raise OSError(ctypes.get_last_error() or "CreateToolhelp32Snapshot 失败")
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        found = None
        try:
            ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == pid:
                    found = entry.th32ThreadID
                    break
                ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        if not found:
            raise OSError(ctypes.get_last_error() or "未找到沙箱进程的主线程")
        return found

    # ---- 内核级禁读（对象强制标签 + NO_READ_UP） ----
    # 低完整性进程读 Medium 对象默认是**允许**的（MIC 默认只管写），所以「读隔离」不能
    # 靠现有的 Low 完整性写作用域。把持密对象的强制标签 ACE 掩码加上 NO_READ_UP 后，
    # 完整性更低的沙箱进程读它会被内核直接拒绝（EACCES）；同级或更高的进程（控制器、
    # 用户自己）不受影响 —— 不需要管理员、不需要额外账户或容器。
    _SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
    _LABEL_SECURITY_INFORMATION = 0x00000010
    _SE_FILE_OBJECT = 1
    _ACL_HEADER_SIZE = 8
    _ACE_HEADER_SIZE = 4

    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR,
                                                ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                               ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_void_p]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.restype = wintypes.HANDLE

    def _set_label_policy(path, sid_string, policy_mask):
        """重写 path 的强制标签 ACE（单一 ACE：ACE_HEADER + 掩码 + SID）。"""
        sid = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(sid_string, ctypes.byref(sid)):
            raise OSError(ctypes.get_last_error() or "ConvertStringSidToSidW 失败")
        try:
            sid_length = advapi32.GetLengthSid(sid)
            ace_size = _ACE_HEADER_SIZE + 4 + sid_length
            acl_size = _ACL_HEADER_SIZE + ace_size
            buffer = ctypes.create_string_buffer(acl_size)
            ctypes.c_ubyte.from_buffer(buffer, 0).value = 2          # ACL_REVISION
            ctypes.c_ushort.from_buffer(buffer, 2).value = acl_size  # AclSize
            ctypes.c_ushort.from_buffer(buffer, 4).value = 1         # AceCount
            ctypes.c_ubyte.from_buffer(buffer, _ACL_HEADER_SIZE).value = \
                _SYSTEM_MANDATORY_LABEL_ACE_TYPE
            ctypes.c_ushort.from_buffer(buffer, _ACL_HEADER_SIZE + 2).value = ace_size
            ctypes.c_uint32.from_buffer(buffer, _ACL_HEADER_SIZE + 4).value = policy_mask
            ctypes.memmove(ctypes.byref(buffer, _ACL_HEADER_SIZE + 8), sid, sid_length)
            result = advapi32.SetNamedSecurityInfoW(
                str(path), _SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION,
                None, None, None, ctypes.cast(buffer, ctypes.c_void_p))
            if result:
                raise OSError(result, f"SetNamedSecurityInfoW 失败: {path}")
        finally:
            kernel32.LocalFree(sid)

_JOB_LIMITS = None
_SEAL = None
_AVAILABLE = None
_NETBLOCK = None


def available():
    """低完整性后端是否可用（子进程自降级探活，结果缓存）。"""
    global _AVAILABLE
    if _AVAILABLE is None:
        if os.name != "nt":
            _AVAILABLE = False
        else:
            try:
                proc = subprocess.run([sys.executable, "-c", BOOTSTRAP],
                                      stdin=subprocess.DEVNULL, capture_output=True,
                                      timeout=30, creationflags=NO_WINDOW)
                _AVAILABLE = proc.returncode == 0
            except Exception:
                _AVAILABLE = False
    return _AVAILABLE


def job_limits_available():
    """Job Object 资源限额是否可用（创建+设置内存限额探活，结果缓存）。"""
    global _JOB_LIMITS
    if _JOB_LIMITS is None:
        if os.name != "nt":
            _JOB_LIMITS = False
        else:
            try:
                job = _create_limited_job(mem_limit_mb=16, cpu_time_seconds=None,
                                          max_processes=None)
                kernel32.CloseHandle(job)
                _JOB_LIMITS = True
            except OSError:
                _JOB_LIMITS = False
    return _JOB_LIMITS


def tree_termination_available():
    """整棵进程树能否被内核级终止：Windows 上只有 Job Object 提供该保证。

    Job 不可用时降级为 ``taskkill /T``（按父子关系遍历，孙进程被重新挂靠后可能逃逸），
    因此计为未实现，而不是笼统标 True。
    """
    return job_limits_available()


# ---- 内核级断网（管理员门控 WFP，Windows 专用） ----
# 无管理员时内核断网不可行（WFP/netsh 需提权，AppContainer/CreateProcessWithTokenW
# 需本机缺失的特权），故该层仅在**提权**环境生效：为沙箱的**专用 python 副本**添加
# 程序级出站防火墙规则（避免阻断控制器自身解释器），运行结束后删除规则与副本。
def netblock_available():
    """内核级断网是否可用 = 当前进程已提权（可加 WFP 规则）。结果缓存。"""
    global _NETBLOCK
    if _NETBLOCK is None:
        _NETBLOCK = _is_elevated()
    return _NETBLOCK


def _is_elevated():
    if os.name != "nt":
        return False
    h = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(h)):
        return False
    try:
        elevation = _TOKEN_ELEVATION()
        returned = wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(h, 20, ctypes.byref(elevation),
                                            ctypes.sizeof(elevation), ctypes.byref(returned)):
            return False
        return bool(elevation.TokenIsElevated)
    finally:
        kernel32.CloseHandle(h)


def _netblock_rule_name():
    return f"popper-sandbox-block-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _make_python_copy():
    """为沙箱创建专用 python 副本（python.exe + 核心 DLL），便于防火墙按程序路径
    只阻断沙箱解释器而不影响控制器。返回 (副本目录, 解释器环境覆盖)。

    venv 里的 python.exe 只是靠旁边 pyvenv.cfg 找真解释器的重定向壳：直接拷到临时
    目录既找不到配置也找不到标准库（实测退出码 106）。所以副本必须以 base 安装为源、
    PYTHONHOME 指向 base；再把 venv 的 site-packages 挂进 PYTHONPATH，否则沙箱 worker
    会失去 venv 里装的第三方库（sklearn / numpy 等），断网路径就变成不可用路径。
    """
    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix)).resolve()
    venv_prefix = Path(sys.prefix).resolve()
    source = base_prefix if (base_prefix / "python.exe").is_file() else venv_prefix
    copy_dir = Path(tempfile.gettempdir()) / f"popper-pycopy-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    copy_dir.mkdir(parents=True, exist_ok=True)
    dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    for name in (dll, "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "python.exe"):
        src = source / name
        if src.exists():
            shutil.copy2(src, copy_dir / name)
    overrides = {"PYTHONHOME": str(source)}
    if venv_prefix != source:
        site_packages = venv_prefix / "Lib" / "site-packages"
        if site_packages.is_dir():
            overrides["PYTHONPATH"] = str(site_packages)
    return copy_dir, overrides


def _netsh(command):
    return subprocess.run(["netsh"], input=command + "\r\n", capture_output=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          creationflags=NO_WINDOW)


def _apply_netblock(rule_name, python_exe):
    cmd = (f'advfirewall firewall add rule name="{rule_name}" dir=out '
           f'program="{python_exe}" action=block')
    proc = _netsh(cmd)
    if proc.returncode != 0 or "Ok." not in (proc.stdout or ""):
        raise OSError(f"netsh 添加出站阻断规则失败({proc.returncode}): {proc.stdout}{proc.stderr}")


def _remove_netblock(rule_name):
    try:
        _netsh(f'advfirewall firewall delete rule name="{rule_name}"')
    except Exception:
        pass


def label_write_scope(path):
    """把写作用域目录标记为 Low-IL 可写（仅目录本身，不递归）。"""
    if os.name != "nt":
        raise OSError("本函数只实现 Windows 低完整性写作用域"
                      "（Linux 由 bubblewrap mount namespace 提供）")
    subprocess.run(["icacls", str(path), "/setintegritylevel", "Low"],
                   capture_output=True, check=True, creationflags=NO_WINDOW)


def unlabel_write_scope(path):
    """把写作用域目录恢复为 Medium 完整性标记（尽力而为）。"""
    if os.name != "nt":
        raise OSError("本函数只实现 Windows 低完整性写作用域"
                      "（Linux 由 bubblewrap mount namespace 提供）")
    subprocess.run(["icacls", str(path), "/setintegritylevel", "Medium"],
                   capture_output=True, check=False, creationflags=NO_WINDOW)


def seal_read(path):
    """禁止**更低完整性**的主体读 path（内核强制，不需要管理员/额外账户）。

    沙箱候选进程以 Low 完整性运行，而它是 Medium 对象；默认 MIC 只拦写不拦读，所以
    「读隔离」必须靠标签掩码里的 NO_READ_UP：低完整性主体读它会直接得到 EACCES，
    而用户本人与控制器（同为 Medium）不受影响。本实现只走 Windows 强制完整性标签；
    Linux 上的等价能力由 bubblewrap mount namespace 遮蔽提供（机制不同，候选读到的是
    空内容而不是 EACCES）。
    """
    if os.name != "nt":
        raise OSError("本函数只实现 Windows 强制完整性标签禁读"
                      "（Linux 请用 popper.sandbox.seal_read 分派到 bubblewrap 后端）")
    _set_label_policy(path, _MEDIUM_SID,
                      _MANDATORY_LABEL_NO_WRITE_UP | _MANDATORY_LABEL_NO_READ_UP)


def unseal_read(path):
    """恢复默认强制标签（Medium + 仅禁写上探），即解除 seal_read 的限制。"""
    if os.name != "nt":
        raise OSError("本函数只实现 Windows 强制完整性标签禁读"
                      "（Linux 请走 popper.sandbox.unseal_read 分派到 bubblewrap 后端）")
    _set_label_policy(path, _MEDIUM_SID, _MANDATORY_LABEL_NO_WRITE_UP)


def seal_read_available():
    """本平台能否对对象加「禁止低完整性主体读取」的强制标签（结果缓存）。"""
    global _SEAL
    if _SEAL is None:
        _SEAL = False
        if os.name == "nt":
            handle, name = tempfile.mkstemp(prefix="popper-seal-probe-")
            os.close(handle)
            try:
                seal_read(name)
                unseal_read(name)
                _SEAL = True
            except OSError:
                _SEAL = False
            finally:
                try:
                    os.remove(name)
                except OSError:
                    pass
    return _SEAL


def launch_low_integrity(command, cwd, env, stdout, stderr, timeout_seconds,
                         mem_limit_mb=None, cpu_time_seconds=None, max_processes=None,
                         on_spawn=None):
    """沙箱路径：引导进程先自降完整性，再运行真实入口；入组后由 Job 兜底杀树。"""
    if os.name != "nt":
        raise OSError("低完整性后端只实现于 Windows（沙箱路径另有 Linux bubblewrap 后端）")
    env = proxy_block_env(env)
    env = _command_interception_env(env)
    want_limits = bool(mem_limit_mb or cpu_time_seconds or max_processes)
    if want_limits and not job_limits_available():
        raise OSError("Job Object 资源限额在当前系统不可用")
    job = _create_limited_job(mem_limit_mb, cpu_time_seconds, max_processes) if want_limits else None
    rule_name, python_copy = None, None
    try:
        # 内核级断网：提权时用专用 python 副本 + WFP 出站阻断；非提权走代理拦截。
        if netblock_available():
            python_copy, home = _make_python_copy()
            env = dict(env)
            for key, value in home.items():
                if key == "PYTHONPATH" and env.get("PYTHONPATH"):
                    env["PYTHONPATH"] = value + os.pathsep + env["PYTHONPATH"]
                else:
                    env[key] = value
            interpreter = str(python_copy / "python.exe")
            rule_name = _netblock_rule_name()
            _apply_netblock(rule_name, interpreter)
        else:
            interpreter = sys.executable
        bootstrap = [interpreter, "-c", BOOTSTRAP] + [str(arg) for arg in command[1:]]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
        if job:
            # 有 Job 时必须先入组再放行：否则子进程可在 Popen 返回前创建逃逸的后代。
            flags |= _CREATE_SUSPENDED
        proc = subprocess.Popen(bootstrap, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, creationflags=flags)
        if job:
            try:
                _assign_to_job(job, proc.pid)
                _resume_process(proc.pid)
            except Exception:
                force_kill_tree(proc.pid)  # 挂起态无法自行退出，必须显式清理
                raise
        if on_spawn is not None:
            on_spawn(proc)
        reap(command, proc, timeout_seconds, lambda pid: _kill_tree_or_job(job, pid))
        return proc
    finally:
        if rule_name:
            _remove_netblock(rule_name)
        if python_copy:
            shutil.rmtree(python_copy, ignore_errors=True)
        if job:
            kernel32.CloseHandle(job)  # KILL_ON_JOB_CLOSE 兜底终止整个作业树
        _cleanup_interception_env(env)


def _kill_tree_or_job(job, pid):
    if job is not None:
        kernel32.TerminateJobObject(job, 1)
        return
    force_kill_tree(pid)


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


def _network_block_env(env):
    """已上提为平台中立的 `base.proxy_block_env`（Linux bubblewrap 也复用）；此处保留
    旧名作为薄转发，避免遗漏调用点。"""
    return proxy_block_env(env)


# 能力清单（popper experiment isolation）里平台相关条目的措辞由后端自己提供，
# 避免其它平台印出只有 Windows 才成立的机制描述。
CAPABILITY_NOTES = {
    "os_sandbox_filesystem_scope":
        "Windows 低完整性文件写作用域：候选进程仅可写运行工作区（icacls /setintegritylevel "
        "Low），其余目录只读；读取除保留集禁读外不受限，未断网时网络仍可达",
    "network_default_off":
        "内核级断网（管理员门控）：提权时经 WFP/netsh 对专用 python 副本做程序级出站阻断；"
        "非提权环境不可用（保持仅代理拦截）",
    "http_proxy_interception":
        "尽力而为：候选进程环境变量代理指向死端口，拦截遵循代理的 HTTP(S) 客户端"
        "（urllib/requests/pip 等）；裸 socket 与 DNS 不受限",
    "low_privilege_process":
        "候选进程降级为 Low Integrity 后令牌被锁（无法再调整特权，实证），并尽力剥离危险"
        "特权（SeDebug/SeShutdown 等）；保留 SeChangeNotify",
    "command_interception":
        "Python 层命令拦截（sitecustomize 注入）：高危命令（rm/curl/powershell/cmd 等）直接"
        "拒绝；可用 ctypes/CreateProcess 等原生 API 绕过，非内核级",
    "resource_limits":
        "Job Object 资源限额（内存/进程 CPU 时间/活跃进程数，内核强制，实证有效）；"
        "Job 不可用时直接报错，不静默丢弃声明的约束",
    "process_tree_termination":
        "沙箱路径用 Job Object 的 TerminateJobObject / KILL_ON_JOB_CLOSE 内核级终止整棵树；"
        "Job 不可用时降级为 taskkill /T（尽力而为，孙进程可能逃逸），故不是硬编码 True",
    "heldout_sealed_channel":
        "Windows 强制完整性内核级禁读：消费测试集期间对已注册的测试数据集文件加 NO_READ_UP "
        "标签，降级为 Low IL 的候选进程读它在内核层被拒（EACCES），控制器与用户（同级或更高"
        "完整性）不受影响，窗口结束即恢复。覆盖范围仅该文件本身：同账户下候选仍可读其它 "
        "Medium 文件；标签不可用时 --sandbox 对测试划分直接报错，不静默降级。账户级/主机级"
        "隔离仍需外部评估服务",
}


class WindowsLowIntegrityBackend:
    """``SandboxBackend`` 的 Windows 低完整性实现。

    只是一层薄适配：实现留在模块级函数里，便于 ``popper experiment isolation`` 与测试直接引用
    单个能力探针，而不必先构造后端对象。
    """

    name = "win_lowil"

    def available(self):
        return available()

    def limits_available(self):
        return job_limits_available()

    def tree_termination_available(self):
        return tree_termination_available()

    def network_block_available(self):
        return netblock_available()

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
        return launch_low_integrity(command, cwd, env, stdout, stderr, timeout_seconds,
                                    mem_limit_mb=mem_limit_mb,
                                    cpu_time_seconds=cpu_time_seconds,
                                    max_processes=max_processes, on_spawn=on_spawn)

    def capability_notes(self):
        return CAPABILITY_NOTES


BACKEND = WindowsLowIntegrityBackend()
