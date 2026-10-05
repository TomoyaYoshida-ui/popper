import json
import shutil
import threading
import urllib.request
from pathlib import Path

import pytest

from popper.core import ProtocolError, initialize, read_json
from popper.research.controller import ResearchController
from popper.research.project_prep import (commit_project_preparation,
                                          preview_project_preparation)
from popper.server import Workstation


SOURCE = Path(__file__).resolve().parents[1] / "examples" / "ode-convergence"
SPEC = read_json(SOURCE / "experiment.json")
INPUTS = {name for name in ("model.py", "train.json", "dev.json", "test.json")}


def _copy_inputs(target):
    target.mkdir()
    for name in INPUTS:
        shutil.copyfile(SOURCE / name, target / name)
    shutil.copyfile(SOURCE / "experiment.json", target / "experiment.json")


def test_import_project_and_switch_workstation(tmp_path):
    primary = tmp_path / "primary"
    _copy_inputs(primary)
    initialize(primary)
    target = tmp_path / "imported"
    _copy_inputs(target)
    server = Workstation(("127.0.0.1", 0), primary, trusted_local=True)
    try:
        preview = server.preview_project_setup(str(target), None, {})
        assert preview["domain_pack"] == "convergence-order-v1"
        assert preview["repeat_unit"] == "independent_run"
        result = server.prepare_project(str(target), None, {}, preview["approval_sha256"])
        assert result["status"] == "registered"
        assert server.workspace.root == target.resolve()
        assert server.project == target.resolve()
        assert server.preview_study_initialization("study-1", 4)["project"] == str(target.resolve())
        assert server.prepare_project(str(target), None, {}, preview["approval_sha256"])["workspace_id"] == result["workspace_id"]
    finally:
        server.server_close()


def test_imported_project_does_not_show_previous_workspace_study(tmp_path):
    primary = tmp_path / "primary"
    _copy_inputs(primary)
    initialize(primary)
    previous_run = tmp_path / "previous-run"
    ResearchController.initialize(primary, previous_run, budget_cap=4)
    target = tmp_path / "imported"
    _copy_inputs(target)
    server = Workstation(("127.0.0.1", 0), primary,
                         research_runs=[previous_run], trusted_local=True)
    try:
        assert len(server.research_payload()["runs"]) == 1
        previous_id = read_json(previous_run / "research.json")["study_id"]
        assert previous_id in server._chat_system_prompt()
        preview = server.preview_project_setup(str(target), None, {})
        server.prepare_project(str(target), None, {}, preview["approval_sha256"])
        assert server.research_payload()["runs"] == []
        assert previous_id not in server._chat_system_prompt()
        denied = server._agent_tool_public({"tool_name": "research.status",
            "arguments": {"study_id": previous_id}})
        assert denied["preview"]["ok"] is False
        assert "当前工作区" in denied["preview"]["reason"]
    finally:
        server.server_close()


def test_new_project_from_protocol_and_registered_inputs(tmp_path):
    target = tmp_path / "new-project"
    files = {name: (SOURCE / name).read_text(encoding="utf-8") for name in INPUTS}
    preview = preview_project_preparation(str(target), SPEC, files)
    assert not target.exists()
    assert not preview["already_initialized"]
    with pytest.raises(ProtocolError):
        commit_project_preparation(str(target), SPEC, files, "0" * 64)
    assert not target.exists()
    committed = commit_project_preparation(str(target), SPEC, files, preview["approval_sha256"])
    assert committed["status"] == "registered"
    assert (target / ".popper" / "state.db").is_file()
    assert commit_project_preparation(str(target), SPEC, files, preview["approval_sha256"])["already_initialized"]
    (target / "model.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ProtocolError):
        preview_project_preparation(str(target), SPEC, files)


def test_incomplete_initialization_keeps_site_for_audit(tmp_path):
    target = tmp_path / "interrupted"
    _copy_inputs(target)
    (target / ".popper").mkdir()
    (target / ".popper" / "failure.txt").write_text("retain", encoding="utf-8")
    with pytest.raises(ProtocolError, match="未完成"):
        preview_project_preparation(str(target))
    assert (target / ".popper" / "failure.txt").read_text(encoding="utf-8") == "retain"


def test_project_preparation_http_preview_and_commit(tmp_path):
    primary = tmp_path / "primary"
    _copy_inputs(primary)
    initialize(primary)
    target = tmp_path / "imported"
    _copy_inputs(target)
    server = Workstation(("127.0.0.1", 0), primary, trusted_local=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/v1/research/projects"

    def post(url, body):
        request = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Popper-Token": server.token})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())["data"]

    try:
        body = {"schema_version": "1.0", "project_path": str(target),
                "protocol": None, "files": {}}
        status, preview = post(base + "/preview", body)
        assert status == 200 and preview["candidate_count"] == 2
        status, committed = post(base, {**body,
            "approval_sha256": preview["approval_sha256"]})
        assert status == 201 and committed["workspace_id"]
        assert server.project == target.resolve()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
