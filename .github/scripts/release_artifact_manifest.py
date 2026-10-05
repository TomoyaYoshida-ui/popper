"""Record inspectable hashes and wheel inventory for CI build artifacts.

This is an artifact manifest, not a dependency SBOM or a signed provenance
statement. It describes exactly the wheel and sdist produced in this job.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def describe(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Missing artifact: {path}")
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}


def wheel_inventory(path: Path) -> dict:
    with ZipFile(path) as archive:
        names = sorted(name for name in archive.namelist() if not name.endswith("/"))
        metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise SystemExit("Wheel must have one METADATA file")
        metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8"))
        if metadata.get("Name") != "popper-research":
            raise SystemExit("Unexpected wheel distribution name")
        return {
            "distribution": metadata["Name"], "version": metadata["Version"],
            "summary": metadata.get("Summary"),
            "development_status": [item for item in metadata.get_all("Classifier", [])
                                   if item.startswith("Development Status ::")],
            "files": [{"path": name, "bytes": archive.getinfo(name).file_size,
                       "sha256": hashlib.sha256(archive.read(name)).hexdigest()}
                      for name in names],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sdist", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="verify an existing manifest")
    args = parser.parse_args()
    inventory = wheel_inventory(args.wheel)
    payload = {"schema_version": "1.0", "kind": "ci_artifact_manifest",
               "claim": "hashes_and_inventory_only",
               "wheel": describe(args.wheel), "sdist": describe(args.sdist),
               "wheel_inventory": inventory}
    if args.check:
        if not args.output.is_file() or json.loads(args.output.read_text(encoding="utf-8")) != payload:
            raise SystemExit("Artifact manifest differs from current wheel/sdist")
        print(f"Artifact manifest verified: {args.output}")
        return
    args.output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      indent=2) + "\n", encoding="utf-8")
    print(f"Artifact manifest: {args.output} ({len(inventory['files'])} wheel files)")


if __name__ == "__main__":
    main()
