"""Create both projects using the CLI imported from an independently installed wheel."""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_installed_demo.py <pip --target directory>")
    installed = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(installed))
    import popper
    from popper.cli import main as cli_main
    from popper.demo import FILES

    if not Path(popper.__file__).resolve().is_relative_to(installed):
        raise SystemExit("Demo smoke imported checkout instead of installed wheel")
    with tempfile.TemporaryDirectory(prefix="popper-wheel-demo-") as temporary:
        for case in ("ode-convergence", "throughput-sort"):
            project = Path(temporary) / case
            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(["demo", "init", str(project), "--case", case])
            if code != 0 or json.loads(output.getvalue())["case"] != case:
                raise SystemExit(f"Installed CLI could not initialize {case}")
            if any(not (project / name).is_file() for name in FILES):
                raise SystemExit(f"Installed demo is incomplete: {case}")
            run_dir = Path(temporary) / (case + "-run")
            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(["research", "init", str(project), str(run_dir)])
            if code != 0 or not run_dir.is_dir():
                raise SystemExit(f"Installed research init failed for {case}")
            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(["research", "run", str(run_dir),
                                 "--trusted-local", "--max-steps", "1"])
            if code != 0:
                raise SystemExit(f"Installed research run failed for {case}: {output.getvalue()}")
    print("Installed wheel CLI: both demos and one trusted-local research step OK")


if __name__ == "__main__":
    main()
