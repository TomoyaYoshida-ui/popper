"""Assert that a Popper wheel contains the real workbench entry and its assets."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
from zipfile import ZipFile


class AssetRefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        address = values.get("src") if tag == "script" else values.get("href")
        if address and address.startswith("/assets/"):
            self.paths.add("popper/web" + address)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    staged = Path(__file__).resolve().parents[2] / "popper" / "web"
    expected = {"popper/web/" + path.relative_to(staged).as_posix(): path.read_bytes()
                for path in staged.rglob("*") if path.is_file()}
    if not expected:
        raise SystemExit("No staged workbench files to compare")
    with ZipFile(args.wheel) as archive:
        names = set(archive.namelist())
        packaged = {name for name in names
                    if name.startswith("popper/web/") and not name.endswith("/")}
        if packaged != expected.keys():
            raise SystemExit(f"Wheel workbench inventory differs: "
                             f"missing={sorted(expected.keys() - packaged)}, "
                             f"extra={sorted(packaged - expected.keys())}")
        changed = [name for name, content in expected.items()
                   if archive.read(name) != content]
        if changed:
            raise SystemExit(f"Wheel workbench bytes differ: {sorted(changed)}")
        index = "popper/web/index.html"
        if index not in names:
            raise SystemExit("Wheel does not contain workbench index.html")
        refs = AssetRefs()
        refs.feed(archive.read(index).decode("utf-8"))
        if not refs.paths:
            raise SystemExit("Workbench index has no referenced compiled assets")
        missing = sorted(refs.paths - names)
        if missing:
            raise SystemExit(f"Wheel is missing workbench assets: {missing}")
        print(f"Wheel workbench complete: {len(expected)} files, "
              f"{len(refs.paths)} entrypoint assets")


if __name__ == "__main__":
    main()
