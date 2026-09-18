"""C7 验证项 3：在一个真实平台上跑通**一整个** `--sandbox` 端到端实验。

为什么要单独一个脚本（而不靠单测）：单测只能在「单个断言」这一层证明沙箱后端能用，
它证明不了从 `init` 到 `confirm` 的完整生命周期里，一个真实候选进程**始终**在被约束
的状态下执行——而 §0.1 M2 与 README 承诺的正是这条路径。CI 以前只跑单测，所以这条
承诺一直挂在「明确未完成」里。

证据结构（三条彼此独立，任何一条单独成立都不算通过）：

1. **候选自己的现场自述**：一段见证代码在 `experiment init` **之前**注入到候选
   `model.py`，因此它属于已注册代码（哈希被 init 记账、执行前后各校验一次），不存在
   「跑完再改代码伪造证据」的空间。它把每次执行的观察打到 stderr——stderr 落在运行
   工作区内，并进入该 run 的产物 manifest ⇒ 见证本身在证据链里。
2. **宿主侧独立复核**：作用域外的两个 canary 路径由本脚本在宿主上直接查存在性，
   不信候选的自述（候选说谎也躲不过这一步）。
3. **科学正确性**：三种显式单步法的观测收敛阶是数学上已知的（euler≈1、midpoint≈2、
   rk4≈4）。断言实测值落在真值附近，用来排除「沙箱把执行挡坏了却仍然绿」这类假通过
   ——只要求「跑完不报错」的端到端，是可以全程什么都不做也绿的。

断言分两档，避免拿平台差异造假红：
- **两平台都主张的**（写作用域、命令拦截、保留集封读、trust 字段、证据链）一律硬断言；
- **只有某平台主张的**（空 netns 断网、namespace 内 pid、沙箱内可观察的 rlimit）只在
  `popper.sandbox` 的能力探针真的主张它时才断言。探针说「不主张」时，观察值仍然记录，
  但不参与判红（能力清单说自己做不到，端到端却因为一个平台差异而红，是假信号）。

输出通道沿用 `ci_failure_summary.py` 的教训：annotation 与 job summary 彼此独立，
红的时候必发 `::error::`，绿的时候发一条 `::notice::` 摘要（Actions 日志需登录，
annotation 常是免登录时唯一读得到的东西）。所有文件读写显式 UTF-8。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "ode-convergence"
WITNESS_PREFIX = "POPPER-E2E-WITNESS"
# 每种显式单步法的**已知**观测收敛阶（欧拉 1 阶、中点 2 阶、RK4 4 阶）。
EXPECTED_ORDER = {"euler": 1.0, "midpoint": 2.0, "rk4": 4.0}
ORDER_TOLERANCE = 0.25
SANDBOX_TRUST = "controller_scored_os_sandbox"
# 见证必须出现的观察行。少任何一行都算门禁红：那意味着候选根本没跑到见证代码。
WITNESS_KEYS = ("pid", "scope_write", "escape_write_project", "escape_write_outside",
                "holdout_read", "command_block", "net_raw", "rlimit_cpu", "shim_visible")
# Linux 上 bwrap 的 pid namespace 里，候选看到的 pid 必然是个位数；宿主上则是四位数。
NAMESPACE_PID_CEILING = 10
MAX_ANNOTATIONS = 8  # 与 ci_failure_summary 同一预算：GitHub 每 check run 只留最近 10 条
MAX_FAILURES_IN_SUMMARY = 40
# 见证对「保留集可读否」给出的取值前缀（两平台的强制形状不同，语义相同：读不出真值）。
# 注意：注入的见证代码跑在**候选进程**里，拿不到本模块的常量，所以它只能用字面量。
BLOCKED_PREFIX = "BLOCKED"

# 注入到候选代码末尾的见证段。它是**产品执行路径下的候选代码**，不是本脚本的一部分，
# 因此必须自包含（只用标准库）且异常安全：它坏了由门禁按「缺观察行」判红，而不是它自己
# 把 run 弄崩成看不懂的形状。
WITNESS_SNIPPET = r'''

# ============ Popper 端到端沙箱见证（由 .github/scripts/e2e_sandbox_experiment.py 注入）====
def _popper_e2e_witness():
    import json
    import os
    import socket
    import subprocess
    import sys
    from pathlib import Path

    def line(key, value):
        print("%s %s=%s" % ("POPPER-E2E-WITNESS", key, value), file=sys.stderr, flush=True)

    def probe(name, action):
        try:
            action()
            line(name, "GRANTED")
        except Exception as error:
            line(name, "DENIED:" + type(error).__name__)

    here = Path(__file__).resolve()
    run_dir = here.parents[1]
    project = None
    for cand in here.parents:
        if (cand / "experiment.json").is_file():
            project = cand
            break
    if project is None:
        line("witness", "NO_PROJECT_ROOT")
        return
    line("pid", str(os.getpid()))
    shim = os.environ.get("POPPER_SANDBOX_SHIMDIR")
    if not shim:
        line("shim_visible", "ABSENT")
    elif os.path.isfile(os.path.join(shim, "sitecustomize.py")):
        line("shim_visible", "GRANTED")
    else:
        line("shim_visible", "HIDDEN")

    def write_at(path):
        path.write_text("popper-e2e-canary", encoding="utf-8")

    # 正向对照：作用域内必须写得动。缺了它，下面两条 DENIED 可能只是「什么都写不了」
    # 造成的假通过（Windows 上误标整盘只读、Linux 上漏挂 --bind 都会这样）。
    probe("scope_write", lambda: write_at(run_dir / "e2e-scope-write.txt"))
    probe("escape_write_project", lambda: write_at(project / "e2e-escape-project.txt"))
    probe("escape_write_outside", lambda: write_at(project.parent / "e2e-escape-outside.txt"))

    def read_holdout():
        raw = (project / "test.json").read_text(encoding="utf-8")
        if not raw.strip():
            return "BLOCKED_EMPTY"  # Linux：/dev/null 屏蔽 ⇒ 读到空内容
        rows = json.loads(raw)
        if any(str(row.get("id", "")).startswith("test-") for row in rows):
            return "READABLE"  # 读到了真值：封读根本没生效
        return "READABLE_PARTIAL"

    try:
        holdout = read_holdout()
    except Exception as error:
        # Windows：强制完整性标签 NO_READ_UP → PermissionError（EACCES）
        holdout = "BLOCKED:" + type(error).__name__
    line("holdout_read", holdout)
    probe("command_block", lambda: subprocess.run(
        ["curl", "-s", "http://popper-e2e.invalid"], capture_output=True, timeout=5))
    probe("net_raw", lambda: socket.create_connection(("198.51.100.1", 80), timeout=3).close())
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_CPU)[0]
        line("rlimit_cpu", "NA" if soft in (-1, resource.RLIM_INFINITY) else str(soft))
    except Exception as error:
        line("rlimit_cpu", "NA:" + type(error).__name__)  # Windows 无 resource 模块


try:
    _popper_e2e_witness()
except Exception as _witness_error:  # 见证自身坏了：报一行，门禁按「缺观察行」判红
    print("POPPER-E2E-WITNESS self_error=%s" % type(_witness_error).__name__,
          file=sys.stderr, flush=True)
'''


# --------------------------------------------------------------------------- 构建 / 执行
def build_project(workspace: Path) -> Path:
    """把示例工程复制到工作区，并在 `init` 之前注入见证代码。

    复制出来跑（而不是就地跑仓库里的 examples/）有两个理由：① 不在工作树里留 `.popper/`
    与 `__pycache__`；② 注入见证代码等于修改已注册候选，绝不能污染仓库里那份工程。
    """
    project = workspace / "proj"
    if project.exists():
        shutil.rmtree(project)
    shutil.copytree(EXAMPLE, project)
    model = project / "model.py"
    original = model.read_text(encoding="utf-8")
    model.write_text(original + WITNESS_SNIPPET, encoding="utf-8")
    return project


def run_cli(step: str, args: list) -> dict:
    """跑一条 popper CLI 并解析其 JSON 输出。

    刻意走 CLI 而不是 `import popper` 直接调：端到端要覆盖的是**用户实际输入的那条命令**
    （含参数解析、退出码、stdout 契约）。stdout 用管道取字节再自己解码，不经 PowerShell。
    """
    command = [sys.executable, "-X", "utf8", "-m", "popper", *args]
    proc = subprocess.run(command, cwd=str(REPO), capture_output=True, timeout=1800)
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"步骤「{step}」退出码 {proc.returncode}\n"
                           f"stdout: {stdout[-1500:]}\nstderr: {stderr[-1500:]}")
    try:
        return {"step": step, "payload": json.loads(stdout)}
    except ValueError as error:
        raise RuntimeError(f"步骤「{step}」的输出不是 JSON: {error}\n{stdout[:800]}") from error


# --------------------------------------------------------------------------- 取证
def parse_witness(text: str) -> dict:
    """把一份 stderr 日志里的见证行抽成 {key: value}。"""
    observed = {}
    for raw in text.splitlines():
        if not raw.startswith(WITNESS_PREFIX):
            continue
        body = raw[len(WITNESS_PREFIX):].strip()
        key, _, value = body.partition("=")
        if key:
            observed[key.strip()] = value.strip()
    return observed


def collect_runs(project: Path) -> list:
    """读 `.popper/runs/*`：每 run 的 results.json + 每次执行的 stderr 见证。"""
    runs = []
    runs_root = project / ".popper" / "runs"
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        results = run_dir / "results.json"
        if not results.is_file():
            runs.append({"id": run_dir.name, "missing_results": True, "logs": {}})
            continue
        payload = json.loads(results.read_text(encoding="utf-8"))
        logs = {}
        for log in sorted(run_dir.glob("stderr-*.log")):
            logs[log.stem] = parse_witness(log.read_text(encoding="utf-8", errors="replace"))
        runs.append({"id": run_dir.name, "payload": payload, "logs": logs})
    return runs


def host_canaries(workspace: Path) -> list:
    """宿主侧独立复核：作用域外的两处 canary 是否真被写出来了。

    不信候选的自述：见证行说 DENIED 而磁盘上却真有文件，就是自述不可信的形状。
    """
    return sorted(str(path) for pattern in ("e2e-escape-*.txt", "*/e2e-escape-*.txt")
                  for path in workspace.glob(pattern))


# --------------------------------------------------------------------------- 门禁
def is_denied(value: str) -> bool:
    return bool(value) and value.startswith("DENIED")


def blocked_by_interception(value: str) -> bool:
    """高危命令是否被**拦截层**本身挡下（而不是碰巧跑不起来）。

    为什么不能只看 `DENIED`：sitecustomize 的 `_check` 在 OS 查找二进制**之前**就抛
    `PermissionError`，所以 `DENIED:FileNotFoundError`（ubuntu runner 上没装 curl 时的形状）
    恰恰意味着拦截没生效——拿它当「已拦截」是一个只会在这条 job 该抓的东西上变绿的假绿。
    """
    return value == "DENIED:PermissionError"


def gate(context: dict) -> list:
    """返回 [(标签, 明细)] 失败清单；空表即通过。纯函数，可被单测直接喂假输入。"""
    failures = []
    runs = context["runs"]
    claims = context.get("platform_claims", {})

    if context.get("backend") is None:
        failures.append(("no-backend", "本平台没有可用沙箱后端，端到端无法证明任何隔离主张"))
    expect_backend = context.get("expect_backend")
    if expect_backend and context.get("backend") != expect_backend:
        failures.append(("backend-mismatch",
                         f"实际后端 {context.get('backend')!r} != 期望 {expect_backend!r}"))

    # 1) 生命周期：阶段、结论、选中候选
    status = context.get("status") or {}
    if status.get("phase") != "completed":
        failures.append(("phase", f"phase={status.get('phase')!r}，端到端没走完"))
    claim = status.get("claim") or {}
    if claim.get("status") != "supports_threshold":
        failures.append(("claim", f"claim.status={claim.get('status')!r}"))
    selected = (status.get("selected") or {}).get("scheme")
    if selected != "rk4":
        failures.append(("selected", f"selected={selected!r}，期望 rk4"))

    # 2) run 集合：dev 3 个配置 + test 2 个配置 = 5 次 run，每次 3 个重复
    dev = [r for r in runs if (r.get("payload") or {}).get("split") == "dev"]
    test = [r for r in runs if (r.get("payload") or {}).get("split") == "test"]
    if len(runs) != 5 or len(dev) != 3 or len(test) != 2:
        failures.append(("run-shape", f"runs={len(runs)} dev={len(dev)} test={len(test)}，"
                                      "期望 5=3+2（基线 + 2 候选在 dev，基线 + 选中候选在 test）"))
    for run in runs:
        payload = run.get("payload") or {}
        rid = run["id"]
        if run.get("missing_results"):
            failures.append(("missing-results", f"{rid} 没有 results.json"))
            continue
        if payload.get("trust") != SANDBOX_TRUST:
            failures.append(("trust", f"{rid} trust={payload.get('trust')!r}，"
                                      "沙箱路径被静默降级为 trusted-local"))
        if payload.get("n_seeds") != 3:
            failures.append(("n_seeds", f"{rid} n_seeds={payload.get('n_seeds')!r}"))
        if not run["logs"]:
            failures.append(("no-witness-log", f"{rid} 没有任何 stderr-*.log 见证"))
        # 科学正确性：观测收敛阶必须落在已知真值附近
        scheme = (payload.get("config") or {}).get("scheme")
        expected = EXPECTED_ORDER.get(scheme)
        if expected is None:
            failures.append(("unknown-scheme", f"{rid} config.scheme={scheme!r}"))
        elif abs(float(payload.get("mean", 0.0)) - expected) > ORDER_TOLERANCE:
            failures.append(("metric-truth",
                             f"{rid} {scheme} 实测 {payload.get('mean')}，已知真值 {expected}"
                             "（沙箱把执行挡坏了，或候选根本没跑）"))

    # 3) 每次执行的见证
    for run in runs:
        payload = run.get("payload") or {}
        rid = run["id"]
        for unit, seen in sorted(run["logs"].items()):
            where = f"{rid}/{unit}"
            missing = [key for key in WITNESS_KEYS if key not in seen]
            if missing:
                failures.append(("witness-incomplete", f"{where} 缺观察行 {missing}"))
            if seen.get("scope_write") != "GRANTED":
                failures.append(("scope-control", f"{where} 作用域内写 != GRANTED"
                                                  "（没有正向对照，DENIED 可能是假通过）"))
            for key in ("escape_write_project", "escape_write_outside"):
                if not is_denied(seen.get(key, "")):
                    failures.append(("escape-write", f"{where} {key}={seen.get(key)!r}，"
                                                     "作用域外可写 ⇒ 写作用域不成立"))
            if not blocked_by_interception(seen.get("command_block", "")):
                failures.append(("command-block", f"{where} 高危命令未被拦截层拒绝"
                                                  f"（command_block={seen.get('command_block')!r}；"
                                                  "只接受 DENIED:PermissionError）"))
            if seen.get("shim_visible") != "GRANTED":
                failures.append(("shim-visible", f"{where} 命令拦截 shim 在沙箱内不可见"
                                                 "（被 --tmpfs /tmp 遮住即为此形状）"))
            holdout = seen.get("holdout_read", "")
            if payload.get("split") == "test" and not holdout.startswith(BLOCKED_PREFIX):
                # 保留集封读窗口只覆盖 test 消费阶段；dev 阶段可读是已登记的边界，
                # 所以这里只在 test 判红，dev 的观察值全部进摘要表格供人看。
                failures.append(("holdout-seal", f"{where} test 划分里保留集仍可读："
                                                 f"holdout_read={holdout!r}"))
            if claims.get("namespace_pid") and seen.get("pid", "").isdigit():
                if int(seen["pid"]) > NAMESPACE_PID_CEILING:
                    failures.append(("namespace-pid", f"{where} pid={seen['pid']} 不像新 pid "
                                                      "namespace 内的进程（bwrap 未真的参与？）"))
            if claims.get("observable_rlimits"):
                if seen.get("rlimit_cpu") in (None, "", "NA") or seen["rlimit_cpu"].startswith("NA"):
                    failures.append(("rlimit-claim", f"{where} 声称内核强制 rlimit，却在沙箱内读不到"))
                elif int(seen["rlimit_cpu"]) > 3600:
                    failures.append(("rlimit-claim", f"{where} RLIMIT_CPU={seen['rlimit_cpu']} 未收紧"))
            if claims.get("kernel_net") and not is_denied(seen.get("net_raw", "")):
                failures.append(("net-block", f"{where} 能力清单主张空 netns 断网，实测可连"))

    # 4) 宿主侧独立复核
    if context.get("canaries"):
        failures.append(("canary-on-host", f"作用域外发现候选写出的文件：{context['canaries']}"))

    # 5) 证据链与能力清单
    replay = context.get("replay") or {}
    recomputed_expected = len([r for r in runs if not r.get("missing_results")])
    if replay.get("status") != "verified":
        failures.append(("replay", f"replay.status={replay.get('status')!r}"))
    if replay.get("runs_recomputed") != recomputed_expected:
        failures.append(("replay-scope", f"runs_recomputed={replay.get('runs_recomputed')!r} "
                                         f"!= 实际可重算的 run 数 {recomputed_expected}"))
    if not replay.get("claim_recomputed"):
        failures.append(("replay-claim", "结论未能从保存制品重算"))
    isolation = context.get("isolation") or {}
    if isolation.get("backend") != context.get("backend"):
        failures.append(("isolation-backend",
                         f"isolation.backend={isolation.get('backend')!r} != "
                         f"{context.get('backend')!r}"))
    report_text = context.get("report_text") or ""
    if context.get("backend") and "OS 沙箱" not in report_text:
        failures.append(("report-trust-text",
                         "report.md 的「执行模式」行没写出沙箱后端——产物口径与实际路径漂移"))
    return failures


# --------------------------------------------------------------------------- 上报
def workflow_command(level: str, message: str) -> str:
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{level}::{escaped[:300]}"


def summarize(context: dict, failures: list) -> str:
    lines = ["## C7 端到端 `--sandbox` 实验（真实平台）", "",
             f"- 平台后端：`{context.get('backend')}`（期望 `{context.get('expect_backend')}`）",
             f"- run 数：{len(context['runs'])}（dev 3 + test 2），每次执行 3 个重复",
             f"- 判定：{'**失败 ' + str(len(failures)) + ' 项**' if failures else '**通过**'}", ""]
    lines.append("| run | split | scheme | 实测阶 | 已知真值 | 作用域内写 | 越界写 | 保留集读 | 命令拦截 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for run in context["runs"]:
        payload = run.get("payload") or {}
        seen = {}
        for values in run.get("logs", {}).values():
            for key, value in values.items():
                seen.setdefault(key, value)
        scheme = (payload.get("config") or {}).get("scheme", "-")
        mean = payload.get("mean")
        escapes = [seen.get("escape_write_project"), seen.get("escape_write_outside")]
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            run["id"][:8], payload.get("split", "-"), scheme,
            f"{mean:.4f}" if isinstance(mean, float) else "-",
            EXPECTED_ORDER.get(scheme, "-"), seen.get("scope_write", "-"),
            "全 DENIED" if all(is_denied(v) for v in escapes) else " / ".join(map(str, escapes)),
            seen.get("holdout_read", "-"), seen.get("command_block", "-")))
    if failures:
        lines.extend(["", "### 失败清单", ""])
        lines.extend(f"- `{label}` · {detail}" for label, detail in failures[:MAX_FAILURES_IN_SUMMARY])
    return "\n".join(lines) + "\n"


def emit(summary: str, failures: list) -> None:
    for label, detail in failures[:MAX_ANNOTATIONS]:
        print(workflow_command("error", f"[e2e-sandbox] {label}: {detail}"))
    if failures:
        print(workflow_command("error", f"[e2e-sandbox] FAILED total={len(failures)} "
                                        f"labels={sorted({label for label, _ in failures})}"))
    else:
        print(workflow_command("notice", "[e2e-sandbox] 一整个 --sandbox 端到端实验在真实沙箱下跑通："
                                          "5 次 run × 3 重复，越界写/高危命令/保留集读全部被挡，"
                                          "观测收敛阶与已知真值一致，证据链可离线重算"))
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(summary)
        except OSError as error:
            # 摘要写不上不能让 annotation 闭嘴（两条通道彼此独立）
            print(workflow_command("warning", f"[e2e-sandbox] 无法写 job summary: {error}"))


# --------------------------------------------------------------------------- 入口
def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None,
                        help="工作区父目录（默认临时目录）；工程会复制到 <workspace>/proj")
    parser.add_argument("--expect-backend", default=None,
                        help="期望的真实后端名（windows_low_integrity / linux_bubblewrap）")
    parser.add_argument("--keep", action="store_true", help="保留工作区以便取证")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO))
    from popper import sandbox  # noqa: E402  延迟导入：本脚本也要能在未安装 popper 时给出清晰报错

    backend = sandbox.execution_backend_name()
    claims = {
        # 「沙箱内 pid 应该很小」只有在真有 pid namespace 时才成立
        "namespace_pid": backend == "linux_bubblewrap",
        "kernel_net": sandbox.netblock_available(),
        # Windows 的限额由 Job Object 强制，候选进程内没有 resource 模块可自证
        "observable_rlimits": backend == "linux_bubblewrap",
    }
    parent = args.workspace or Path(tempfile.mkdtemp(prefix="popper-e2e-workspace-"))
    if backend is None:
        # 本 job 的全部意义就是「在真沙箱下跑完」：没后端时绿的端到端是假端到端。
        # 与单测侧的 POPPER_REQUIRE_SANDBOX=1 同一取向：宁可红，不可静默降级。
        print(workflow_command("error", f"[e2e-sandbox] {sandbox.no_backend_message()}"))
        return 2
    parent.mkdir(parents=True, exist_ok=True)
    project = build_project(parent)
    context = {"backend": backend, "expect_backend": args.expect_backend,
               "platform_claims": claims, "runs": [], "canaries": [],
               "status": {}, "replay": {}, "isolation": {}, "report_text": ""}
    try:
        steps = [
            ("init", ["experiment", "init", str(project)]),
            ("search --sandbox", ["experiment", "search", str(project), "--sandbox"]),
            ("freeze", ["experiment", "freeze", str(project)]),
            ("confirm --sandbox", ["experiment", "confirm", str(project), "--sandbox"]),
            ("status", ["experiment", "status", str(project)]),
            ("report", ["experiment", "report", str(project)]),
            ("replay", ["experiment", "replay", str(project)]),
            ("isolation", ["experiment", "isolation", str(project)]),
        ]
        payloads = {}
        for step, command in steps:
            payloads[step] = run_cli(step, command)["payload"]
            print(f"[e2e-sandbox] 步骤「{step}」完成", file=sys.stderr)
        context["status"] = payloads["status"]
        context["replay"] = payloads["replay"]
        context["isolation"] = payloads["isolation"]
        report_path = payloads["report"]
        if isinstance(report_path, str):
            path = Path(report_path)
            context["report_text"] = path.read_text(encoding="utf-8") if path.is_file() else ""
        context["runs"] = collect_runs(project)
        context["canaries"] = host_canaries(parent)
        failures = gate(context)
    except RuntimeError as error:
        emit(f"## C7 端到端 `--sandbox` 实验\n\n步骤失败：\n\n```\n{error}\n```\n",
             [("step-failed", str(error)[:600])])
        print(workflow_command("error", f"[e2e-sandbox] {error}"), file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(parent, ignore_errors=True)
    summary = summarize(context, failures)
    emit(summary, failures)
    print(summary)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
