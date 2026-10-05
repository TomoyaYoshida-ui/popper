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
    return {"case": case, "project": str(destination), "initialized": True,
            "next": [f"popper research init {destination} <research-run目录>",
                     f"popper serve {destination}"]}
