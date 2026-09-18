"""把 pytest 输出里的失败清单写进 GitHub Actions 的 job summary。

为什么需要它：Actions 的运行日志要求登录才能看（公开仓库也一样），失败时外面的人
（包括没有凭据的协作者与本仓库的作者）只能看到「Process completed with exit code 1」，
只能靠猜。job summary 渲染在 run 页面上，**人在宽屏浏览器里**免登录可见（窄版式不挂载该
面板，summary 的 fragment 端点与 step 日志都要凭据），所以把「哪些用例红了」写进摘要有用，
但它不是机器可读的那一条——那一条只能是 annotation。

只做两件事：读测试步骤落盘的 pytest 输出（`> pytest-output.txt 2>&1`），抽取失败行
（`FAILED`/`ERROR` 以及子测试的 `SUBFAILED`/`SUBERROR`）与跳过行（`SKIPPED [N] 位置: 原因`，
需要测试步骤带 `-rs`），以 UTF-8 追加到 `$GITHUB_STEP_SUMMARY`，并把关键结论同时打成
`::error::` / `::notice::` annotation。所有文件读写都显式指定 encoding，不随宿主 locale 变
（这是 verify_claims.py 刚踩过的同一个坑）。

两条通道彼此独立：摘要写失败、没有 `GITHUB_STEP_SUMMARY`、输出里没有失败行——都不能
让 annotation 闭嘴（它常常是免登录时唯一能拿到信息的那一条）。

为什么 skip 名单也要报：带原因的 skip 在外部与「真跑了」完全不可区分，所以「job 绿」
并不证明那些用例跑过（例：沙箱硬门禁 `POPPER_REQUIRE_SANDBOX=1` 的价值，恰恰取决于
真实沙箱用例到底跑了没有）。本脚本因此要在**无论成败**时被调用（工作流侧 `if: always()`），
并用 `TEST_STEP_OUTCOME` 分辨该走「绿：只报 skip」还是「红：报失败清单」——缺了这根
判据，绿跑会被「永不沉默」那条分支误报成 error。

本脚本自身**永不以非零退出**：它是失败后的诊断步骤，不该再把 job 变成第二个红点。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MAX_FAILURE_LINES = 200
# annotation 预算：GitHub 对 push 触发的 run 只保留每个 check run 「最近 10 条」
# annotation，且系统自己会占 2 条（Node 版本弃用 warning + “Process completed with
# exit code 1”）。所以我们自己最多只能占 8 条：超出部分不是被 GitHub 显示不全，而是
# 直接丢弃——拿一个会漏报的仪表做诊断，比没有仪表更危险（曾试过 40 条，实拿 10 条）。
MAX_ANNOTATIONS = 8
ANNOTATION_LINE_CHARS = 300
SIGNATURE_CHARS = 180
# pytest 短汇总里的「失败行」前缀：`unittest.subTest` 里的失败打的是 SUBFAILED/
# SUBERROR，不带 FAILED 前缀——只认后两者会把「有红」读成「没有失败行」。
FAILURE_PREFIXES = ("FAILED", "ERROR", "SUBFAILED", "SUBERROR")
FAILURE_LABEL = "FAILED/ERROR/SUBFAILED/SUBERROR"
SUB_DETAIL = re.compile(r"^SUB(?:FAILED|ERROR)\((.*)\)\s")
# `pytest -rs` 的短汇总行：`SKIPPED [次数] 位置: 原因`。位置不含空格（`a/b.py:12`
# 或 `a/b.py::K::t`），所以用 \S+ 掐到第一个「冒号+空格」为止，剩下的整段都是原因。
SKIP_LINE = re.compile(r"^SKIPPED(?: \[(\d+)\])? (\S+): (.*)$")
SKIP_LOOSE = re.compile(r"^SKIPPED(?: \[(\d+)\])? (.*)$")
DECLARED_SKIPPED = re.compile(r"(\d+) skipped")
MAX_SKIP_LINES = 200
SKIP_SIGNATURE_CHARS = 160
# skip 的 notice 要能装下全部类别：实测发现「job summary 免登录可读」只对**人在宽屏浏览器**
# 成立——窄版式（曾测到 innerWidth=694）根本不挂载 summary 面板，而 summary 的 fragment
# 端点全部 404、step 日志需 admin（run 35313943849 实拍）。所以机器能稳定读到的只有
# annotation，它就不能只给前 5 类：上限抬到 8 类，每条原因截到 SKIP_NOTICE_REASON_CHARS，
# 真装不下的部分继续由「另有 K 类 / M 次」那句明示。
MAX_SKIP_NOTICE_CLUSTERS = 8
SKIP_NOTICE_REASON_CHARS = 70
TRUNCATION_MARK = "…"

# pytest 的断言消息本身就是中文，所以 ::error:: 行会带中文：stdout 必须自己钉在 UTF-8，
# 不能靠宿主 locale（runner 把 stdout 当管道收，中文 Windows 上不钉就是 cp936）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def workflow_error(message: str) -> str:
    """拼 GitHub Actions 工作流命令；按文档预转义 % / CR / LF，否则会被截断或误解析。"""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error::{escaped}"


def workflow_warning(message: str) -> str:
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning::{escaped}"


def workflow_notice(message: str) -> str:
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::notice::{escaped}"


def failure_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(FAILURE_PREFIXES)]


def exit_status(output_path: Path) -> str:
    """测试步骤单独落盘的 pytest 退出码（拿不到则 "?"）。

    为什么需要它：输出里没 FAILED/ERROR 行却非零退出（崩在采集前、被子进程带崩、
    被 kill）与「测试真红」是两回事，而 GitHub 只会告诉我们「exit code 1」。
    """
    candidate = output_path.with_name("pytest-status.txt")
    if not candidate.is_file():
        return "?"
    return candidate.read_text(encoding="utf-8", errors="replace").strip() or "?"


def tail_lines(text: str, count: int = 4) -> list[str]:
    return [line for line in text.splitlines() if line.strip()][-count:]


def test_step_outcome() -> str:
    """测试步骤的 outcome（工作流侧传入）；空串表示调用方没给，只能按「非绿」处理。

    为何不能靠猜：本脚本现在无论成败都会跑，而「没有失败行」在绿跑里是常态、在红跑里
    是事故。没这根判据时，绿跑会被误报成 `::error::`（一个不存在的故障）。
    """
    return os.environ.get("TEST_STEP_OUTCOME", "").strip().lower()


def test_id(line: str) -> str:
    """`FAILED tests/x.py::K::t - 详情` -> `tests/x.py::K::t`。

    子测试行在「前缀 + 参数化详情」后面才跟测试名（`SUBFAILED(path='a') tests/x.py::K::t`），
    所以取最后一个空白分隔的 token；拿不到 `::` 就退回整行，至少不丢信息。
    """
    head = line.split(" - ", 1)[0]
    token = head.split()[-1]
    return token if "::" in token else head


def reason_of(line: str) -> str:
    if " - " in line:
        return line.split(" - ", 1)[1].strip()
    match = SUB_DETAIL.match(line)
    if match:                      # 子测试没有异常摘要，括号里的参数化详情就是区分线索
        return f"子测试 {match.group(1)}"
    return line


def signatures(lines: list[str], limit: int = 4) -> list[str]:
    """按异常消息聚类（保留出现顺序），返回 `Nx 消息` 形式。

    同一根因往往覆盖几十个用例（例：netsh 短名、缺少字体）；annotation 装不下逐个详情，
    但「几类根因、各多少条」能完整保住信息形状。
    """
    counts: dict[str, int] = {}
    for line in lines:
        key = reason_of(line)[:SIGNATURE_CHARS]
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [f"{count}x {reason}" for reason, count in ranked[:limit]]


def chunk_join(items: list[str], per_chunk: int) -> list[str]:
    return ["; ".join(items[i:i + per_chunk]) for i in range(0, len(items), per_chunk)]


def summary_lines(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return [line for line in lines[-3:] if "passed" in line or "failed" in line or "error" in line]


def skip_entries(text: str) -> list[tuple[int, str, str]]:
    """`SKIPPED [N] 位置: 原因` -> [(N, 位置, 原因)]；不带 `-rs` 时返回空。

    N 是 pytest 对同一（位置, 原因）的聚合次数，所以下面的总数取 sum(N) 而不是行数。
    """
    entries: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        if not line.startswith("SKIPPED"):
            continue
        match = SKIP_LINE.match(line)
        if match:
            entries.append((int(match.group(1) or 1), match.group(2), match.group(3).strip()))
            continue
        loose = SKIP_LOOSE.match(line)
        if loose:
            entries.append((int(loose.group(1) or 1), loose.group(2).strip(), ""))
    return entries


def skip_total(entries: list[tuple[int, str, str]]) -> int:
    return sum(count for count, _, _ in entries)


def declared_skips(text: str) -> int:
    """末尾汇总行里写的 `N skipped`——用来分辨「真的 0 跳过」与「测试步骤忘了 `-rs`」。"""
    matches = DECLARED_SKIPPED.findall(text)
    return int(matches[-1]) if matches else 0


def skip_clustered(entries: list[tuple[int, str, str]]) -> list[tuple[str, int]]:
    """按原因聚合（次数相加，不是行数的），保留「多→少 + 字典序」的稳定序。"""
    counts: dict[str, int] = {}
    for count, _, reason in entries:
        key = (reason or "（无原因）")[:SKIP_SIGNATURE_CHARS]
        counts[key] = counts.get(key, 0) + count
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def cut_reason(reason: str) -> str:
    """按字截断单条原因，**并留下截断标记**：静默剪短会被当成完整原因读。

    这和「前 N 类不说还差多少」是同一个缺陷的小版本——仪表的任何一处
    不完整都得自己标出来，否则下游会拿它做全量判断。
    """
    if len(reason) <= SKIP_NOTICE_REASON_CHARS:
        return reason
    return reason[:SKIP_NOTICE_REASON_CHARS] + TRUNCATION_MARK


def skip_notice(entries: list[tuple[int, str, str]], limit: int = MAX_SKIP_NOTICE_CLUSTERS) -> str:
    """单条 notice 文本：尽量装下全部原因类别 + **明确的未列出量**。

    只给前 N 类而不说「还差多少」，就是仪表自己的漏报：run 35312360291 的 ubuntu 实到
    `SKIP total=27`，当时前 4 类只能对上 24 次，剩下 3 次归不入任何一类——读的人无从知道
    名单不完整。抬到 8 类并按字截断后，实测的 ubuntu 名单（共 7 类）能全部递出；真超
    出时仍报「另 K 类 / M 次」并指向逐条名单。
    """
    total = skip_total(entries)
    ranked = skip_clustered(entries)
    shown, hidden = ranked[:limit], ranked[limit:]
    message = (f"SKIP total={total} 按原因聚合（列出 {len(shown)}/{len(ranked)} 类）: "
               + " || ".join(f"{count}x {cut_reason(reason)}" for reason, count in shown))
    if hidden:
        rest = sum(count for _, count in hidden)
        message += (f" || 另有 {len(hidden)} 类 / {rest} 次未列出"
                    f"（逐条名单在 job summary，需宽屏浏览器看）")
    return message


def skip_markdown(entries: list[tuple[int, str, str]], declared: int) -> list[str]:
    # 测试/排障时可缩小逐条清单上限，以便验证「截断必须自己说」这条行为。
    cap = MAX_SKIP_LINES
    try:
        cap = int(os.environ.get("CI_SUMMARY_MAX_SKIP_LINES") or MAX_SKIP_LINES)
    except ValueError:
        pass
    parts = ["", f"跳过（skip）条目：汇总行 `{declared} skipped`，短汇总里按位置列出 "
                f"{len(entries)} 行 / 合计 {skip_total(entries)} 次（最多显示 {cap} 行）："]
    if not entries:
        parts += ["", "短汇总里没有 `SKIPPED` 行。" + (
            "汇总行说跳过了 " + str(declared) + " 项，那就是测试步骤没带 `-rs`——skip 名单丢失。"
            if declared else "汇总行也是 0 skipped，即本 job 确实一项未跳。")]
        return parts
    parts.append("")
    parts.append("```text")
    parts += [f"SKIPPED [{count}] {location}: {reason or '（无原因）'}"
              for count, location, reason in entries[:cap]]
    if len(entries) > cap:
        parts.append(f"……另有 {len(entries) - cap} 行未列出（逐条清单被截断，"
                     f"共 {len(entries)} 行）")
    parts.append("```")
    ranked = skip_clustered(entries)
    shown = [f"{count}x {reason}" for reason, count in ranked[:8]]
    hidden = ranked[8:]
    if hidden:
        shown.append(f"另有 {len(hidden)} 类 / {sum(count for _, count in hidden)} 次未列出"
                     f"（逐条清单见上，未被截断时它就是全量）")
    parts += ["", "按原因聚合：", "", "```text", *shown, "```"]
    return parts


def build_markdown(payload: str, missing: bool, status: str = "?", green: bool = False) -> str:
    label = os.environ.get("GITHUB_JOB", "job")
    runner = os.environ.get("RUNNER_OS", "?")
    python = os.environ.get("pythonLocation") or sys.executable
    title = "CI 结果与 skip 名单" if green else "CI 失败清单"
    parts = [f"### {title} · `{label}` · {runner}", "",
             f"解释器：`{python}`  pytest 退出码：`{status}`  测试步骤结论：`{test_step_outcome() or '未知'}`", ""]
    if missing:
        parts += [
            "没有 `pytest-output.txt`——失败发生在**测试步骤之前**"
            "（安装依赖 / bubblewrap / 沙箱探活之一），需要看该步骤的日志。",
        ]
        return "\n".join(parts) + "\n"
    fails = failure_lines(payload)
    if green:
        parts.append(f"测试步骤结论为 success：失败/错误条目 {len(fails)} 项。")
    else:
        parts.append(f"失败/错误条目 {len(fails)} 项（最多显示 {MAX_FAILURE_LINES} 项）：")
        parts.append("")
        parts.append("```text")
        parts += fails[:MAX_FAILURE_LINES] or [f"（没有 {FAILURE_LABEL} 行，见末尾汇总）"]
        parts.append("```")
    tail = summary_lines(payload)
    if tail:
        parts += ["", "末尾汇总：", "", "```text", *tail, "```"]
    parts += skip_markdown(skip_entries(payload), declared_skips(payload))
    if not fails and not green:
        parts += ["", f"没有 {FAILURE_LABEL} 行（pytest 退出码 `{status}`）——常见于崩在采集前或"
                      "进程被带崩。末尾输出：", "", "```text",
                  *payload.splitlines()[-60:], "```"]
    return "\n".join(parts) + "\n"


def main() -> int:
    output_path = Path(sys.argv[1] if len(sys.argv) > 1 else "pytest-output.txt")
    missing = not output_path.is_file()
    payload = "" if missing else output_path.read_text(encoding="utf-8", errors="replace")
    status = "?" if missing else exit_status(output_path)
    outcome = test_step_outcome()
    green = outcome == "success"
    fails = failure_lines(payload)
    entries = skip_entries(payload)
    # 顺序很重要：annotation 先走。它是免登录时唯一可靠的通道，不能因为
    # 「写 job summary 失败」或「没有 GITHUB_STEP_SUMMARY」而被连带沉默——那两条早退
    # 路径与上面那个「无 FAILED 行」一起，构成同一个缺陷的四个子形：仪表只在
    # 一切顺利时开口，而失败时不顺利的恰恰就是这些前置条件。
    # skip 的 notice 放最前：GitHub 只留最近 10 条，红跑时要保的是失败详情，
    # 被顶掉的应该是这条（绿跑时本来就只有系统那 2 条，notice 肯定能留下）。
    if green and entries:
        print(workflow_notice(skip_notice(entries)))
    if missing:
        if green:
            print(workflow_error(
                "test step reported success but pytest-output.txt is missing"))
        else:
            print(workflow_error("no pytest-output.txt: the failure happened BEFORE the test step"))
    elif green:
        emit_green(payload, status, entries, fails)
    else:
        if entries:
            print(workflow_notice(skip_notice(entries)))
        emit_annotations(payload, status)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        print(f"[ci_failure_summary] no GITHUB_STEP_SUMMARY, summary skipped "
              f"(output file: {output_path}, failure_lines={len(fails)}, "
              f"skip_lines={len(entries)})")
        return 0
    markdown = build_markdown(payload, missing=missing, status=status, green=green)
    try:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(markdown)
    except OSError as error:  # 诊断步骤不得让 job 再红一次
        print(f"[ci_failure_summary] cannot write summary: {error}")
        return 0
    # stdout 只放两类东西：ASCII 状态行 + 带中文的 annotation（已钉 UTF-8）；
    # 中文 Markdown 正文只进摘要文件，不往管道里刷。
    print(f"[ci_failure_summary] ok: wrote {len(markdown)} chars to job summary; "
          f"missing_output={int(missing)} failure_lines={len(fails)} skip_lines={len(entries)} "
          f"exit={status} outcome={outcome or 'unknown'}")
    return 0


def emit_green(payload: str, status: str, entries: list[tuple[int, str, str]],
               fails: list[str]) -> None:
    """绿跑不得发 `::error::`（无事不报），但也不能沉默：skip 名单就是它的正文。

    两个不自洽的例外必须报：绿却带失败行/非零退出码（上报链路或工作流坏了）；
    汇总行说跳过了 N 项却没有 `SKIPPED` 行（测试步骤少了 `-rs`，名单正在静默丢失）。
    后者用 warning：job 确实绿，把它报成 error 是另一种漏报。
    """
    if fails or status not in ("0", "?"):
        print(workflow_error(
            f"测试步骤结论为 success 但输出不自洽：{FAILURE_LABEL} 行 {len(fails)} 条，"
            f"pytest 退出码 {status}"))
        return
    declared = declared_skips(payload)
    listed = skip_total(entries)
    if declared and not entries:
        print(workflow_warning(
            f"汇总行有 {declared} 项 skipped，但短汇总里没有 SKIPPED 行——"
            f"测试步骤未带 `-rs`，skip 名单不可读"))
    elif declared != listed:
        print(workflow_warning(
            f"skip 名单与汇总行不一致：汇总 {declared}，逐条合计 {listed}"))


def emit_annotations(payload: str, status: str = "?") -> None:
    """在 8 条预算内把失败信息压出去；最重要的结论放最后一条（被挤掉时先没的是细节）。

    job summary 里的完整清单照旧写（浏览器里看得全），但本仓库的作者常常只能拿到
    免登录的静态页面，所以 annotation 必须自己就能回答「哪几个用例、几类根因」。

    **永不沉默**：没有失败行时也必须报一条。run 35303020780 的 ubuntu job 就是
    倒在这里——测试步骤非零退出、汇总步骤跑成功，但 annotation 一条没发，免登录能看到的
    就只剩「exit code 1」，等于仪表在最需要它的分支上罢工。（同一个沉默还有第三个子形：
    run 35305181428 的红项是子测试，pytest 打的是 `SUBFAILED(...)`，旧版把它读成「没有
    失败行」，于是报了一条与真实原因无关的 tail。）
    """
    fails = failure_lines(payload)
    if not fails:
        tail = " \u23ce ".join(line[:120] for line in tail_lines(payload))
        print(workflow_error(
            f"no {FAILURE_LABEL} line but pytest exited {status} "
            f"(bytes={len(payload.encode('utf-8', 'replace'))}); tail: {tail[:700]}"[:1200]))
        return
    budget = MAX_ANNOTATIONS
    messages: list[str] = []
    # 1) 根因聚类（最有价值，最后发，顺位上不会被顶掉）
    clusters = signatures(fails)
    # 2) 失败用例名：按每条 ~120 字、每次 6 条 annotation 均分剩下的预算
    ids = [test_id(line) for line in fails]
    id_slots = max(1, budget - 2)
    per_chunk = max(1, len(ids) // id_slots + (1 if len(ids) % id_slots else 0))
    chunks = chunk_join(ids, per_chunk)
    dropped = max(0, len(chunks) - id_slots)
    if dropped:
        chunks = chunks[:id_slots]
    for index, chunk in enumerate(chunks, start=1):
        messages.append(f"FAIL[{index}/{len(chunks)}] {chunk}")
    if dropped:
        messages.append(f"另有 {dropped * per_chunk} 项未列出（GitHub 10 条上限），"
                        f"完整清单在 job summary")
    messages.append(f"失败条目 total={len(fails)} 根因聚类（前 {len(clusters)} 类）: "
                    + " || ".join(clusters))
    for message in messages[-budget:]:
        print(workflow_error(message[:ANNOTATION_LINE_CHARS * 4]))


if __name__ == "__main__":
    raise SystemExit(main())
