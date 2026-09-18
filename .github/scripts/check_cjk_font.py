"""自证：popper 在本机能否探测到「子集化器真能渲染」的中文字体；失败时把环境事实打成 annotation。

为什么单独一步：runner 上「没装字体」与「装了但 popper 的探测不认」在测试里是同一条红
（`PDF 渲染需要系统中文字体`），可前者要 apt、后者要改代码。Actions 日志需登录才能看，
所以这里把候选清单与逐个探测结果直接打成 annotation（公开仓库免登录可读）。

本步骤**不充当门禁**：真正的门禁是 `test_manuscript_pdf_is_valid_single_embedded_font`。
诊断步骤若把自己变成红，就会连带 skip 掉后面的全量测试——那等于用一个环境抖动
遮掉整轮信号（run 35300378254 的 ubuntu job 正是这样丢掉了 Linux 侧的全部结果）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

CJK_SAMPLE = 0x4E2D          # '中'
NAME_HINTS = ("cjk", "chinese", "noto", "wqy", "hans", "hant", "sc", "hei",
              "song", "kai", "simhei", "simkai", "simfang", "msyh", "deng", "ming")
MAX_REPORTED = 12


def font_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("WINDIR") or "C:/Windows") / "Fonts"
    return Path("/usr/share/fonts")


def inspect(path: Path) -> str:
    """返回一行紧凑判定：轮廓类型 + 是否含常用汉字（机读行只用 ASCII，免得被控制台编码洗掉）。"""
    from popper.pdfgen import load_font

    try:
        font = load_font(path)
    except Exception as error:  # noqa: BLE001
        return f"load-error {type(error).__name__}"
    true_type = "glyf" in font.tables and "loca" in font.tables
    has_cjk = bool(font.glyph_map.get(CJK_SAMPLE, 0))
    return f"{'TrueType' if true_type else 'not-truetype'}/cjk={'y' if has_cjk else 'n'}"


def main() -> int:
    from popper.pdfgen import discover_font

    base = font_dir()
    all_tt = sorted(base.rglob("*.tt*")) if base.is_dir() else []
    hints = [p for p in all_tt if any(q in p.name.lower() for q in NAME_HINTS)]
    print(f"[check_cjk_font] dir={base} exists={base.is_dir()} "
          f"tt_files={len(all_tt)} name_matched={len(hints)}")
    for path in hints[:MAX_REPORTED]:
        print(f"  {path.name}: {inspect(path)}")
    found = discover_font()
    if found:
        print(f"[check_cjk_font] ok: popper 探测到可用字体 {found}")
        return 0
    detail = "; ".join(f"{p.name}={inspect(p)}" for p in hints[:6]) or "无名称匹配的候选"
    print(f"::error::popper 探测不到可用的 TrueType 中文字体："
          f"dir={base} exists={base.is_dir()} tt_files={len(all_tt)} "
          f"matched={len(hints)} 逐个判定[{detail[:400]}]")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
