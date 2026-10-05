"""Exercise the installed wheel's real Windows ConPTY through its HTTP API."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_installed_terminal.py <pip --target directory>")
    if os.name != "nt" or importlib.util.find_spec("winpty") is None:
        raise SystemExit("Windows ConPTY + pywinpty are required; refusing a skipped smoke test")
    installed = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(installed))
    import popper
    from popper.demo import initialize_demo
    from popper.server import Workstation

    if not Path(popper.__file__).resolve().is_relative_to(installed):
        raise SystemExit("Terminal smoke imported checkout instead of installed wheel")
    with tempfile.TemporaryDirectory(prefix="popper-wheel-terminal-") as temporary:
        project = Path(temporary) / "project"
        initialize_demo(project, "ode-convergence")
        server = Workstation(("127.0.0.1", 0), project, trusted_local=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/api/session", timeout=5) as response:
                token = json.loads(response.read())["token"]

            def request(payload: dict) -> dict:
                req = urllib.request.Request(
                    base + "/api/terminal", data=json.dumps(payload).encode("utf-8"),
                    headers={"X-Popper-Token": token, "Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.loads(response.read())

            capability = server.state_payload()["terminal_capability"]
            if not capability["available"]:
                raise SystemExit(f"Installed terminal unavailable: {capability['reason']}")
            created = request({"op": "create", "cols": 90, "rows": 28})
            session_id = created["id"]
            try:
                if created["root"] != str(project.resolve()):
                    raise SystemExit("Installed terminal opened in the wrong workspace")
                request({"op": "input", "id": session_id,
                         "data": "Set-Content -LiteralPath popper-wheel-terminal.txt -Value executed\r\n"})
                marker = project / "popper-wheel-terminal.txt"
                cursor = created["cursor"]
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    frame = request({"op": "read", "id": session_id, "cursor": cursor})
                    cursor = frame["cursor"]
                    try:
                        if marker.read_text(encoding="utf-8-sig").strip() == "executed":
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    raise SystemExit("Installed terminal did not execute the PowerShell command")
            finally:
                closed = request({"op": "close", "id": session_id})
                if closed.get("alive") is not False:
                    raise SystemExit("Installed terminal did not close")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("Installed wheel HTTP terminal: real ConPTY PowerShell command and cleanup OK")


if __name__ == "__main__":
    main()
