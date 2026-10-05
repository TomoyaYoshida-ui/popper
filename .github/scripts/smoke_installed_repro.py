"""Prove an independently installed wheel can export and replay raw evidence."""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_installed_repro.py <pip --target directory>")
    installed = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(installed))
    import popper
    from popper.cli import main as cli_main

    if not Path(popper.__file__).resolve().is_relative_to(installed):
        raise SystemExit("Reproduction smoke imported checkout instead of installed wheel")

    def call(*args: str) -> dict:
        output = StringIO()
        with redirect_stdout(output):
            code = cli_main(list(args))
        if code != 0:
            raise SystemExit(f"Installed CLI failed: {args!r}; {output.getvalue()}")
        return json.loads(output.getvalue())

    with tempfile.TemporaryDirectory(prefix="popper-wheel-repro-") as temporary:
        root = Path(temporary)
        project = root / "ode"
        exported = root / "export"
        extracted = root / "extracted"
        call("demo", "init", str(project), "--case", "ode-convergence")
        call("experiment", "search", str(project), "--trusted-local")
        call("experiment", "freeze", str(project))
        call("experiment", "confirm", str(project), "--trusted-local")
        package = call("research", "materialize", "repro", str(project), str(exported),
                       "--include-raw-evidence")
        if package.get("verification_level") != "raw_prediction_replay_with_installed_popper":
            raise SystemExit("Installed wheel did not export raw replay evidence")
        with ZipFile(package["path"]) as archive:
            archive.extractall(extracted)
        replay = call("experiment", "replay", str(extracted), "--portable")
        if not replay.get("claim_recomputed") or replay.get("runs_recomputed", 0) < 4:
            raise SystemExit(f"Installed wheel could not recompute extracted claim: {replay}")
    print("Installed wheel: non-ML claim exported and replayed from extracted raw evidence OK")


if __name__ == "__main__":
    main()
