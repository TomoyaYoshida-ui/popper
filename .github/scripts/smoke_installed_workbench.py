"""Serve the workbench from an independently installed wheel, not the checkout."""
from __future__ import annotations

import json
import runpy
import shutil
import sys
import tempfile
import threading
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class AssetRefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        address = values.get("src") if tag == "script" else values.get("href")
        if address and address.startswith("/assets/"):
            self.paths.add(address)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_installed_workbench.py <pip --target directory>")
    installed = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(installed))
    import popper
    from popper.core import initialize
    from popper.server import Workstation

    if not Path(popper.__file__).resolve().is_relative_to(installed):
        raise SystemExit("Smoke test imported checkout instead of installed wheel")
    example = ROOT / "examples" / "quadratic"
    with tempfile.TemporaryDirectory(prefix="popper-wheel-http-") as temporary:
        project = Path(temporary) / "project"
        project.mkdir()
        for name in ("experiment.json", "model.py"):
            shutil.copyfile(example / name, project / name)
        runpy.run_path(str(example / "generate_data.py"))["generate"](project)
        initialize(project)
        server = Workstation(("127.0.0.1", 0), project)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/", timeout=5) as response:
                if response.status != 200:
                    raise SystemExit(f"Workbench index returned {response.status}")
                html = response.read().decode("utf-8")
            refs = AssetRefs()
            refs.feed(html)
            if not refs.paths:
                raise SystemExit("Installed workbench has no compiled asset references")
            for path in sorted(refs.paths):
                with urllib.request.urlopen(base + path, timeout=5) as response:
                    if response.status != 200 or not response.read():
                        raise SystemExit(f"Workbench asset failed: {path}")
            asset_root = Path(popper.__file__).resolve().parent / "web" / "assets"
            assets = list(asset_root.rglob("*"))
            assets = [path for path in assets if path.is_file()]
            for asset in assets:
                url = "/assets/" + asset.relative_to(asset_root).as_posix()
                with urllib.request.urlopen(base + url, timeout=5) as response:
                    if response.status != 200 or response.read() != asset.read_bytes():
                        raise SystemExit(f"Installed workbench asset differs: {url}")
            with urllib.request.urlopen(base + "/api/session", timeout=5) as response:
                if not json.loads(response.read()).get("token"):
                    raise SystemExit("Installed workbench session endpoint failed")
            print(f"Installed wheel HTTP workbench: index + {len(assets)} assets + session OK")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
