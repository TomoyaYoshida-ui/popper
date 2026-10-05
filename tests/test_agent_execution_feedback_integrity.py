"""Authoritative feedback refuses foreign or altered real execution evidence.

Only navigation is a minimal read-only locator. Projects, Study/Revision
identities, executions, observations, evaluation artifacts and queue events
are genuine isolated fixtures; no model or confirmation resource is used.
"""
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from popper.agent_execution_feedback import execution_feedback
from popper.core import ProtocolError, canonical, file_hash, read_json
from popper.eventlog import GENESIS_HASH, chained_body
from popper.research.controller import ResearchController
from popper.research.execution_jobs import RevisionExecutionJobStore
from popper.research.readout import read_research_state
from popper.research.revision_drafts import preview_initial_revision, submit_initial_revision
from popper.research.study_jobs import StudyJobStore, preview_study_action
from popper.workspace_files import ProjectFiles
from tests.test_server_chat import _make_project


class ProjectLocator:
    def __init__(self, runs):
        self.runs = {read_json(run / "research.json")["study_id"]: run for run in runs}

    def research_run_for_study(self, study_id):
        return self.runs[study_id]

    def _visible_research_runs(self, root):
        return [run for run in self.runs.values()
                if Path(read_json(run / "research.json")["project"]).resolve() == Path(root).resolve()]


def files_snapshot(root):
    return {str(path.relative_to(root)): file_hash(path)
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def development(tmp_path):
    project, run = _make_project(tmp_path / "project"), tmp_path / "research"
    ResearchController.initialize(project, run)
    preview = preview_study_action(run, "run", 1)
    arguments = {"study_id": preview["study_id"], "action": "run", "max_steps": 1,
                 "approval_sha256": preview["approval_sha256"]}
    queue = StudyJobStore(run)
    try:
        original = queue.enqueue("tool-tcl_feedback_integrity", arguments)
        queue.claim(original["job_id"])
        controller = ResearchController(run)
        try:
            result = controller.run(trusted_local=True, max_steps=1, auto_confirm=False)
        finally:
            controller.close()
        queue.finish(original["job_id"], "succeeded", result={"phase": result["phase"],
            "observation_ids": [row["observation_id"] for row in result["observations"]],
            "claim_ids": [row["claim_id"] for row in result["claims"]]})
        assert queue.verify()["ok"]
    finally:
        queue.close()
    assert result["observations"] and not result["claims"]
    return {"project": project, "run": run, "locator": ProjectLocator([run]),
        "call": {"tool_name": "research.study_action", "status": "completed",
                 "call_id": "tcl_feedback_integrity", "turn_id": "trn_feedback_integrity",
                 "arguments": arguments, "result": {"job": original}}}


@pytest.fixture
def revision(development):
    project, run = development["project"], development["run"]
    state, manifest = read_research_state(run), read_json(run / "research.json")
    hypothesis = next(row for row in state["hypotheses"] if row["status"] == "untested"
                      and row["hypothesis_id"] != manifest["control_hypothesis_id"])
    design = next(row for row in state["designs"] if row["hypothesis_id"] == hypothesis["hypothesis_id"])
    source = ProjectFiles(project).read("model.py")
    specification = {"study_id": manifest["study_id"], "expected_state_version": state["state_version"],
        "hypothesis_id": hypothesis["hypothesis_id"], "design_id": design["design_id"],
        "path": "model.py", "expected_sha256": source["sha256"],
        "replacement": source["content"].replace("def main", "# Isolated integrity fixture\ndef main")}
    preview = preview_initial_revision(run, project, specification)
    created = submit_initial_revision(run, project, specification, preview["proposal_sha256"], actor="fixture_agent")
    queue = RevisionExecutionJobStore(run / "revision-execution-jobs.sqlite")
    try:
        original = queue.enqueue("approved:" + created["revision_id"], created["revision_id"])
        queue.claim(original["job_id"])
        controller = ResearchController(run)
        try:
            result = controller.execute_revision(created["revision_id"])
        finally:
            controller.close()
        queue.finish(original["job_id"], run_id=result["observation"]["run_id"])
        assert queue.verify()["ok"]
    finally:
        queue.close()
    call = {"tool_name": "research.revision_execute", "status": "completed",
        "call_id": "tcl_revision_integrity", "turn_id": "trn_revision_integrity",
        "arguments": {"study_id": manifest["study_id"], "revision_id": created["revision_id"],
                      "manifest_sha256": created["manifest_sha256"]},
        "result": {"job": original, "manifest_sha256": created["manifest_sha256"]}}
    return {**development, "call": call}


def read_feedback(fixture):
    return execution_feedback(fixture["locator"], fixture["project"], fixture["call"])


def test_healthy_study_feedback_is_readonly_and_matches_real_observations(development):
    before = files_snapshot(development["project"].parent)
    feedback = read_feedback(development)
    actual = read_research_state(development["run"])
    assert feedback["terminal"] and feedback["execution_status"] == "succeeded"
    assert {row["observation_id"]: row["value"] for row in feedback["observations"]} == {
        row["observation_id"]: row["value"] for row in actual["observations"]}
    assert feedback["claim_ids"] == [] and not actual["claims"]
    assert files_snapshot(development["project"].parent) == before


def test_foreign_study_or_project_cannot_supply_feedback(development, tmp_path):
    other, foreign_run = _make_project(tmp_path / "other"), tmp_path / "foreign-research"
    ResearchController.initialize(other, foreign_run)
    locator = ProjectLocator([development["run"], foreign_run])
    before = files_snapshot(tmp_path)
    with pytest.raises(ProtocolError, match="不属于此项目"):
        execution_feedback(locator, other, development["call"])
    foreign_call = copy.deepcopy(development["call"])
    foreign_call["arguments"]["study_id"] = read_json(foreign_run / "research.json")["study_id"]
    with pytest.raises(ProtocolError, match="不属于此项目"):
        execution_feedback(locator, development["project"], foreign_call)
    assert files_snapshot(tmp_path) == before


@pytest.mark.parametrize("missing", ["study-jobs.sqlite", "research.sqlite"])
def test_missing_database_is_refused_and_not_initialized(development, missing):
    path = development["run"] / missing
    path.rename(path.with_suffix(path.suffix + ".retained"))
    before = files_snapshot(development["project"].parent)
    with pytest.raises((ProtocolError, FileNotFoundError, sqlite3.DatabaseError)):
        read_feedback(development)
    assert not path.exists()
    assert files_snapshot(development["project"].parent) == before


def test_changed_observation_snapshot_value_is_not_sent_as_a_real_result(development):
    state = read_research_state(development["run"])
    observation = state["observations"][0]
    with sqlite3.connect(development["run"] / "research.sqlite") as connection:
        payload = json.loads(connection.execute("SELECT payload FROM snapshots WHERE kind='observation' AND entity_id=?",
                                               (observation["observation_id"],)).fetchone()[0])
        payload["value"] = float(payload["value"]) + 123
        connection.execute("UPDATE snapshots SET payload=? WHERE kind='observation' AND entity_id=?",
                           (canonical(payload), observation["observation_id"]))
    with pytest.raises(ProtocolError):
        read_feedback(development)


def test_changed_evaluation_artifact_is_refused(development):
    observation = read_research_state(development["run"])["observations"][0]
    path = Path(observation["artifact_id"])
    assert path.is_relative_to(development["run"])
    artifact = read_json(path)
    artifact["tampered_fixture_marker"] = True
    path.write_text(canonical(artifact), encoding="utf-8")
    with pytest.raises(ProtocolError):
        read_feedback(development)


def test_healthy_revision_feedback_matches_genuine_run_and_is_readonly(revision):
    before = files_snapshot(revision["project"].parent)
    feedback = read_feedback(revision)
    assert feedback["terminal"] and feedback["execution_status"] == "succeeded"
    assert feedback["run"]["status"] == "succeeded"
    assert feedback["observations"]
    assert all(row["run_id"] == feedback["run"]["run_id"] for row in feedback["observations"])
    assert files_snapshot(revision["project"].parent) == before


@pytest.mark.parametrize("field,value", [
    ("run_id", "RUN-foreign-fixture"),
    ("error", "forged successful-Job error"),
    ("worktree_id", "approved:REV-foreign-fixture"),
])
def test_revision_queue_outcome_and_identity_must_match_real_events(revision, field, value):
    path = revision["run"] / "revision-execution-jobs.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE revision_execution_jobs SET {field}=? WHERE job_id=?",
                           (value, revision["call"]["result"]["job"]["job_id"]))
    with pytest.raises(ProtocolError, match="与执行事件不一致"):
        read_feedback(revision)


def test_revision_event_payload_tampering_is_rejected(revision):
    with sqlite3.connect(revision["run"] / "revision-execution-jobs.sqlite") as connection:
        connection.execute("UPDATE revision_execution_job_events SET payload='{}' WHERE event_type='succeeded'")
    with pytest.raises(ProtocolError):
        read_feedback(revision)


def test_valid_queue_chain_cannot_bind_revision_to_another_real_run(revision):
    state = read_research_state(revision["run"])
    own_revision = revision["call"]["arguments"]["revision_id"]
    other_run = next(row for row in state["runs"] if row["revision_id"] != own_revision)
    path = revision["run"] / "revision-execution-jobs.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE revision_execution_jobs SET run_id=? WHERE job_id=?",
                           (other_run["run_id"], revision["call"]["result"]["job"]["job_id"]))
        previous = GENESIS_HASH
        for seq, job_id, event, payload, created in connection.execute(
                "SELECT seq,job_id,event_type,payload,created FROM revision_execution_job_events ORDER BY seq").fetchall():
            payload = json.loads(payload)
            if event == "succeeded":
                payload["run_id"] = other_run["run_id"]
            _, event_hash = chained_body({"job_id": job_id, "event_type": event, "payload": payload,
                                           "created": created}, previous)
            connection.execute("UPDATE revision_execution_job_events SET payload=?,previous_hash=?,hash=? WHERE seq=?",
                               (canonical(payload), previous, event_hash, seq))
            previous = event_hash
    queue = RevisionExecutionJobStore.readonly(path)
    try:
        assert queue.verify()["ok"], "this case specifically reaches Run ownership validation"
    finally:
        queue.close()
    with pytest.raises(ProtocolError, match="Run 归属或源码版本不一致"):
        read_feedback(revision)


def test_missing_revision_queue_does_not_create_a_replacement(revision):
    path = revision["run"] / "revision-execution-jobs.sqlite"
    path.rename(path.with_suffix(path.suffix + ".retained"))
    before = files_snapshot(revision["project"].parent)
    with pytest.raises(FileNotFoundError):
        read_feedback(revision)
    assert not path.exists()
    assert files_snapshot(revision["project"].parent) == before
