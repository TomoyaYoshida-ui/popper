"""Offline, non-ML starter projects shipped with the installed package."""
from __future__ import annotations

import shutil
from pathlib import Path

from .core import ProtocolError, initialize


CASES = ("ode-convergence", "throughput-sort")
FILES = ("experiment.json", "model.py", "train.json", "dev.json", "test.json")


def initialize_demo(target, case="ode-convergence"):
    if case not in CASES:
        raise ProtocolError(f"未知示例：{case}")
    destination = Path(target).resolve()
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ProtocolError("示例目标必须是不存在或为空的目录，拒绝覆盖现有数据")
    source = Path(__file__).resolve().parent / "demo_templates" / case
    if not all((source / name).is_file() for name in FILES):
        raise ProtocolError(f"安装包缺少示例文件：{case}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copyfile(source / name, destination / name)
    initialize(destination)
    project_name = destination.name
    return {
        "case": case,
        "project": str(destination),
        "initialized": True,
        "next": [
            f"popper research init {project_name} demo-run",
            "popper research run demo-run --trusted-local",
            "popper research confirm demo-run --trusted-local",
        ],
        "note": "默认产品路径：已登记研究证据闭环。判断多为 inconclusive 属正常，"
                "不代表策略优劣；确认集只打开一次。",
    }


def summarize_research_status(status: dict) -> dict:
    """Compact human-facing readout for the default demo path."""
    study = status.get("study") or {}
    judgments = status.get("judgments") or []
    claims = status.get("claims") or []
    latest = judgments[-1] if judgments else None
    claim = claims[-1] if claims else None
    return {
        "phase": status.get("phase"),
        "study_status": study.get("status"),
        "integrity_ok": bool((status.get("integrity") or {}).get("ok")),
        "observations": len(status.get("observations") or []),
        "latest_judgment": (None if latest is None else {
            "scope": latest.get("scope"),
            "new_judgment": latest.get("new_judgment"),
            "reason": latest.get("reason"),
        }),
        "claim_status": None if claim is None else claim.get("status"),
        "budget": status.get("budget"),
        "read_as": (
            "演示完成：phase=concluded 且 integrity_ok。"
            "科研判断与描述性 claim 可能不一致；以 judgment 三态为准，"
            "inconclusive 不是失败。"
            if status.get("phase") == "concluded"
            else "尚未结束：继续 research run / confirm。"
        ),
    }


def run_offline_smoke(*, root: Path | None = None, case: str = "ode-convergence") -> dict:
    """Create a temp (or given) root and run demo → init → run → confirm offline."""
    import tempfile

    from .research.controller import ResearchController

    if case not in CASES:
        raise ProtocolError(f"未知示例：{case}")
    owned = root is None
    base = Path(tempfile.mkdtemp(prefix="popper-demo-smoke-")) if owned else Path(root).resolve()
    if not owned:
        base.mkdir(parents=True, exist_ok=True)
        if any(base.iterdir()):
            raise ProtocolError("smoke 根目录必须为空")
    project = base / "demo-ode"
    run_dir = base / "demo-run"
    try:
        init = initialize_demo(project, case)
        ResearchController.initialize(project, run_dir)
        controller = ResearchController(run_dir)
        try:
            developed = controller.run(trusted_local=True)
            confirmed = controller.confirm(trusted_local=True)
        finally:
            controller.close()
        summary = summarize_research_status(confirmed)
        return {
            "ok": bool(summary["integrity_ok"] and summary["phase"] == "concluded"),
            "case": case,
            "root": str(base),
            "project": init["project"],
            "run_dir": str(run_dir),
            "after_run_phase": developed.get("phase"),
            "summary": summary,
        }
    except Exception:
        if owned:
            shutil.rmtree(base, ignore_errors=True)
        raise
