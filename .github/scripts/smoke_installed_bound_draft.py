"""Exercise non-ML evidence and human-review gates from an installed wheel."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


def _cli(main, *args):
    output = StringIO()
    with redirect_stdout(output):
        status = main(list(args))
    if status != 0:
        raise SystemExit(f"Installed CLI failed: {args}: {output.getvalue()}")
    return json.loads(output.getvalue())


def _json(url, token=None, payload=None):
    headers = {"X-Popper-Token": token} if token else {}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_installed_bound_draft.py <pip --target directory>")
    installed = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(installed))
    import popper
    from popper.cli import main as cli_main
    from popper.server import Workstation

    if not Path(popper.__file__).resolve().is_relative_to(installed):
        raise SystemExit("Bound-draft smoke imported checkout instead of installed wheel")
    with tempfile.TemporaryDirectory(prefix="popper-wheel-bound-") as temporary:
        root = Path(temporary)
        project, run = root / "ode", root / "research"
        _cli(cli_main, "demo", "init", str(project), "--case", "ode-convergence")
        _cli(cli_main, "research", "init", str(project), str(run))
        _cli(cli_main, "research", "run", str(run), "--trusted-local",
             "--max-steps", "2")
        confirmed = _cli(cli_main, "research", "confirm", str(run), "--trusted-local")
        if len(confirmed.get("claims", [])) != 1:
            raise SystemExit("Installed non-ML confirmation did not create a Claim")

        server = Workstation(("127.0.0.1", 0), project,
                             research_runs=[run], trusted_local=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            token = _json(base + "/api/session")["token"]
            study_id = _json(base + "/api/research", token)["runs"][0]["manifest"]["study_id"]
            endpoint = base + f"/api/v1/research/{study_id}"
            evidence = _json(endpoint + "/evidence-preview", token)["data"]
            bindings = evidence["bundle"]["bindings"]
            if len(bindings) != 1:
                raise SystemExit("Installed evidence endpoint did not bind the Claim")
            spec = {"schema_version": "1.0", "title": "数值收敛研究草稿",
                    "sections": [{"heading": "结果", "blocks": [
                        {"kind": "claim", "claim_id": bindings[0]["claim_id"]}]}]}
            draft_url = endpoint + "/bound-drafts"
            created = _json(draft_url, token,
                            {"schema_version": "1.0", "spec": spec})["data"]
            if created["status"] != "draft_needs_review":
                raise SystemExit("Installed draft endpoint returned an unexpected status")
            listed = _json(draft_url, token)["data"]["drafts"]
            if len(listed) != 1 or not listed[0]["ok"]:
                raise SystemExit("Installed workbench cannot rediscover its bound draft")
            detail_url = draft_url + "/" + created["draft_id"]
            reviewed = _json(detail_url + "/review", token,
                             {"schema_version": "1.0",
                              "reason": "已人工检查演示稿件"})["data"]
            detail = _json(detail_url, token)["data"]
            if reviewed["status"] != "reviewed" or detail["flow_state"] != "reviewed":
                raise SystemExit("Installed workbench did not persist human review")
            _cli(cli_main, "research", "paper", "submit-bound", str(project), str(run),
                 created["evidence_dir"], "--manuscript", created["manuscript_path"],
                 "--draft-spec-json", created["draft_spec_path"])
            if _json(detail_url, token)["data"]["flow_state"] != "submitted":
                raise SystemExit("Installed CLI submission was not visible in workbench")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("Installed wheel non-ML Claim→evidence→draft→review→submit OK")


if __name__ == "__main__":
    main()
