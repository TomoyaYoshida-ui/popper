"""Persist a serial full-regression exit receipt across chat/tool lifetimes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from popper.core import file_hash, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    revision = args.revision.resolve()
    log = args.log.resolve()
    receipt = log.with_suffix(".receipt.json")
    root = Path(__file__).resolve().parents[1]
    frozen = json.loads(revision.read_text(encoding="utf-8"))
    sources = frozen["implementation_sha256"]
    if any(file_hash(path) != expected for path, expected in sources.items()):
        raise SystemExit("Frozen source mismatch; regression not launched")
    started = datetime.now(timezone.utc).isoformat()
    command = [sys.executable, "-m", "pytest", "-q", "-rfEs", "--tb=short"]
    # Never overwrite prior failure/interruption evidence.
    with log.open("x", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=root, stdout=stream,
                                stderr=subprocess.STDOUT, check=False)
    stable = all(file_hash(path) == expected for path, expected in sources.items())
    write_json(receipt, {"command": command, "revision": str(revision),
        "revision_sha256": file_hash(revision), "log_sha256": file_hash(log),
        "exit_code": result.returncode, "sources_stable": stable,
        "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat()})
    return result.returncode if stable else 2


if __name__ == "__main__":
    raise SystemExit(main())
