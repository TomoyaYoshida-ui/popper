"""把 pytest 输出里的失败清单写进 GitHub Actions 的 job summary。

为什么需要它：Actions 的运行日志要求登录才能看（公开仓库也一样），失败时外面的人
（包括没有凭据的协作者与本仓库的作者）只能看到「Process completed with exit code 1」，
只能靠猜。job summary 渲染在 run 页面上，公开仓库免登录可读，所以把「哪些用例红了」
写进摘要，就等于让 CI 自己把证据递出来。

只做一件事：读测试步骤落盘的 pytest 输出（`> pytest-output.txt 2>&1`），抽取失败行
（`FAILED`/`ERROR` 以及子测试的 `SUBFAILED`/`SUBERROR`）与末尾汇总行，以 UTF-8 追加到
`$GITHUB_STEP_SUMMARY`，并把关键结论同时打成 `::error::` annotation。所有文件读写都
显式指定 encoding，不随宿主 locale 变（这是 verify_claims.py 刚踩过的同一个坑）。

两条通道彼此独立：摘要写失败、没有 `GITHUB_STEP_SUMMARY`、输出里没有失败行——都不能
让 annotation 闭嘴（它常常是免登录时唯一能拿到信息的那一条）。

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

# pytest 的断言消息本身就是中文，所以 ::error:: 行会带中文：stdout 必须自己钉在 UTF-8，
# 不能靠宿主 locale（runner 把 stdout 当管道收，中文 Windows 上不钉就是 cp936）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def workflow_error(message: str) -> str:
    """拼 GitHub Actions 工作流命令；按文档预转义 % / CR / LF，否则会被截断或误解析。"""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error::{escaped}"


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


def build_markdown(payload: str, missing: bool, status: str = "?") -> str:
    label = os.environ.get("GITHUB_JOB", "job")
    runner = os.environ.get("RUNNER_OS", "?")
    python = os.environ.get("pythonLocation") or sys.executable
    parts = [f"### CI 失败清单 · `{label}` · {runner}", "",
             f"解释器：`{python}`  pytest 退出码：`{status}`", ""]
    if missing:
        parts += [
            "没有 `pytest-output.txt`——失败发生在**测试步骤之前**"
            "（安装依赖 / bubblewrap / 沙箱探活之一），需要看该步骤的日志。",
        ]
        return "\n".join(parts) + "\n"
    fails = failure_lines(payload)
    parts.append(f"失败/错误条目 {len(fails)} 项（最多显示 {MAX_FAILURE_LINES} 项）：")
    parts.append("")
    parts.append("```text")
    parts += fails[:MAX_FAILURE_LINES] or [f"（没有 {FAILURE_LABEL} 行，见末尾汇总）"]
    parts.append("```")
    tail = summary_lines(payload)
    if tail:
        parts += ["", "末尾汇总：", "", "```text", *tail, "```"]
    if not fails:
        parts += ["", f"没有 {FAILURE_LABEL} 行（pytest 退出码 `{status}`）——常见于崩在采集前或"
                      "进程被带崩。末尾输出：", "", "```text",
                  *payload.splitlines()[-60:], "```"]
    return "\n".join(parts) + "\n"


def main() -> int:
    output_path = Path(sys.argv[1] if len(sys.argv) > 1 else "pytest-output.txt")
    missing = not output_path.is_file()
    payload = "" if missing else output_path.read_text(encoding="utf-8", errors="replace")
    status = "?" if missing else exit_status(output_path)
    fails = failure_lines(payload)
    # 顺序很重要：annotation 先走。它是免登录时唯一可靠的通道，不能因为
    # 「写 job summary 失败」或「没有 GITHUB_STEP_SUMMARY」而被连带沉默——那两条早退
    # 路径与上面那个「无 FAILED 行」一起，构成同一个缺陷的四个子形：仪表只在
    # 一切顺利时开口，而失败时不顺利的恰恰就是这些前置条件。
    if missing:
        print(workflow_error("no pytest-output.txt: the failure happened BEFORE the test step"))
    else:
        emit_annotations(payload, status)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        print(f"[ci_failure_summary] no GITHUB_STEP_SUMMARY, summary skipped "
              f"(output file: {output_path}, failure_lines={len(fails)})")
        return 0
    markdown = build_markdown(payload, missing=missing, status=status)
    try:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(markdown)
    except OSError as error:  # 诊断步骤不得让 job 再红一次
        print(f"[ci_failure_summary] cannot write summary: {error}")
        return 0
    # stdout 只放两类东西：ASCII 状态行 + 带中文的 ::error:: annotation（已钉 UTF-8）；
    # 中文 Markdown 正文只进摘要文件，不往管道里刷。
    print(f"[ci_failure_summary] ok: wrote {len(markdown)} chars to job summary; "
          f"missing_output={int(missing)} failure_lines={len(fails)} exit={status}")
    return 0


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
