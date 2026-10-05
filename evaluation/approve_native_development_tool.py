"""Approve one current development tool; never implicitly recover or retry.

An exclusive intent precedes Workstation construction, which can recover
persistent work. A terminal tool does not mean its model continuation or
research Job has finished. Old receipts reconcile without a Workstation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from evaluation.request_authored_candidate import _quiet_registries
from evaluation.review_development_request import (
    _read_budget, _research_quiet, _validate_arm, _write_new)
from popper.core import ProtocolError, canonical, digest, read_json
from popper.research import agent_revisions
from popper.research.oscillator_campaign import load_campaign
from popper.research.readout import read_research_state
from popper.server import Workstation, validate_agent_tool_arguments
from popper.sessions import AgentSessionStore
from popper.task_context import redact


_ALLOWED_TOOLS = {
    "research.revision_preview", "research.revision_create", "research.revision_execute",
    "research.revision_commit", "research.revision_run", "research.revision_job",
    "research.status", "workspace.file_list", "workspace.file_read"}
_ACTIVE_TOOLS = {"pending_approval", "approved", "running"}
_TERMINAL_TOOLS = {"completed", "failed", "rejected", "cancelled", "interrupted", "invalid"}
_CAMPAIGN = Path(__file__).parent / "runs/agent-oscillator-2026-10-01-v3/formal-campaign-qwen-v2"


def _latest_revision(campaign):
    files = sorted((Path(campaign) / "execution-revisions").glob("*.json"))
    if not files or not files[-1].stem.isdigit():
        raise ProtocolError("current frozen execution revision is missing")
    return int(files[-1].stem)


def _project_snapshot(project, *, approval_call_id=None):
    """Read every session without migration or recovery."""
    with closing(AgentSessionStore.readonly(project)) as store:
        if not store.verify_turn_events()["ok"] or not store.verify_tool_events()["ok"]:
            raise ProtocolError("project Turn/tool event integrity failed")
        sessions = store.list_sessions()
        jobs = [job for session in sessions for job in store.agent_jobs(session["session_id"])]
        turns = [turn for session in sessions for turn in store.turns(session["session_id"])]
        calls = [call for session in sessions for call in store.tool_calls(session["session_id"])]
        queued = store.queued_agent_jobs()
        call = store.get_tool_call(approval_call_id) if approval_call_id else None
        if approval_call_id:
            if (call["status"] != "pending_approval" or call.get("proposed_by") != "agent"
                    or call["tool_name"] not in _ALLOWED_TOOLS):
                raise ProtocolError("only a genuine pending Agent development proposal can be approved")
            source = store.get_turn(call["turn_id"])
            if source["session_id"] != call["session_id"] or source["status"] != "completed":
                raise ProtocolError("approval source Turn is not complete in its explicit session")
            if (queued or any(job["status"] in {"queued", "running"} for job in jobs)
                    or any(turn["status"] == "running" for turn in turns)):
                raise ProtocolError("project has active model jobs; do not recover or approve implicitly")
            for item in calls:
                if item["call_id"] == approval_call_id or item["status"] not in _ACTIVE_TOOLS:
                    continue
                # Valid siblings from this completed Turn need independent
                # decisions; a pending sibling is not recovering executable
                # work. Reject every approved/running or foreign-context peer.
                if (item["status"] != "pending_approval" or item.get("proposed_by") != "agent"
                        or item["session_id"] != call["session_id"] or item["turn_id"] != call["turn_id"]
                        or item["tool_name"] not in _ALLOWED_TOOLS):
                    raise ProtocolError("another project tool awaits approval or execution; settle separately")
                validate_agent_tool_arguments(item["tool_name"], item["arguments"])
        return {"call": call, "jobs": jobs, "tool_calls": calls,
                "job_ids": [job["job_id"] for job in jobs], "turns": turns}


def _check_tool_scope(project, research, call):
    """Validate current Study/version, including unchanged reference receipts."""
    args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
    if not call["tool_name"].startswith("research."):
        return None
    state = read_research_state(research)
    manifest = read_json(Path(research) / "research.json")
    if (not state.get("ok") or args.get("study_id") != manifest.get("study_id")
            or state.get("study", {}).get("study_id") != args["study_id"]):
        raise ProtocolError("tool Study does not belong to this explicit project/research")
    expected = args.get("expected_state_version")
    if call["tool_name"] in {"research.revision_commit", "research.revision_run"}:
        source = agent_revisions._reference_source(call, args, project)
        expected = (source["arguments"].get("expected_state_version")
                    if call["tool_name"] == "research.revision_commit" else source["result"].get("state_version"))
        if type(expected) is not int:
            raise ProtocolError("referenced native receipt has no explicit Study version")
    if expected is not None and expected != state.get("state_version"):
        raise ProtocolError("tool or referenced receipt is stale; no approval or version replacement")
    return {"study_id": args["study_id"], "state_version": state["state_version"]}


def preview_native_development_approval(campaign, run_id, call_id, execution_revision=None):
    """Read-only eligibility, not a live preview or permission."""
    if run_id not in {"popper_loop-1", "popper_loop-2"}:
        raise ProtocolError("loop3 stopped; no implicit restart or unrelated approval")
    campaign = Path(campaign).resolve()
    manifest, run = load_campaign(campaign, run_id)
    if (run.get("arm") != "popper_loop"
            or Path(run["project"]).resolve().parent != campaign / run_id):
        raise ProtocolError("approval must belong to its existing loop project")
    revision = _latest_revision(campaign) if execution_revision is None else execution_revision
    arm, regression = _validate_arm(campaign, manifest, revision)
    project, research = Path(run["project"]).resolve(), Path(run["research"]).resolve()
    _quiet_registries(project, research)
    snapshot = _project_snapshot(project, approval_call_id=call_id)
    _research_quiet(research)
    budget = _read_budget(manifest["budget_ledger"])
    call = snapshot["call"]
    scope = _check_tool_scope(project, research, call)
    siblings = [item for item in snapshot["tool_calls"]
                if item["status"] == "pending_approval" and item["call_id"] != call_id]
    sibling_scopes = [_check_tool_scope(project, research, item) for item in siblings]
    value = {"schema_version": "1.0", "run_id": run_id, "call_id": call_id,
             "session_id": call["session_id"], "project": str(project), "research": str(research),
             "tool_call": call, "scope": scope, "job_ids_before": snapshot["job_ids"],
             "pending_siblings": siblings, "sibling_scopes": sibling_scopes,
             "execution_revision": revision, "execution_snapshot_sha256": arm["execution_snapshot_sha256"],
             "regression": regression, "budget_before": budget, "execution_approved": False}
    return {**value, "preflight_sha256": digest(value)}


def _research_active(research):
    """Inspect persistent research queues without their recovering constructors."""
    for filename, query in (
            ("revision-execution-jobs.sqlite", "SELECT status FROM revision_execution_jobs"),
            ("design-execution-jobs.sqlite", "SELECT status FROM design_execution_jobs"),
            ("study-jobs.sqlite", "SELECT snapshot FROM study_jobs")):
        path = Path(research).resolve() / filename
        if not path.is_file():
            continue
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute(query).fetchall()
        if any((json.loads(row[0])["status"] if filename == "study-jobs.sqlite" else row[0])
               in {"queued", "running"} for row in rows):
            return True
    return False


def _observe(project, research, call_id, before_ids):
    snapshot = _project_snapshot(project)
    with closing(AgentSessionStore.readonly(project)) as store:
        current = store.get_tool_call(call_id)
    jobs = [job for job in snapshot["jobs"] if job["job_id"] not in before_ids]
    turn_ids = {job["turn_id"] for job in jobs}
    return {"tool_call": current, "continuation_jobs": jobs,
            "continuation_turns": [turn for turn in snapshot["turns"] if turn["turn_id"] in turn_ids],
            "new_tool_calls": [call for call in snapshot["tool_calls"] if call["turn_id"] in turn_ids],
            "remaining_pending_tools": [call for call in snapshot["tool_calls"] if call["status"] == "pending_approval"],
            "persistent_active": (any(job["status"] in {"queued", "running"} for job in snapshot["jobs"])
                                  or any(turn["status"] == "running" for turn in snapshot["turns"])
                                  or _research_active(research))}


def _reconcile(campaign, run_id, call_id, intent, settled):
    """Existing intent is a stop, even when no approval/execution occurred."""
    saved = read_json(intent)
    if (saved.get("call_id", saved.get("tool_call", {}).get("call_id")) != call_id
            or saved.get("run_id", run_id) != run_id):
        raise ProtocolError("existing approval receipt identity conflict; no redispatch")
    manifest, run = load_campaign(campaign, run_id)
    with closing(AgentSessionStore.readonly(run["project"])) as store:
        if not store.verify_turn_events()["ok"] or not store.verify_tool_events()["ok"]:
            raise ProtocolError("existing approval project event integrity failed")
        actual = store.get_tool_call(call_id)
    previous = saved.get("tool_call")
    immutable = ("call_id", "session_id", "turn_id", "tool_name", "arguments", "proposed_by")
    if (not isinstance(previous, dict)
            or any(key not in previous or key not in actual for key in immutable)
            or canonical(redact({key: previous[key] for key in immutable}))
            != canonical(redact({key: actual[key] for key in immutable}))):
        raise ProtocolError("existing approval receipt differs from the real immutable tool identity")
    return {"status": "already_attempted_do_not_redispatch", "intent": saved,
            "result": read_json(settled) if settled.exists() else None,
            "actual_tool_call": actual,
            "budget_after": _read_budget(manifest["budget_ledger"], require_known=False),
            "note": "read-only reconciliation; no Workstation, approval, recovery or continuation"}


def approve_native_development_tool(campaign, run_id, call_id, execution_revision=None, *, wait_timeout=900):
    """One reviewed approval; terminal tool and finished reasoning are distinct."""
    campaign = Path(campaign).resolve()
    if run_id not in {"popper_loop-1", "popper_loop-2", "popper_loop-3"}:
        raise ProtocolError("unregistered loop run")
    if (not isinstance(call_id, str) or not call_id.startswith("tcl_")
            or any(c not in "abcdefghijklmnopqrstuvwxyz234567_" for c in call_id)):
        raise ProtocolError("invalid explicit native call ID")
    folder = campaign / run_id / "native-development-approvals"
    target, settled = folder / (call_id + ".json"), folder / (call_id + "-settled.json")
    if target.exists():
        return _reconcile(campaign, run_id, call_id, target, settled)
    if settled.exists():
        raise ProtocolError("orphan approval result must be reconciled; never approve again")
    if type(wait_timeout) not in (int, float) or wait_timeout <= 0:
        raise ProtocolError("wait_timeout must be positive")
    preview = preview_native_development_approval(campaign, run_id, call_id, execution_revision)
    manifest, run = load_campaign(campaign, run_id)
    folder.mkdir(exist_ok=True)
    intent = {**preview, "phase": "intent_claimed_not_approved",
              "approval_source": "human delegated task-scoped approval",
              "numerical_source_unchanged": True, "note": "no automatic confirmation or method advice"}
    try:
        _write_new(target, intent)
    except FileExistsError:
        raise ProtocolError("approval intent already claimed; reconcile without execution") from None
    ws, phase, result = None, "preflight", None
    try:
        checked = preview_native_development_approval(campaign, run_id, call_id, preview["execution_revision"])
        if checked["preflight_sha256"] != preview["preflight_sha256"]:
            raise ProtocolError("tool/context/budget changed after intent; no approval")
        ws = Workstation(("127.0.0.1", 0), run["project"], trusted_local=True,
                         research_runs=[run["research"]], chat_base_url=manifest["base_url"],
                         chat_model=manifest["model"], evaluation_budget_path=manifest["budget_ledger"])
        checked = preview_native_development_approval(campaign, run_id, call_id, preview["execution_revision"])
        if checked["preflight_sha256"] != preview["preflight_sha256"]:
            raise ProtocolError("tool/context/budget changed before approval")
        public = ws._agent_tool_public(checked["tool_call"])
        if public.get("preview", {}).get("ok") is not True:
            raise ProtocolError("invalid current scope/version/digest preview; no approval")
        _write_new(folder / (call_id + "-reviewed.json"), {
            "phase": "reviewed_not_approved", "tool_call": checked["tool_call"],
            "preview": public["preview"], "preflight_sha256": preview["preflight_sha256"],
            "approval_source": "human task delegation, not Agent approval", "numerical_source_unchanged": True})
        phase = "approval"
        ws.decide_agent_tool(call_id, "approve", "User delegated task-scoped approval; exact current native proposal reviewed, no numerical changes")
        phase = "execution"
        result = ws.execute_agent_tool(call_id)
        phase, deadline = "waiting", time.monotonic() + wait_timeout
        while True:
            threads_active = bool(ws.wait_for_jobs(timeout=1))
            observed = _observe(run["project"], run["research"], call_id, set(preview["job_ids_before"]))
            active = threads_active or observed["persistent_active"]
            if not active:
                if observed["tool_call"]["status"] not in _TERMINAL_TOOLS:
                    raise ProtocolError("approved tool lacks a real terminal receipt")
                status = "settled_needs_native_approval" if (observed.get("remaining_pending_tools") or any(
                    call["status"] == "pending_approval" for call in observed["new_tool_calls"])) else "settled"
                break
            if time.monotonic() >= deadline:
                status = "timed_out_uncertain_no_retry"
                break
            time.sleep(0.05)
        budget = _read_budget(manifest["budget_ledger"], require_known=False)
        if budget["status"]["unresolved_requests"]:
            status = "timed_out_uncertain_no_retry" if active else "usage_unknown_blocked"
        record = {**observed, "status": status,
                  "phase": "settled" if status.startswith("settled") else "uncertain_not_settled",
                  "run_id": run_id, "call_id": call_id, "result": result,
                  "reviewed_preview": public["preview"], "budget_after": budget,
                  "approval_source": "human task delegation, not Agent approval",
                  "numerical_source_unchanged": True,
                  "note": "tool outcome, research Job and model continuation are separate; no retry or cancellation"}
        _write_new(settled, record)
        return record
    except Exception as error:
        if not settled.exists():
            _write_new(settled, {"status": "preflight_failed_no_approval" if phase == "preflight" else "approval_uncertain_no_retry",
                                "phase": "uncertain_not_settled", "failure_phase": phase,
                                "run_id": run_id, "call_id": call_id, "result": result,
                                "error": {"type": type(error).__name__, "message": str(error)},
                                "note": "intent retained; no automatic recovery, approval, execution or retry"})
        raise
    finally:
        if ws is not None:
            ws.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", choices=[f"popper_loop-{i}" for i in range(1, 4)])
    parser.add_argument("call_id")
    parser.add_argument("--execution-revision", type=int, default=None,
                        help="defaults to latest frozen revision; old arms are rejected")
    args = parser.parse_args()
    try:
        result = approve_native_development_tool(_CAMPAIGN, args.run_id, args.call_id, args.execution_revision)
    except Exception as error:
        print(canonical(redact({"status": "stopped_no_retry", "error": {
            "type": type(error).__name__, "message": str(error)}})), flush=True)
        return 1
    print(result["status"], flush=True)
    return 0 if result["status"].startswith(("settled", "already_attempted")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
