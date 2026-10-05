"""Stage the two offline, non-ML examples inside the Python package."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = ("ode-convergence", "throughput-sort")
FILES = ("experiment.json", "model.py", "train.json", "dev.json", "test.json")
SOURCE = ROOT / "examples"
TARGET = ROOT / "popper" / "demo_templates"
BUILD_CACHE = ROOT / "build" / "lib" / "popper" / "demo_templates"


def contents(root: Path) -> dict[str, bytes]:
    result = {}
    for case in CASES:
        for name in FILES:
            path = root / case / name
            if not path.is_file():
                raise SystemExit(f"Missing demo file: {path}")
            result[f"{case}/{name}"] = path.read_bytes()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = contents(SOURCE)
    if args.check:
        actual = contents(TARGET)
        extra = [p.relative_to(TARGET).as_posix() for p in TARGET.rglob("*")
                 if p.is_file() and p.relative_to(TARGET).as_posix() not in expected]
        if actual != expected or extra:
            raise SystemExit(f"Packaged demo templates differ from examples; extra={extra}")
        print(f"Demo templates match source ({len(actual)} files)")
        return
    if TARGET.resolve().parent != (ROOT / "popper").resolve():
        raise SystemExit("Unsafe demo staging target")
    if BUILD_CACHE.resolve().parent != (ROOT / "build" / "lib" / "popper").resolve():
        raise SystemExit("Unsafe demo build-cache target")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    if BUILD_CACHE.exists():
        shutil.rmtree(BUILD_CACHE)
    for case in CASES:
        destination = TARGET / case
        destination.mkdir(parents=True)
        for name in FILES:
            shutil.copyfile(SOURCE / case / name, destination / name)
    print(f"Staged {len(expected)} demo files into {TARGET}")


if __name__ == "__main__":
    main()
