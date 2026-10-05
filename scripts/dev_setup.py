"""Set up a Popper source checkout and stage its built frontend/demo assets."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extras", default="", help="optional extras, e.g. workstation,ml")
    parser.add_argument("--test", action="store_true", help="run focused developer smoke tests")
    parser.add_argument("--check", action="store_true", help="check staged assets without installing")
    args = parser.parse_args()
    if not (ROOT / "pyproject.toml").is_file():
        raise SystemExit("Popper repository root not found")
    if args.check:
        run(sys.executable, "scripts/stage_frontend.py", "--check")
        run(sys.executable, "scripts/stage_demos.py", "--check")
        return
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("Node.js npm is required to build the workbench frontend")
    if args.extras and any(not part.replace("-", "").isalnum()
                           for part in args.extras.split(",")):
        raise SystemExit("--extras must be comma-separated package extra names")
    editable = ".[" + args.extras + "]" if args.extras else "."
    run(sys.executable, "-m", "pip", "install", "-e", editable)
    run(npm, "ci", "--prefix", "frontend")
    run(npm, "run", "build", "--prefix", "frontend")
    run(sys.executable, "scripts/stage_frontend.py")
    run(sys.executable, "scripts/stage_demos.py")
    if args.test:
        run(sys.executable, "-m", "pip", "install", "pytest")
        run(sys.executable, "-m", "pytest", "tests/test_demo.py",
            "tests/test_packaged_web.py", "tests/test_doc_consistency.py", "-q")
    print("Ready. Try: popper demo init <empty-directory> --case ode-convergence")


if __name__ == "__main__":
    main()
