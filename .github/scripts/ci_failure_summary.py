"""把 pytest 输出里的失败清单写进 GitHub Actions 的 job summary。

为什么需要它：Actions 的运行日志要求登录才能看（公开仓库也一样），失败时外面的人
（包括没有凭据的协作者与本仓库的作者）只能看到「Process completed with exit code 1」，
只能靠猜。job summary 渲染在 run 页面上，公开仓库免登录可读，所以把「哪些用例红了」
写进摘要，就等于让 CI 自己把证据递出来。

只做一件事：读测试步骤落盘的 pytest 输出（`> pytest-output.txt 2>&1`），抽取 `FAILED`/
`ERROR` 行与末尾汇总行，以 UTF-8 追加到 `$GITHUB_STEP_SUMMARY`。所有文件读写都显式
指定 encoding，不随宿主 locale 变（这是 verify_claims.py 刚踩过的同一个坑）。

本脚本自身**永不以非零退出**：它是失败后的诊断步骤，不该再把 job 变成第二个红点。
"""
from __future__ import annotations

import os
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

# pytest 的断言消息本身就是中文，所以 ::error:: 行会带中文：stdout 必须自己钉在 UTF-8，
# 不能靠宿主 locale（runner 把 stdout 当管道收，中文 Windows 上不钉就是 cp936）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def workflow_error(message: str) -> str:
    """拼 GitHub Actions 工作流命令；按文档预转义 % / CR / LF，否则会被截断或误解析。"""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error::{escaped}"


def failure_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(("FAILED", "ERROR"))]


def test_id(line: str) -> str:
    """`FAILED tests/x.py::K::t - 详情` -> `tests/x.py::K::t`。"""
    return line.split(" - ", 1)[0].split(" ", 1)[-1]


def reason_of(line: str) -> str:
    return line.split(" - ", 1)[1].strip() if " - " in line else line


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


def build_markdown(payload: str, missing: bool) -> str:
    label = os.environ.get("GITHUB_JOB", "job")
    runner = os.environ.get("RUNNER_OS", "?")
    python = os.environ.get("pythonLocation") or sys.executable
    parts = [f"### CI 失败清单 · `{label}` · {runner}", "", f"解释器：`{python}`", ""]
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
    parts += fails[:MAX_FAILURE_LINES] or ["（没有 FAILED/ERROR 行，见末尾汇总）"]
    parts.append("```")
    tail = summary_lines(payload)
    if tail:
        parts += ["", "末尾汇总：", "", "```text", *tail, "```"]
    if not fails:
        parts += ["", "完整输出（可能是采集错误或超时）：", "", "```text",
                  *payload.splitlines()[-60:], "```"]
    return "\n".join(parts) + "\n"


def main() -> int:
    output_path = Path(sys.argv[1] if len(sys.argv) > 1 else "pytest-output.txt")
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        print(f"[ci_failure_summary] skipped: no GITHUB_STEP_SUMMARY (output file: {output_path})")
        return 0
    missing = not output_path.is_file()
    payload = "" if missing else output_path.read_text(encoding="utf-8", errors="replace")
    markdown = build_markdown(payload, missing=missing)
    try:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(markdown)
    except OSError as error:  # 诊断步骤不得让 job 再红一次
        print(f"[ci_failure_summary] cannot write summary: {error}")
        return 0
    # stdout 只放两类东西：ASCII 状态行 + 带中文的 ::error:: annotation（已钉 UTF-8）；
    # 中文 Markdown 正文只进摘要文件，不往管道里刷。
    print(f"[ci_failure_summary] ok: wrote {len(markdown)} chars to job summary; "
          f"missing_output={int(missing)} failure_lines={len(failure_lines(payload))}")
    emit_annotations(payload)
    if missing:
        print(workflow_error("no pytest-output.txt: the failure happened BEFORE the test step"))
    return 0


def emit_annotations(payload: str) -> None:
    """在 8 条预算内把失败信息压出去；最重要的结论放最后一条（被挤掉时先没的是细节）。

    job summary 里的完整清单照旧写（浏览器里看得全），但本仓库的作者常常只能拿到
    免登录的静态页面，所以 annotation 必须自己就能回答「哪几个用例、几类根因」。
    """
    fails = failure_lines(payload)
    if not fails:
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
        messages.append(f"FAILED[{index}/{len(chunks)}] {chunk}")
    if dropped:
        messages.append(f"FAILED 另有 {dropped * per_chunk} 项未列出（GitHub 10 条上限），"
                        f"完整清单在 job summary")
    messages.append(f"FAILED total={len(fails)} 根因聚类（前 {len(clusters)} 类）: "
                    + " || ".join(clusters))
    for message in messages[-budget:]:
        print(workflow_error(message[:ANNOTATION_LINE_CHARS * 4]))


if __name__ == "__main__":
    raise SystemExit(main())
