"""Compare every shipped offline demo byte against its staged source."""
from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
STAGED = ROOT / "popper" / "demo_templates"
PREFIX = "popper/demo_templates/"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: check_wheel_demos.py <wheel>")
    expected = {PREFIX + path.relative_to(STAGED).as_posix(): path.read_bytes()
                for path in STAGED.rglob("*")
                if path.is_file() and path.name in {
                    "experiment.json", "model.py", "train.json", "dev.json", "test.json"}}
    if len(expected) != 10:
        raise SystemExit(f"Expected ten staged demo files, found {len(expected)}")
    with ZipFile(sys.argv[1]) as archive:
        actual = {name for name in archive.namelist()
                  if name.startswith(PREFIX) and not name.endswith("/")}
        if actual != expected.keys():
            raise SystemExit(f"Wheel demos differ: missing={sorted(expected.keys() - actual)}, "
                             f"extra={sorted(actual - expected.keys())}")
        changed = [name for name, content in expected.items()
                   if archive.read(name) != content]
        if changed:
            raise SystemExit(f"Wheel demo bytes differ: {sorted(changed)}")
    print("Wheel contains both complete offline demos (10 verified files)")


if __name__ == "__main__":
    main()
