"""V1.5 light Web-shell glue (standard-library placeholder).

Returns a static HTML skeleton that renders a GET view from a Python list, with data
inlined as JSON so there is no fetch dependency. V1.5 will migrate to React; keep this
a static-file placeholder that only renders genuine state, never fabricated numbers.
"""
from __future__ import annotations

import html
import json

_TEMPLATE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem; padding: 0 1rem; }}
h1 {{ font-size: 1.4rem; }}
#shell li {{ margin: 0.25rem 0; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div id="shell"></div>
<script>
// V1.5 将迁移到 React，当前为标准库占位。数据由 Python 内联，避免 fetch 依赖。
(function () {{
  var items = {items_json};
  var shell = document.getElementById("shell");
  var ul = document.createElement("ul");
  items.forEach(function (item) {{
    var li = document.createElement("li");
    li.textContent = (item && typeof item === "object" && ("title" in item))
      ? String(item.title)
      : JSON.stringify(item);
    ul.appendChild(li);
  }});
  shell.appendChild(ul);
}})();
</script>
</body>
</html>
"""


def render_shell(title, items):
    """Render a minimal list view from a Python `items` sequence."""
    items_json = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.format(title=html.escape(str(title)), items_json=items_json)