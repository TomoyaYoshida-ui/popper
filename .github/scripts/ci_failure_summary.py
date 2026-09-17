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


def failure_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(("FAILED", "ERROR"))]


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
    # 只往 stdout 打 ASCII 状态行：本脚本的中文只进摘要文件，不依赖宿主 locale。
    # （把含中文的结果 print 到管道，正是 verify_claims.py 刚踩过的同一个坑。）
    print(f"[ci_failure_summary] ok: wrote {len(markdown)} chars to job summary; "
          f"missing_output={int(missing)} failure_lines={len(failure_lines(payload))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
