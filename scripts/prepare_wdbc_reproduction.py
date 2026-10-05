"""Create a fresh, registered WDBC paper-reproduction task from repo inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from popper.core import ProtocolError
from popper.reproduction import initialize_reproduction


SOURCE = Path(__file__).resolve().parents[1] / "reproductions" / "street-1993-wdbc"
INPUTS = ("reproduction.json", "paper.json", "protocol.json", "reproduce.py",
          "verify.py", "source/wdbc.data")


def prepare(target: Path) -> dict:
    target = Path(target).resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ProtocolError("目标必须是不存在或为空的目录；不会覆盖旧复现任务或证据")
    protocol = json.loads((SOURCE / "protocol.json").read_text(encoding="utf-8"))
    dataset = SOURCE / "source" / "wdbc.data"
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != protocol["dataset"]["expected_sha256"]:
        raise ProtocolError("仓库内 WDBC 数据与冻结协议哈希不一致")
    for name in INPUTS:
        if not (SOURCE / name).is_file():
            raise ProtocolError(f"缺少复现输入：{name}")
    target.mkdir(parents=True, exist_ok=True)
    for name in INPUTS:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, destination)
    state = initialize_reproduction(target)
    return {"project": str(target), "phase": state["phase"],
            "next": f"popper experiment reproduce run {target} --trusted-local"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.target), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
