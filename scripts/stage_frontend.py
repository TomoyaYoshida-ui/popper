"""Copy the verified Vite build into the Python package, or check for drift."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend" / "dist"
TARGET = ROOT / "popper" / "web"
BUILD_CACHE = ROOT / "build" / "lib" / "popper" / "web"


def files_under(root: Path) -> dict[str, bytes]:
    if not (root / "index.html").is_file():
        raise SystemExit(f"Missing frontend build: {root / 'index.html'}")
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if staged assets differ")
    args = parser.parse_args()
    expected = files_under(SOURCE)
    if args.check:
        actual = files_under(TARGET)
        if actual != expected:
            missing = sorted(expected.keys() - actual.keys())
            stale = sorted(actual.keys() - expected.keys())
            changed = sorted(name for name in expected.keys() & actual.keys()
                             if expected[name] != actual[name])
            raise SystemExit(f"Staged frontend differs: missing={missing}, "
                             f"stale={stale}, changed={changed}")
        print(f"Frontend package assets match build ({len(actual)} files)")
        return
    if TARGET.resolve().parent != (ROOT / "popper").resolve() or TARGET.name != "web":
        raise SystemExit("Unsafe frontend staging target")
    if (BUILD_CACHE.resolve().parent != (ROOT / "build" / "lib" / "popper").resolve()
            or BUILD_CACHE.name != "web"):
        raise SystemExit("Unsafe frontend build-cache target")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    if BUILD_CACHE.exists():
        shutil.rmtree(BUILD_CACHE)
    shutil.copytree(SOURCE, TARGET)
    print(f"Staged {len(expected)} frontend files into {TARGET}")


if __name__ == "__main__":
    main()
