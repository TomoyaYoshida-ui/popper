import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from popper import sandbox
from popper.core import Experiment, ProtocolError, initialize, read_json
from popper.research.controller import ResearchController
from popper.research.readout import read_research_state
from popper.research.study_jobs import StudyJobStore, preview_study_action
from popper.server import Workstation

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def make_project(tmp_path, domain):
    project = tmp_path / "project"
    project.mkdir()
    source = EXAMPLES / domain
    spec = read_json(source / "experiment.json")
    for name in {"experiment.json", spec["train"], spec["dev"], spec["test"],
                 *spec["code_files"], *spec.get("provenance_files", [])}:
        destination = project / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    initialize(project)
    run_dir = tmp_path / "research"
    ResearchController.initialize(project, run_dir)
    return project, run_dir


@pytest.mark.parametrize("domain", ["ode-convergence", "quadratic"])
def test_http_study_run_confirm_claim_and_replay(tmp_path, domain):
    if not sandbox.available():
        pytest.skip("requires an available OS sandbox")
    project, run_dir = make_project(tmp_path, domain)
    server = Workstation(("127.0.0.1", 0), project, trusted_local=True, research_runs=[run_dir])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    study_id = read_json(run_dir / "research.json")["study_id"]
    url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/research/{study_id}/lifecycle"

    def post(suffix, payload, auth=True):
        request = urllib.request.Request(url + suffix, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **({"X-Popper-Token": server.token} if auth else {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        for action in ("run", "confirm"):
            body = {"schema_version": "1.0", "action": action, "max_steps": 2}
            assert post("/preview", body, auth=False)[0] == 403
            code, preview = post("/preview", body)
            assert code == 200, preview
            execution = {**body, "approval_sha256": preview["data"]["approval_sha256"],
                         "idempotency_key": f"http-study-{action}"}
            assert post("/jobs", {**execution, "approval_sha256": "0" * 64})[0] == 400
            code, accepted = post("/jobs", execution)
            assert code == 202, accepted
            assert server.wait_for_jobs(timeout=120) == 0
            jobs = server.study_jobs_payload(study_id)["jobs"]
            job = next(row for row in jobs if row["job_id"] == accepted["data"]["job_id"])
            assert job["status"] == "succeeded", job
            code, replay = post("/jobs", execution)
            assert code == 202 and replay["data"]["job_id"] == job["job_id"]
        state = read_research_state(run_dir)
        assert state["study"]["status"] == "concluded"
        assert len(state["claims"]) == 1
        assert state["budget"]["reserved"] == 0
        assert state["experiment_graph"]["integrity"]["ok"]
        assert post("/preview", {"schema_version": "1.0", "action": "confirm", "max_steps": 1})[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_autopilot_preview_keeps_the_same_approval_and_excludes_confirmation(tmp_path):
    _, run_dir = make_project(tmp_path, "ode-convergence")
    code_file = read_json(run_dir.parent / "project" / "experiment.json")["code_files"][0]
    requested = {"max_dev_runs": 2, "max_model_tokens": 1000,
                 "max_wall_clock_minutes": 30, "code_files": [code_file]}
    preview = preview_study_action(run_dir, "autopilot", 2, requested)
    assert preview["scope"] == "dev_search"
    assert "confirm" not in preview["envelope"]["allowed_actions"]
    replay = preview_study_action(run_dir, "autopilot", 2, preview["envelope"])
    assert replay["approval_sha256"] == preview["approval_sha256"]
    with pytest.raises(ProtocolError, match="开发运行上限"):
        preview_study_action(run_dir, "autopilot", 2, {**requested, "max_dev_runs": 3})


def test_queue_cancellation_recovery_and_tamper(tmp_path):
    project, run_dir = make_project(tmp_path, "ode-convergence")
    preview = preview_study_action(run_dir, "run", 1)
    assert not (run_dir / "study-jobs.sqlite").exists()
    server = Workstation(("127.0.0.1", 0), project, trusted_local=True, research_runs=[run_dir])
    try:
        with patch.object(server, "_start_job"):
            job = server.enqueue_study_action(preview["study_id"], "run", 1,
                                             preview["approval_sha256"], "queued-test-1")
        assert server.cancel_study_job(preview["study_id"], job["job_id"])["status"] == "cancelled"
        with patch.object(server, "_start_job"):
            job = server.enqueue_study_action(preview["study_id"], "run", 1,
                                             preview["approval_sha256"], "queued-test-2")
        store = StudyJobStore(run_dir)
        try:
            store.claim(job["job_id"])
        finally:
            store.close()
        server._recover_study_jobs()
        assert server.study_jobs_payload(preview["study_id"])["jobs"][-1]["status"] == "interrupted"
        store = StudyJobStore(run_dir)
        try:
            with store.db.transaction(write=True):
                store.db.execute("UPDATE study_jobs SET snapshot=REPLACE(snapshot,'interrupted','queued') WHERE job_id=?",
                                 (job["job_id"],))
            with pytest.raises(ProtocolError, match="快照"):
                store.verify()
        finally:
            store.close()
    finally:
        server.server_close()


def test_confirm_preview_recovers_pre_consumption_freeze(tmp_path):
    if not sandbox.available():
        pytest.skip("requires an available OS sandbox")
    project, run_dir = make_project(tmp_path, "ode-convergence")
    controller = ResearchController(run_dir)
    try:
        controller.run(sandboxed=True, max_steps=2)
    finally:
        controller.close()
    exp = Experiment(project)
    try:
        exp.freeze()
        assert exp.state()["phase"] == "frozen"
        assert not exp.state().get("test_consumed")
    finally:
        exp.close()
    preview = preview_study_action(run_dir, "confirm", 1)
    assert preview["scope"] == "registered_independent_confirmation"
    with pytest.raises(ProtocolError, match="阶段"):
        preview_study_action(run_dir, "run", 1)
