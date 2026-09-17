# C7 · Linux bubblewrap 沙箱后端

## Context

`popper/sandbox/__init__.py` 的注册表目前只有 `win_lowil.BACKEND` 一项。`base.SandboxBackend` 协议注释明确写道「计划中的后端是 macOS seatbelt 与 Linux bubblewrap（尚未实现，故未注册）」。后果：
- 非 Windows 上 `selected_backend()` 返回 `None` → `--sandbox` 显式报错指向 `--trusted-local`
- `.github/workflows/ci.yml` 的 Linux job 标 `continue-on-error: true`（信息性，不阻断合并）
- 跨平台交付承诺（§0.1 M2）只完成 Windows 半边

C7 落地一个 Linux bubblewrap 后端，让 `selected_backend()` 在 Linux + bwrap 可用时返回该后端，CI Linux 升为硬门禁。

## 设计要点

### 边界（与 Windows 后端对齐）
| 能力 | Windows Low IL | Linux bubblewrap |
|---|---|---|
| 写作用域 | `icacls /setintegritylevel Low` | bwrap `--bind <scope> <scope>`（rw）|
| 读隔离（holdout） | 强制标签 ACE NO_READ_UP → PermissionError | mount namespace 不绑定 / 绑 /dev/null → FileNotFoundError |
| 断网 | netsh WFP（需提权）/ 代理拦截 | `--unshare-net` 内核级（空 netns）|
| 资源限额 | Job Object（JobMemoryLimit / PerProcessUserTimeLimit / ActiveProcessLimit）| POSIX `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_NPROC`，子进程 preexec 设置 |
| 整树终止 | Job Object KILL_ON_JOB_CLOSE | POSIX 进程组 SIGKILL（bwrap 作 PID 1 in namespace，杀 pg = 杀整树）|
| 命令拦截 | sitecustomize.py Python 层 | 同 sitecustomize.py（Python 层，可绕过，如实标注）|

### 平台差异的诚实标注
- `seal_read(path)` 在 Linux 后端是 **per-launch 的状态记录**，不是内核级 toggle：把 path 加入进程级 `_SEALED_PATHS` 集合，下次 `launch` 时为该 path 追加 `--ro-bind /dev/null <path>` 让候选读到 EOF（空内容）。
- `label_write_scope(path)` / `unlabel_write_scope(path)` 是 no-op（仅记录 scope 路径，供 launch 构造 `--bind`）。
- **测试语义更新**：Windows 候选读保留集得 `PermissionError`；Linux 候选读保留集得空字符串（`/dev/null` 屏蔽）。两者都是「内核强制、候选无法读出真实数据」。测试拆 Windows/Linux 路径分别断言对应错误名，文档与代码注释如实标注该差异。
- `available()` 探活：`bwrap --version` 退出码 0 + `os.name == "posix"` 且 `sys.platform.startswith("linux")`。
- `limits_available()` = True（POSIX RLIMIT 在 Linux 内核强制）。
- `tree_termination_available()` = True（POSIX 进程组 + bubblewrap PID 1 namespace）。
- `network_block_available()` = True（`--unshare-net` 内核级）。
- `read_seal_available()` = True（mount namespace per-launch 屏蔽）。

## 实施步骤

### 1. 新建 `popper/sandbox/linux_bwrap.py`

参考 `popper/sandbox/win_lowil.py` 的薄适配结构（模块级函数 + 末尾 `BACKEND = LinuxBubblewrapBackend()`）。核心实现：

- `available()` / `bwrap_available()`：探活缓存，`bwrap --version` 退出码 0。
- `label_write_scope(path)` / `unlabel_write_scope(path)`：no-op（仅记日志或写入 `_SCOPE_PATHS` 集合，供 launch 用）。
- `seal_read(path)` / `unseal_read(path)`：把 path 加入/移出进程级 `_SEALED_PATHS` 集合。
- `launch_bwrap(command, cwd, env, stdout, stderr, timeout_seconds, *, mem_limit_mb, cpu_time_seconds, max_processes, on_spawn)`：
  1. 构造 bwrap argv：`--unshare-all`（新 mount/pid/ipc/uts/net/user namespace）+ `--ro-bind / /`（host root 只读）+ `--bind <scope> <scope>`（运行工作区 rw）+ `--dev /dev` + `--proc /proc` + `--tmpfs /tmp`
  2. 对 `_SEALED_PATHS` 中每个 path：追加 `--ro-bind /dev/null <path>` 屏蔽（候选读到 EOF / 空字符串）。这是 mount namespace 强制，候选无法绕过读到真实数据。
  3. 资源限额：用 bwrap 内部 bootstrap 脚本设置 rlimit（与 Windows BOOTSTRAP 同思路），`resource.setrlimit(RLIMIT_AS, ...)` / `RLIMIT_CPU` / `RLIMIT_NPROC`。bwrap 的 `--` 后是真实命令；preexec 作用在 Popen 直接启动的 bwrap 进程上，不影响沙箱内 python，所以限额必须在 bootstrap 里设置。
  4. 整树终止：`start_new_session=True` 创建新会话，`force_kill_tree` 用 `os.killpg(SIGKILL)`。
  5. 命令拦截：复用 `win_lowil.SITECUSTOMIZE`（与平台无关，纯 Python 层）。
- 末尾 `LinuxBubblewrapBackend` 类（与 `WindowsLowIntegrityBackend` 镜像结构）+ `BACKEND = LinuxBubblewrapBackend()`。

### 2. 注册到 `popper/sandbox/__init__.py`

- `_BACKENDS` 元组追加 `linux_bwrap.BACKEND`（无条件加入；`available()` 探活决定是否被 `selected_backend()` 选中）。
- `selected_backend()` 不变（已有逻辑：按顺序找第一个 `available()` 为真的后端）。

### 3. 更新 `tests/test_sandbox.py`

- `SandboxAvailabilityTests.test_available_matches_platform`：改成 `assertEqual(sandbox.available(), os.name == "nt" or (os.name == "posix" and sys.platform.startswith("linux") and _bwrap_installed()))`。或更简单：`assertTrue(isinstance(sandbox.available(), bool))` + 平台条件分支。
- `SandboxAvailabilityTests.test_write_scope_label_rejects_non_windows`：拆成 `test_write_scope_label_rejects_unsupported_platform`，跳过 Windows 和 Linux（这两个有后端），其余平台断言抛 OSError。
- `SandboxBackendContractTests.test_registry_entries_satisfy_the_protocol`：加入 `linux_bwrap.BACKEND` 断言（条件：Linux）。
- `SandboxBackendContractTests.test_selected_backend_matches_availability`：Windows 断言 `win_lowil`，Linux（bwrap 装时）断言 `linux_bwrap`，其余 None。
- `ReadSealAvailabilityTests.test_seal_read_available_matches_platform`：改成 True on Windows OR Linux-with-bwrap。
- `ReadSealAvailabilityTests.test_seal_read_rejects_non_windows`：跳过 Windows 和 Linux，其余平台断言抛 OSError。
- `ReadSealEnforcementTests`：保持 Windows 专属（`@unittest.skipUnless(os.name == "nt", ...)`），新增 `LinuxBubblewrapReadSealEnforcementTests`（`@skipUnless(os.name == "posix" and sys.platform.startswith("linux") and sandbox.available(), ...)`）断言候选读 sealed 文件得空字符串（`res.get("read", "") == ""`，即「读不到真实数据」）。

### 4. 更新 `.github/workflows/ci.yml`

- Linux job 去掉 `continue-on-error: true`。
- 保持只在 Linux runner 上跑 bubblewrap 后端相关测试。

### 5. 更新 `docs/技术方案.md` §2.2

把「macOS seatbelt 与 Linux bubblewrap（尚未实现，故未注册）」改成「Linux bubblewrap 已注册；macOS seatbelt 尚未实现」。同步更新 §0.1 M2 中「OS 沙箱（Windows 低完整性 + 跨平台后端）」表述。

### 6. 更新 `docs/实施记录.md`

顶部追加 C7 章节，按 0917 其它章节同样格式（背景 / 设计要点 / 文件变更 / 测试 / 边界）。

## 关键文件

- 新建：`popper/sandbox/linux_bwrap.py`（参考 `popper/sandbox/win_lowil.py` 结构）
- 改：`popper/sandbox/__init__.py`（注册表加一项）
- 改：`tests/test_sandbox.py`（平台条件断言 + Linux 路径专属测试）
- 改：`.github/workflows/ci.yml`（Linux 升硬门禁）
- 改：`docs/技术方案.md` §0.1 + §2.2（表述更新）
- 改：`docs/实施记录.md`（追加 C7 章节）

## 复用的已有部件

- `popper/sandbox/base.py::SandboxBackend` 协议、`reap` / `force_kill_tree` / `launch_process_group` / `NO_WINDOW`
- `popper/sandbox/win_lowil.py::SITECUSTOMIZE`（命令拦截 Python 层，平台无关）
- `popper/sandbox/__init__.py::selected_backend` / `sealed_reads`（注册与读封窗口逻辑不变）

## 验证

1. 单测：`python -m pytest tests/test_sandbox.py -v`（在 Linux CI 上跑全部通过；Windows 上跑 Windows 子集通过）
2. 全回归：`python -m pytest`（≥586 passed / 1 skipped 不退化）
3. 端到端：在 Linux CI runner 上跑 `python -m popper experiment --sandbox ...` 走完一个 quadratic 例子，确认 holdout seal 在 test split 上生效（候选读 test.json 失败）
4. CI Linux job 不再是 `continue-on-error`，绿即过

## 复核修订（C7.1，2026-09-17）

本计划落地后做了一轮代码级复核：Linux 路径当时**从未真实执行过**（开发机 Windows，新增用例全 skip），上述步骤 1 的多处设计在真实环境不成立。逐项修订见 `docs/实施记录.md` 的「C7.1」章节，与本文的差异集中在：

- 步骤 1 的 `available()`：**不用** `bwrap --version`。Ubuntu 23.10+/24.04+ 的 AppArmor 会在 bwrap 已装、`--version` 返回 0 的情况下仍拒绝非特权用户命名空间；改为按 launch 同形状实跑一次最小沙箱探活，失败缓存原因并在报错里给出修复指引。
- 步骤 1 第 2 条的 `--ro-bind /dev/null <path>`：只追加遮蔽不够，需先只读挂载其**父目录**（只读根上造不出文件挂载点），且遮蔽必须叠在 scope `--bind` 之上；`seal_read` 对不存在路径显式失败。
- 步骤 1 第 1 条的 argv：补 `--die-with-parent`；shim 目录（命令拦截）必须**在 `--tmpfs /tmp` 之后**重新挂回，否则被空 /tmp 遮蔽、拦截静默失效；所有挂载项绝对路径化。
- 步骤 1 第 3 条的 `RLIMIT_NPROC`：按真实 UID 计数而非按进程树，声明值要换算成「当前本 UID 进程数 + 声明值」的天花板，否则候选起不了子进程；能力清单如实标注它与 Job Object 的差别。
- 步骤 3/4 之外新增：能力清单措辞按后端下发（`capability_notes()`）、回执后端名实算、CI 显式装 bwrap + AppArmor `userns` 例外 + 探活步骤 + `POPPER_REQUIRE_SANDBOX=1` 让不可用变红而非 skip。
- 验证项 3（Linux 上跑通一整个 `--sandbox` 端到端实验）**仍未完成**：本轮只补上纯逻辑测试与 CI 探活/强制沙箱用例，端到端需在有 bwrap 的 Linux 环境实测后再记账，不预先当作已交付。
