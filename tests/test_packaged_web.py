"""A source checkout must stage the workbench that a wheel will ship."""
from html.parser import HTMLParser
from pathlib import Path

import popper


class _AssetRefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        address = values.get("src") if tag == "script" else values.get("href")
        if address and address.startswith("/assets/"):
            self.paths.append(address.lstrip("/"))


def test_packaged_workbench_has_referenced_assets():
    root = Path(popper.__file__).resolve().parent / "web"
    index = root / "index.html"
    assert index.is_file()
    refs = _AssetRefs()
    refs.feed(index.read_text(encoding="utf-8"))
    assert refs.paths
    assert all((root / path).is_file() for path in refs.paths)


def test_packaged_workbench_does_not_ship_old_fake_home_state():
    root = Path(popper.__file__).resolve().parent / "web" / "assets"
    bundles = b"\n".join(path.read_bytes() for path in root.glob("*.js"))
    for invented in (b"wangj", b"0.0 GPU", b"4 pass / 1 warn", b"research-agent-v2",
                     "退出登录".encode(), "侧栏面板".encode(), "专家团".encode()):
        assert invented not in bundles
