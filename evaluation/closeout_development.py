"""Read-only cohort closeout, based on authoritative objects, not old audit files.

This command never launches a model, solver, queue recovery or confirmation.
Only a previously nonexistent output directory is written. The manifest is also
usable by retained-artifact re-scoring and explicitly approved isolated replay;
producing it does not itself constitute solver replay or independent validation.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from popper.core import Experiment, ProtocolError, canonical, digest, file_hash, read_json
from popper.research.execution_jobs import RevisionExecutionJobStore
from popper.research.readout import read_research_state
from popper.research.revisions import RevisionStore
from popper.research.study_jobs import preview_study_action
from popper.sessions import AgentSessionStore, SESSIONS_REL
from popper.task_context import load_input, redact


SCHEMA_VERSION = 1
_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_LIMITATIONS = [
    "Development evidence only; no held-out performance or scientific gain verdict.",
    "An execution success is not trajectory validity or attainment of the improvement threshold.",
    "Energy drift is measured only at prescribed trajectory samples, not all internal steps.",
    "Trusted RHS queries do not measure all arithmetic work or equalize model cost.",
    "Historical failed proposals and human interventions remain part of the record.",
    "Execution revisions and supplementary feedback may differ between runs; this is not a clean equal-configuration comparison.",
    "Configured model identity is not an independent provider identity attestation.",
    "Zero catalog cost is not proof that the provider charged no money.",
    "No missing prediction, hypothesis, Observation or Claim is inferred or backfilled.",
    "CSV failed_cases counts scored trajectory failures only; job and host case execution states are separate. Missing count evidence stays blank.",
    "Producing this report is neither solver re-execution nor third-party independent verification.",
]


def _readonly_connection(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ProtocolError(f"Required read-only database is missing: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _budget_snapshot(path):
    """Do not construct AgentEvaluationBudget: its initializer executes DDL."""
    with closing(_readonly_connection(path)) as db:
        db.execute("BEGIN")
        policy_row = db.execute("SELECT max_requests,max_tokens FROM policy WHERE id=1").fetchone()
        if policy_row is None:
            raise ProtocolError("Missing persisted campaign policy")
        policy = dict(policy_row)
        if any(type(value) is not int or value <= 0 for value in policy.values()):
            raise ProtocolError("Invalid persisted campaign policy")
        requests = []
        for row in db.execute("SELECT id,snapshot,sha256,reserved,state,used FROM requests ORDER BY rowid"):
            snapshot = json.loads(row["snapshot"])
            if digest(snapshot) != row["sha256"]:
                raise ProtocolError("Campaign input snapshot digest changed")
            requests.append({"request_id": row["id"], "snapshot_sha256": row["sha256"],
                "reserved_tokens": row["reserved"], "state": row["state"], "used_tokens": row["used"],
                "request_limits": redact(snapshot.get("request_limits", {}))})
        aggregates = []
        for row in db.execute("SELECT id,receipt,sha256,adjustment FROM aggregate_usage ORDER BY id"):
            receipt = json.loads(row["receipt"])
            if digest(receipt) != row["sha256"]:
                raise ProtocolError("Campaign aggregate usage receipt changed")
            members = [item[0] for item in db.execute(
                "SELECT request_id FROM aggregate_members WHERE aggregate_id=? ORDER BY request_id", (row["id"],))]
            if sorted(receipt["request_ids"]) != members:
                raise ProtocolError("Campaign aggregate coverage changed")
            aggregates.append({"receipt_id": row["id"], "receipt_sha256": row["sha256"],
                "adjustment_tokens": row["adjustment"], "request_ids": members})
        amendments = []
        for row in db.execute("SELECT id,receipt,sha256 FROM policy_amendments ORDER BY id"):
            receipt = json.loads(row["receipt"])
            if digest(receipt) != row["sha256"]:
                raise ProtocolError("Campaign policy authorization receipt changed")
            amendments.append({"amendment_id": row["id"], "receipt_sha256": row["sha256"],
                "previous_policy": receipt["expected_policy"], "new_policy": receipt["new_policy"]})
    adjustment = sum(item["adjustment_tokens"] for item in aggregates)
    used = sum(item["used_tokens"] or 0 for item in requests) + adjustment
    known = {"settled", "aggregate_reconciled", "usage_over_reservation"}
    result = {"policy": policy, "requests": requests, "aggregates": aggregates, "amendments": amendments,
        "request_count": len(requests), "used_tokens": used,
        "unresolved_requests": sum(item["state"] not in known for item in requests),
        "over_reservation_requests": sum(item["state"] == "usage_over_reservation" for item in requests),
        "unallocated_usage_requests": sum(item["state"] == "aggregate_reconciled" for item in requests),
        "aggregate_adjustment_tokens": adjustment,
        "remaining_calls": policy["max_requests"] - len(requests),
        "remaining_tokens": policy["max_tokens"] - used,
        "source": {"path": str(Path(path).resolve()), "mode": "read_only_transaction"}}
    result["snapshot_sha256"] = digest(result)
    return result


def _immutable_revision(root, revision_id):
    """Use verify without mkdir: RevisionStore.__init__ is a writing constructor."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ProtocolError("Execution queue has no immutable revision directory")
    revisions = RevisionStore.__new__(RevisionStore)
    revisions.root = root
    revision = revisions.verify(revision_id)
    return {"revision_id": revision_id, "manifest_path": str(root / revision_id / "revision.json"),
        "manifest_sha256": file_hash(root / revision_id / "revision.json"),
        "identity": revision["identity"], "files": revision["files"]}


def _queue_snapshot(research):
    path = Path(research) / "revision-execution-jobs.sqlite"
    if not path.is_file():
        return {"exists": False, "jobs": [], "integrity": None, "snapshot_sha256": digest([])}
    with closing(RevisionExecutionJobStore.readonly(path)) as queue:
        integrity = queue.verify()
        if not integrity["ok"]:
            raise ProtocolError("Revision execution queue integrity failed")
        jobs = queue.list()
        histories = {job["job_id"]: queue.history(job["job_id"]) for job in jobs}
    # verify() checks status replay; also check the immutable identity of enqueue.
    for job in jobs:
        history = histories[job["job_id"]]
        if (not history or history[0]["event_type"] != "queued"
                or history[0]["payload"].get("revision_id") != job["revision_id"]
                or history[0]["payload"].get("worktree_id") != job["worktree_id"]):
            raise ProtocolError("Execution job enqueue identity changed")
    return {"exists": True, "jobs": jobs, "integrity": integrity,
        "snapshot_sha256": digest({"jobs": jobs, "histories": histories, "integrity": integrity})}


def _model_snapshot(project, requests):
    if not (Path(project) / SESSIONS_REL).is_file():
        return {"exists": False, "sessions": [], "jobs": [], "turns": [], "tool_calls": [],
            "snapshot_sha256": digest([])}
    request_index = {item["request_id"]: item for item in requests}
    with closing(AgentSessionStore.readonly(project)) as store:
        turn_integrity, tool_integrity = store.verify_turn_events(), store.verify_tool_events()
        if not turn_integrity["ok"] or not tool_integrity["ok"]:
            raise ProtocolError("Agent conversation or tool event integrity failed")
        sessions, jobs, turns, calls = [], [], [], []
        for session in store.list_sessions():
            session_id = session["session_id"]
            sessions.append({key: session.get(key) for key in
                ("session_id", "title", "status", "model", "created", "updated")})
            for job in store.agent_jobs(session_id):
                jobs.append({key: job.get(key) for key in ("job_id", "session_id", "turn_id", "status",
                    "model_name", "prompt_tokens", "completion_tokens", "total_tokens", "reserved_tokens",
                    "cost_micros", "price_catalog_id", "error", "created", "updated")})
            for turn in store.turns(session_id):
                value = load_input(store, turn["turn_id"])
                snapshot_sha = digest(value) if value is not None else None
                request = request_index.get(turn["turn_id"])
                if request is not None and value is not None and request["snapshot_sha256"] != snapshot_sha:
                    raise ProtocolError("Reservation and persisted model input snapshots differ")
                turns.append({key: turn.get(key) for key in ("turn_id", "session_id", "parent_turn_id",
                    "status", "error", "created", "updated")} | {
                    "user_message_sha256": digest(turn.get("user_message")),
                    "assistant_message_sha256": digest(turn.get("assistant_message")),
                    "input_snapshot_sha256": snapshot_sha,
                    "request_limits": redact(value.get("request_limits", {})) if value else None,
                    "tool_definitions_sha256": digest(value.get("tools")) if value else None,
                    "ledger_request_present": request is not None})
            for call in store.tool_calls(session_id):
                calls.append({key: call.get(key) for key in ("call_id", "session_id", "turn_id", "tool_name",
                    "risk", "status", "proposed_by", "decided_by", "decision_reason", "error", "created", "updated")} | {
                    "arguments_sha256": digest(call.get("arguments")),
                    "result_sha256": digest(call.get("result"))})
    result = redact({"exists": True, "sessions": sessions, "jobs": jobs, "turns": turns,
        "tool_calls": calls, "turn_integrity": turn_integrity, "tool_integrity": tool_integrity})
    result["snapshot_sha256"] = digest(result)
    return result


def _execution_revisions(root, campaign_sha256):
    revisions, previous = [], campaign_sha256
    for path in sorted((root / "execution-revisions").glob("*.json")):
        value = read_json(path)
        if value.get("campaign_sha256") != campaign_sha256 or value.get("parent_sha256") != previous:
            raise ProtocolError("Execution revision provenance chain changed")
        previous = file_hash(path)
        arm_path = root / "execution-arms" / path.name
        arm = None
        if arm_path.is_file():
            body = read_json(arm_path)
            if body.get("campaign_sha256") != campaign_sha256 or body.get("execution_snapshot_sha256") != previous:
                raise ProtocolError("Execution arm provenance mismatch")
            arm = {"path": str(arm_path), "sha256": file_hash(arm_path),
                "formal_requests_before_arm": body.get("formal_requests_before_arm"),
                "regression_log": body.get("regression_log"), "regression_sha256": body.get("regression_sha256"),
                "read_probe": {key: body.get("read_probe", {}).get(key) for key in
                    ("passed", "protected_count", "host_read_verified", "network_block")}}
        revisions.append({"version": value["version"], "path": str(path), "sha256": previous,
            "parent_sha256": value["parent_sha256"], "implementation_sha256": value["implementation_sha256"],
            "reason": value.get("reason"), "arm": arm})
    return revisions


def _approval_provenance(folder, execution_revisions):
    known = {item["version"]: item["sha256"] for item in execution_revisions}
    receipts = []
    for path in sorted((folder / "native-development-approvals").glob("*.json")):
        value = read_json(path)
        version, expected = value.get("execution_revision"), value.get("execution_snapshot_sha256")
        if version is not None and known.get(version) != expected:
            raise ProtocolError("Native approval execution revision mismatch")
        call = value.get("tool_call") or {}
        receipts.append({"path": str(path), "sha256": file_hash(path), "call_id": call.get("call_id"),
            "turn_id": call.get("turn_id"), "phase": value.get("phase"),
            "execution_revision": version, "execution_snapshot_sha256": expected,
            "approval_source": redact(value.get("approval_source")),
            "numerical_source_unchanged": value.get("numerical_source_unchanged")})
    return receipts


def _study_snapshot(project, research):
    with closing(Experiment(project, readonly=True)) as experiment:
        core = experiment.verify_inputs()
    if not (research / "research.json").is_file():
        return {"exists": False, "study_id": None, "state_version": None, "claims_count": 0,
            "confirmation_consumed": bool(core.get("test_consumed")), "confirmation_eligible": False,
            "confirmation_refusal": "No Study: no operator-filled hypotheses or retrospective predictions", "core_phase": core["phase"]}
    manifest = read_json(research / "research.json")
    if Path(manifest["project"]).resolve() != project:
        raise ProtocolError("Research manifest belongs to a different project")
    state = read_research_state(research)
    if not state.get("ok") or not state["experiment_graph"]["integrity"]["ok"]:
        raise ProtocolError("Study or experiment graph integrity failed")
    result = {"exists": True, "study_id": state["study"]["entity_id"], "state_version": state["state_version"],
        "manifest_sha256": file_hash(research / "research.json"),
        "graph_sha256": digest(state["experiment_graph"]), "claims_count": len(state["claims"]),
        "budget": state["budget"], "confirmation_consumed": bool(core.get("test_consumed")),
        "core_phase": core["phase"], "control_hypothesis_id": manifest["control_hypothesis_id"],
        "candidate_hypothesis_ids": manifest["candidate_hypothesis_ids"]}
    try:
        preview = preview_study_action(research, "confirm")
        result.update(confirmation_eligible=True, confirmation_preview=preview)
    except ProtocolError as error:
        result.update(confirmation_eligible=False, confirmation_refusal=str(error))
    return result


def _execution_presence(research, job):
    """Distinguish a job never executed from corrupt artifacts of an execution."""
    if job["status"] not in _TERMINAL:
        return "not_terminal"
    run_id = job.get("run_id") or "RUN-" + digest({"revision_id": job["revision_id"], "split": "dev"})[:24]
    state = read_research_state(research)
    run = next((item for item in state["runs"] if item["run_id"] == run_id), None)
    worker_present = any(read_json(path).get("spec", {}).get("idempotency_key", "").startswith(run_id + ":seed:")
        for path in sorted((research / "jobs").glob("JOB-*/job.json")))
    if run is None or not worker_present:
        if job["status"] == "succeeded":
            raise ProtocolError("Succeeded execution lacks its real Run or Worker")
        return "no_execution"
    return "executed"


def _unscored_evidence(study, job, revision, reason):
    value = {"schema_version": "1.0", "scope": "dev", "kind": "development_evidence_unscored",
        "study_id": study["study_id"], "state_version": study["state_version"],
        "job_id": job["job_id"], "revision_id": job["revision_id"], "run_id": job.get("run_id"),
        "source_digests": {"candidate:revision": revision["manifest_sha256"]},
        "candidate": {"observation_id": None, "trajectory_error": None, "execution_status": job["status"],
            "execution_cases": None, "trajectory_valid_cases": None, "trusted_rhs_queries": None,
            "systems": {}, "cases": [], "workers": []},
        "control": None, "comparison": {"absolute_improvement": None, "relative_improvement": None,
            "threshold": None, "development_threshold_met": None},
        "reason": reason, "error": redact(job.get("error")), "authority": "No numerical grade inferred"}
    return value | {"evidence_sha256": digest(value)}


def _review_files(project):
    """Inventory only existing immutable application receipts, never initialize."""
    directory = project / ".popper" / "drev"
    if any(path.is_symlink() for path in (project / ".popper", directory)):
        raise ProtocolError("Development review storage must remain inside its project")
    if not directory.exists():
        return [], digest({"exists": False, "files": []})
    if not directory.is_dir():
        raise ProtocolError("Development review storage is not a directory")
    files = sorted(directory.glob("*.json"))
    if any(path.is_symlink() or path.resolve().parent != directory.resolve() for path in files):
        raise ProtocolError("Development review receipt escapes its project")
    return files, digest({"exists": True, "files": [(path.name, file_hash(path)) for path in files]})


def _development_reviews(project, research, study, trials):
    """Audit authored reviews without promoting STOP or an untested proposal to a final candidate."""
    files, files_sha = _review_files(project)
    if not files:
        return {"records": [], "snapshot_sha256": files_sha,
            "final_candidate": None, "final_candidate_authority": "No explicit final-candidate selection record"}
    from popper.research.development_review import (
        _candidate_scope, _parse, _read_saved, _source_turn, verify_development_review)
    by_job = {trial["job"]["job_id"]: trial["evidence"] for trial in trials}
    records = []
    for path in files:
        record = _read_saved(path)
        preview = record["review"]
        if (path.stem != str(record.get("idempotency_sha256", ""))[:24]
                or preview.get("kind") != "DevelopmentReview" or preview.get("scope") != "dev"
                or Path(preview.get("project", "")).resolve() != project
                or Path(preview.get("research", "")).resolve() != research
                or preview.get("study_id") != study["study_id"]):
            raise ProtocolError("Development review project, Study or receipt ownership changed")
        evidence = by_job.get(preview.get("job_id"))
        if (evidence is None or evidence.get("revision_id") != preview.get("revision_id")
                or evidence.get("run_id") != preview.get("run_id")):
            raise ProtocolError("Development review does not belong to a retained candidate execution")
        saved_source = preview.get("source") or {}
        source, raw = _source_turn(project, saved_source.get("session_id"), saved_source.get("turn_id"))
        if source != saved_source:
            raise ProtocolError("Development review source Turn or persisted input changed")
        assessment = preview.get("assessment") or {}
        structured = preview.get("status") == "structured_source_review"
        authored = _parse(raw) if structured else None
        if (structured and redact(authored) != preview.get("authored_review")) or (
                not structured and (preview.get("status") != "unverified_source"
                    or preview.get("authored_review") is not None or assessment.get("decision") is not None)):
            raise ProtocolError("Development review differs from its actual authored source")
        if structured:
            decision = assessment.get("decision") or {}
            historical_checks = assessment.get("checks") or []
            observed = authored.get("observed") if isinstance(authored, dict) else None
            if (assessment.get("schema_valid") is not True or not isinstance(observed, list)
                    or decision.get("authored") != redact(authored.get("decision"))
                    or len(historical_checks) != len(observed)
                    or any(check.get("pointer") != item.get("pointer")
                        or check.get("authored_value") != redact(item.get("value"))
                        or check.get("verified") != (canonical(check.get("authored_value")) == canonical(check.get("actual_value")))
                        for check, item in zip(historical_checks, observed))
                    or assessment.get("facts_verified") != all(check.get("verified") for check in historical_checks)):
                raise ProtocolError("Development review assessment does not preserve its authored facts or decision")
        changed = [key for key in ("state_version", "evidence_sha256", "source_digests")
                   if preview.get(key) != evidence.get(key)]
        current_assessment = None
        if structured and not changed:
            if (authored.get("decision") or {}).get("action") == "propose_candidate":
                candidates, attempted, untested = _candidate_scope(project, research, evidence)
            else:
                candidates, attempted, untested = (), (), None
            current_assessment = verify_development_review(evidence, authored,
                registered_candidates=candidates, tested_candidate_indices=attempted,
                untested_indices=untested)
        decision = assessment.get("decision") or {}
        action = (decision.get("authored") or {}).get("action")
        records.append({"path": str(path), "file_sha256": file_hash(path),
            "record_sha256": record["record_sha256"], "approval_sha256": preview["approval_sha256"],
            "source_turn": source, "status": preview.get("status"),
            "study_id": preview["study_id"], "state_version": preview["state_version"],
            "job_id": preview["job_id"], "revision_id": preview.get("revision_id"),
            "run_id": preview.get("run_id"), "evidence_sha256": preview["evidence_sha256"],
            "source_digests": preview["source_digests"], "authored_review": preview.get("authored_review"),
            "historical_assessment": assessment, "current_assessment": current_assessment,
            "decision_action": action, "explicit_stop": action == "stop",
            "decision_eligible_at_record": decision.get("eligible"),
            "stale": bool(changed), "stale_fields": changed,
            "freshness": "needs_new_review" if changed else "current_evidence",
            "execution_approved": False,
            "authority": "Authored source data; mechanisms remain unverified. STOP is not final-candidate selection or execution approval."})
    return {"records": records, "snapshot_sha256": files_sha, "final_candidate": None,
        "final_candidate_authority": "DevelopmentReview stop/propose_candidate does not select a final Revision; no score-based inference"}


def build_closeout(campaign_root, *, evidence_builder=None):
    """Return a derived snapshot. No reporting files or scientific state are written."""
    if evidence_builder is None:
        from popper.research.development_evidence import build_development_evidence
        evidence_builder = build_development_evidence
    root = Path(campaign_root).resolve()
    campaign_path = root / "campaign.json"
    campaign = read_json(campaign_path)
    campaign_sha = file_hash(campaign_path)
    protocol = Path(campaign["study_root"]) / "protocol.json"
    if not protocol.is_file() or file_hash(protocol) != campaign["protocol_sha256"]:
        raise ProtocolError("Original scientific protocol changed")
    entries = campaign["runs"]
    if (len({row["run_id"] for row in entries}) != len(entries)
            or any(row["arm"] not in {"single_shot", "popper_loop"} for row in entries)):
        raise ProtocolError("Invalid campaign run identities")
    budget = _budget_snapshot(campaign["budget_ledger"])
    versions = _execution_revisions(root, campaign_sha)
    runs, replay_entries, checks = [], [], []
    for entry in entries:
        folder = (root / entry["run_id"]).resolve()
        if folder.parent != root:
            raise ProtocolError("Campaign run directory escapes campaign")
        project, research = Path(entry["project"]).resolve(), Path(entry["research"]).resolve()
        if project != folder / "project" or research != folder / "research":
            raise ProtocolError("Campaign run project/research ownership mismatch")
        study = _study_snapshot(project, research)
        queue = _queue_snapshot(research)
        model = _model_snapshot(project, budget["requests"])
        revisions = {}
        trials, controls = [], []
        for job in queue["jobs"]:
            revision = _immutable_revision(research / "revisions", job["revision_id"])
            revisions[revision["revision_id"]] = revision
            if not study["exists"]:
                raise ProtocolError("Revision execution exists without an authoritative Study")
            hypothesis = revision["identity"]["hypothesis_id"]
            is_control = hypothesis == study["control_hypothesis_id"]
            if not is_control and hypothesis not in study["candidate_hypothesis_ids"]:
                raise ProtocolError("Executed revision has an unregistered candidate hypothesis")
            if is_control:
                # A candidate projection verifies the associated frozen control.
                # The strict development builder intentionally rejects a control
                # submitted as a candidate; retain this queue identity separately.
                controls.append({"job": job, "role": "fixed_control",
                    "revision_manifest_sha256": revision["manifest_sha256"], "evidence": None,
                    "execution_complete": job["status"] in _TERMINAL})
                continue
            presence = _execution_presence(research, job)
            evidence = (evidence_builder(project, research, job["job_id"]) if presence == "executed"
                else _unscored_evidence(study, job, revision, presence))
            if evidence["study_id"] != study["study_id"] or evidence["revision_id"] != job["revision_id"]:
                raise ProtocolError("Development evidence belongs to a different object")
            if evidence["state_version"] != study["state_version"]:
                raise ProtocolError("Study changed during development evidence audit")
            trial = {"job": job, "role": "fixed_control" if is_control else "candidate",
                "revision_manifest_sha256": revision["manifest_sha256"], "evidence": evidence,
                "has_observation": bool((evidence.get("candidate") or {}).get("observation_id")),
                "execution_complete": job["status"] in _TERMINAL}
            trials.append(trial)
            replay_entries.append({"campaign_run_id": entry["run_id"], "arm": entry["arm"],
                "study_id": study["study_id"], "state_version": study["state_version"],
                "project": str(project), "research": str(research), "job_id": job["job_id"],
                "run_id": evidence.get("run_id"), "role": trial["role"], "status": job["status"],
                "revision": revision, "evidence_sha256": evidence["evidence_sha256"],
                "source_digests": evidence["source_digests"],
                "workers": (evidence.get("candidate") or {}).get("workers", []),
                "observation_id": (evidence.get("candidate") or {}).get("observation_id"),
                "trajectory_error": (evidence.get("candidate") or {}).get("trajectory_error"),
                "no_observation_reason": job.get("error") if not trial["has_observation"] else None})
        control_sides = {trial["evidence"]["control"]["observation_id"]: trial["evidence"]["control"]
            for trial in trials if trial["evidence"].get("control")}
        for observation_id, side in control_sides.items():
            control_evidence = {"candidate": side, "control": None}
            linked = next((trial for trial in controls if trial["job"]["revision_id"] == side["revision_id"]), None)
            if linked:
                linked.update(evidence=control_evidence, has_observation=True)
            else:
                controls.append({"role": "fixed_control", "has_observation": True, "execution_complete": True,
                    "job": {"job_id": None, "revision_id": side["revision_id"], "run_id": side["run_id"], "status": "succeeded"},
                    "evidence": control_evidence, "source": "authoritative control Observation verified by candidate diagnostics"})
            control_revision = _immutable_revision(research / "revisions", side["revision_id"])
            control_source = next(trial["evidence"]["source_digests"] for trial in trials
                if (trial["evidence"].get("control") or {}).get("observation_id") == observation_id)
            replay_entries.append({"campaign_run_id": entry["run_id"], "arm": entry["arm"],
                "study_id": study["study_id"], "state_version": study["state_version"],
                "project": str(project), "research": str(research), "job_id": linked["job"]["job_id"] if linked else None,
                "run_id": side["run_id"], "role": "fixed_control", "status": side["execution_status"],
                "revision": control_revision, "observation_id": observation_id,
                "trajectory_error": side["trajectory_error"], "workers": side.get("workers", []),
                "source_digests": {key: value for key, value in control_source.items()
                    if not key.startswith("candidate:")},
                "no_observation_reason": None})
        # Created but unexecuted immutable revisions remain visible as well.
        for path in sorted((research / "revisions").glob("REV-*/revision.json")):
            revision = _immutable_revision(research / "revisions", path.parent.name)
            revisions[revision["revision_id"]] = revision
        reviews = _development_reviews(project, research, study, trials)
        run = {"run_id": entry["run_id"], "arm": entry["arm"], "project": str(project), "research": str(research),
            "study": study, "candidate_trials": trials, "control_trials": controls,
            "candidate_execution_count": len({trial["job"]["revision_id"] for trial in trials}),
            "all_jobs_terminal": all(job["status"] in _TERMINAL for job in queue["jobs"]),
            "execution_queue": {key: value for key, value in queue.items() if key != "jobs"},
            "immutable_revisions": list(revisions.values()), "conversation": model,
            "native_approval_receipts": _approval_provenance(folder, versions),
            "development_reviews": reviews,
            "final_candidate": None, "final_candidate_note": "Not inferred from the lowest development score; requires an explicit final model decision."}
        runs.append(run)
        checks.append((project, research, study, queue["snapshot_sha256"], model["snapshot_sha256"], reviews["snapshot_sha256"]))
    if _budget_snapshot(campaign["budget_ledger"])["snapshot_sha256"] != budget["snapshot_sha256"]:
        raise ProtocolError("Campaign budget changed during closeout")
    for project, research, study, queue_sha, model_sha, reviews_sha in checks:
        if (_study_snapshot(project, research) != study or _queue_snapshot(research)["snapshot_sha256"] != queue_sha
                or _model_snapshot(project, budget["requests"])["snapshot_sha256"] != model_sha
                or _review_files(project)[1] != reviews_sha):
            raise ProtocolError("Research or conversation changed during closeout; read a fresh snapshot")
    if file_hash(campaign_path) != campaign_sha:
        raise ProtocolError("Campaign changed during closeout")
    report = {"schema_version": SCHEMA_VERSION, "report_kind": "development_evidence_report",
        "scope": "dev", "derived_not_kernel_state": True,
        "created_at": datetime.now(timezone.utc).isoformat(), "no_model_calls": True,
        "no_confirmation_consumption": True, "no_solver_reexecution": True,
        "reference_arrays_disclosed": False, "campaign": {"path": str(campaign_path), "sha256": campaign_sha,
            "protocol_path": str(protocol.resolve()), "protocol_sha256": campaign["protocol_sha256"],
            "model_configuration": {key: campaign.get(key) for key in ("model", "base_url", "temperature", "max_output_tokens")},
            "original_manifest_token_limit": campaign.get("token_limit"),
            "primary_criterion": campaign["primary_criterion"]},
        "budget": budget, "execution_revisions": versions, "runs": runs,
        "limitations": list(_LIMITATIONS), "scientific_gain_verdict": "not_established",
        "fair_complete_numeric_comparison": False}
    report = redact(report)
    report["report_sha256"] = digest(report)
    reproduction = {"schema_version": SCHEMA_VERSION, "report_sha256": report["report_sha256"],
        "campaign_sha256": campaign_sha, "scope": "dev", "entries": replay_entries,
        "budget_snapshot_sha256": budget["snapshot_sha256"],
        "replay_requires_explicit_isolated_command": True, "no_automatic_model_or_confirmation": True,
        "not_third_party_validation": True}
    reproduction = redact(reproduction)
    reproduction["manifest_sha256"] = digest(reproduction)
    return report, reproduction


def _markdown(report):
    budget = report["budget"]
    lines = ["# Popper 开发证据报告", "", "这是开发集证据整理，不是独立确认、原创发现或通用科研能力通过证明。", "",
        f"- 报告摘要：`{report['report_sha256']}`", f"- 真实总账本：{budget['request_count']} 次请求，{budget['used_tokens']} tokens。",
        f"- 当前累计上限：{budget['policy']['max_requests']} 次请求，{budget['policy']['max_tokens']} tokens。",
        f"- 剩余：{budget['remaining_calls']} 次请求，{budget['remaining_tokens']} tokens；未核实请求 {budget['unresolved_requests']} 个。", ""]
    for run in report["runs"]:
        study = run["study"]
        lines += [f"## {run['run_id']}", "", f"- 组别：{run['arm']}；Study：{study['study_id'] or '未建立'}。",
            f"- 实际候选执行：{run['candidate_execution_count']} 个独立 Revision；Claim：{study['claims_count']} 个。",
            f"- 确认资源已消费：{study['confirmation_consumed']}；当前确认资格：{study['confirmation_eligible']}。"]
        if study.get("confirmation_refusal"):
            lines.append(f"- 确认拒绝原因：{study['confirmation_refusal']}")
        for review in run.get("development_reviews", {}).get("records", []):
            check = review["historical_assessment"]
            lines += [f"- DevelopmentReview `{review['record_sha256']}`：来源 Turn `{review['source_turn']['turn_id']}`，",
                f"  决定 {review['decision_action'] or '无可核验决定'}；记录时资格 {review['decision_eligible_at_record']}；",
                f"  事实核对 {check.get('facts_verified')}；当前状态 {review['freshness']}。"]
            if review["stale"]:
                lines.append(f"  需重新核对的版本/摘要字段：{', '.join(review['stale_fields'])}；历史解释不冒充当前事实。")
            if review["explicit_stop"]:
                lines.append("  模型明确停止；不自动重启，不把停止反推成最终候选选择。")
        for trial in run["candidate_trials"]:
            evidence, job = trial["evidence"], trial["job"]
            candidate, comparison = evidence.get("candidate") or {}, evidence.get("comparison") or {}
            lines += [f"- `{job['job_id']}` / `{job['revision_id']}`：执行状态 {job['status']}，",
                f"  Observation `{candidate.get('observation_id') or '无'}`，轨迹误差 {candidate.get('trajectory_error')}，",
                f"  可信 RHS 查询 {candidate.get('trusted_rhs_queries') if candidate.get('trusted_rhs_queries') is not None else '未知（计量缺失，不计为0）'}，开发改善阈值达到：{comparison.get('development_threshold_met')}。"]
            if not trial["has_observation"]:
                lines.append(f"  未补造数值成绩；执行错误：{job.get('error') or '没有真实 Observation'}。")
        lines += ["", "模型原答、预测及审批仍以原会话和不可变来源为准；本报告不推定最终候选。", ""]
    lines += ["## 边界与未验证事项", ""] + [f"- {item}" for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def _system_plot(report, field, title, axis_label):
    """Standard-library SVG of existing measurements; no invented epsilon."""
    points, missing_candidates, omitted_rows, candidates_without_systems = [], 0, 0, 0
    for run in report["runs"]:
        for index, trial in enumerate(run["candidate_trials"], start=1):
            evidence = trial.get("evidence") or {}
            candidate, control = evidence.get("candidate") or {}, evidence.get("control") or {}
            candidate_systems, control_systems = candidate.get("systems", {}), control.get("systems", {})
            systems = sorted(set(candidate_systems) | set(control_systems))
            if not systems:
                candidates_without_systems += 1
            for system in systems:
                values = [candidate_systems.get(system, {}).get(field), control_systems.get(system, {}).get(field)]
                if values[0] is None:
                    missing_candidates += 1
                if all(value is None for value in values):
                    omitted_rows += 1
                    continue
                if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0) for value in values):
                    raise ProtocolError("Plot measurements must be finite nonnegative values or absent")
                label = f"{run['run_id']} · C{index} · {system}"
                detail = f"{label}; Revision={trial['job']['revision_id']}; Observation={candidate.get('observation_id')}; execution={trial['job']['status']}"
                missing_note = "无 Observation/无成绩" if not candidate.get("observation_id") else "无成绩"
                points.append((label, detail, values, missing_note))
    positives = [value for _, _, values, _ in points for value in values if value is not None and value > 0]
    low = math.floor(min(math.log10(value) for value in positives)) if positives else -1
    high = math.ceil(max(math.log10(value) for value in positives)) if positives else 1
    if low == high:
        low, high = low - 1, high + 1
    width, row_height = 1440, 46
    plot_left, plot_right, zero_x = 390, 1310, 320
    first_y, axis_y = 210, 210 + max(1, len(points)) * row_height
    height = axis_y + 172
    esc = lambda value: escape(str(value), {'"': "&quot;"})
    fragments = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{esc(title)}</title>',
        '<desc id="desc">Development measurements by candidate and system compared with the frozen RK4 control. Positive values use log10 coordinates; exact zero has a separate lane. Missing measurements are not replaced with a score.</desc>',
        '<style>text{font-family:Segoe UI,Arial,sans-serif;fill:#1f2937;font-size:14px}.note{fill:#4b5563;font-size:13px}.grid{stroke:#e5e7eb}.candidate{fill:#b45309}.control{fill:#2563eb}</style>',
        f'<rect width="{width}" height="{height}" fill="#fff"/>',
        f'<text x="24" y="38" font-size="24" font-weight="600">{esc(title)}</text>',
        f'<text class="note" x="24" y="67">范围：dev；来源：{esc(report["report_sha256"])}；生成：{esc(report["created_at"])}</text>',
        '<circle class="candidate" cx="34" cy="98" r="6"/><text x="48" y="103">候选方法（Agent 原始不可变 Revision）</text>',
        '<rect class="control" x="386" y="92" width="12" height="12"/><text x="410" y="103">固定 RK4 对照</text>',
        f'<text class="note" x="24" y="134">{esc(axis_label)}；正值横坐标 = log10(原始值)，标签保留原始值；0 单独显示，不替换为 epsilon。</text>',
        '<text class="note" x="24" y="157">执行完成不等于数值改善；能量漂移只覆盖冻结输出采样点，不能替代轨迹误差或证明相位正确。</text>',
        f'<text class="note" x="{zero_x}" y="184" text-anchor="middle">精确 0</text>',
        f'<text class="note" x="{plot_left}" y="184">正值 log10 坐标</text>']
    step = max(1, math.ceil((high - low) / 8))
    ticks = sorted(set([low, high, *range(low, high + 1, step)]))
    for exponent in ticks:
        x = plot_left + (exponent - low) / (high - low) * (plot_right - plot_left)
        fragments += [f'<line class="grid" x1="{x:.2f}" y1="194" x2="{x:.2f}" y2="{axis_y}"/>',
            f'<text class="note" x="{x:.2f}" y="{axis_y + 24}" text-anchor="middle">10^{exponent}</text>']
    fragments.append(f'<line x1="{zero_x + 32}" y1="194" x2="{zero_x + 32}" y2="{axis_y}" stroke="#9ca3af" stroke-dasharray="4 4"/>')
    for index, (label, detail, values, missing_note) in enumerate(points):
        y = first_y + index * row_height
        fragments += [f'<text x="24" y="{y + 4}" font-size="13">{esc(label)}<title>{esc(detail)}</title></text>',
            f'<line class="grid" x1="294" y1="{y + 23}" x2="{plot_right}" y2="{y + 23}"/>']
        for position, value in enumerate(values):
            row_y = y - 7 if position == 0 else y + 9
            css = "candidate" if position == 0 else "control"
            role = "candidate" if position == 0 else "fixed RK4 control"
            if value is None:
                note = missing_note if position == 0 else "无对照成绩"
                fragments.append(f'<text class="note" x="{plot_left}" y="{row_y + 4}">{esc(note)}<title>{esc(detail + "; " + role + ": missing measurement")}</title></text>')
                continue
            x = zero_x if value == 0 else plot_left + (math.log10(value) - low) / (high - low) * (plot_right - plot_left)
            tooltip = f"{detail}; {role}: {value:.17g} (raw value)"
            if position == 0:
                fragments.append(f'<circle class="{css}" cx="{x:.2f}" cy="{row_y}" r="5"><title>{esc(tooltip)}</title></circle>')
            else:
                fragments.append(f'<rect class="{css}" x="{x - 5:.2f}" y="{row_y - 5}" width="10" height="10"><title>{esc(tooltip)}</title></rect>')
            anchor = "end" if x > plot_right - 80 else "start"
            text_x = x - 10 if anchor == "end" else x + 10
            fragments.append(f'<text class="note" x="{text_x:.2f}" y="{row_y + 4}" text-anchor="{anchor}">{esc(format(value, ".5g"))}</text>')
    if not points:
        fragments.append('<text x="390" y="230">没有可绘制的真实分系统成绩；失败与无 Observation 记录仍保留在报告中。</text>')
    fragments += [f'<text class="note" x="{plot_left}" y="{axis_y + 49}">{esc(axis_label)}（刻度显示原始正值）</text>',
        f'<text class="note" x="24" y="{axis_y + 83}">候选无成绩（按候选/系统）：{missing_candidates}；候选与对照均无成绩而整行省略：{omitted_rows}；无系统记录候选：{candidates_without_systems}。</text>',
        f'<text class="note" x="24" y="{axis_y + 110}">能量图仅取有轨迹成绩的案例均值；未完成/失败案例的漂移不可解释为零。</text>',
        f'<text class="note" x="24" y="{axis_y + 137}">不涉及确认集，不是第三方独立验证；不同执行修订与额外诊断反馈的限制见 report.json。</text>', '</svg>']
    return "\n".join(fragments) + "\n"


def _csv_system_counts(side, system, values):
    """Project only complete retained per-case fields, never absent data as zero."""
    counts = {"scored_count": None, "host_execution_completed_count": None,
        "host_execution_failed_count": None, "host_execution_not_completed_count": None}
    cases = side.get("cases")
    if not isinstance(cases, list):
        return counts
    subset = [case for case in cases if case.get("system") == system]
    if not subset or len(subset) != values.get("case_count"):
        return counts
    if all("trajectory_error" in case for case in subset):
        counts["scored_count"] = sum(case["trajectory_error"] is not None for case in subset)
    statuses = [case.get("execution_status") for case in subset]
    if all(status in {"succeeded", "failed", "not_completed"} for status in statuses):
        counts.update(host_execution_completed_count=statuses.count("succeeded"),
            host_execution_failed_count=statuses.count("failed"),
            host_execution_not_completed_count=statuses.count("not_completed"))
    return counts


def write_closeout(report, reproduction, output):
    """Exclusive new directory; never overwrite a previous report or evidence."""
    output = Path(output).resolve()
    if output.exists():
        raise ProtocolError("Refusing to overwrite an existing closeout directory")
    report_body = {key: value for key, value in report.items() if key != "report_sha256"}
    if report.get("report_sha256") != digest(report_body) or reproduction.get("report_sha256") != report["report_sha256"]:
        raise ProtocolError("Report or reproduction manifest identity changed")
    if reproduction.get("manifest_sha256") != digest({key: value for key, value in reproduction.items() if key != "manifest_sha256"}):
        raise ProtocolError("Reproduction manifest digest changed")
    protected = [Path(report["campaign"]["path"]).parent]
    protected += [Path(run[key]) for run in report["runs"] for key in ("project", "research")]
    if any(path == output or path.is_relative_to(output) for path in protected):
        raise ProtocolError("Report directory cannot replace an evidence ancestor")
    if any(output.is_relative_to(Path(run[key])) for run in report["runs"] for key in ("project", "research")):
        raise ProtocolError("Report output must stay outside scientific project/research roots")
    # Validate/render before creating the new directory, so bad measurements do
    # not leave a partly authoritative-looking report behind.
    figures = {"trajectory-error.svg": _system_plot(report, "mean_trajectory_error", "逐候选、逐系统轨迹误差（开发集）", "归一化轨迹误差（无量纲）"),
        "sampled-energy-drift.svg": _system_plot(report, "mean_sampled_energy_drift_successful", "逐候选、逐系统采样点能量漂移（开发集）", "最大相对能量漂移的案例均值（无量纲）")}
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("report.json", report), ("reproduction-manifest.json", reproduction)):
        with (output / name).open("x", encoding="utf-8") as stream:
            stream.write(canonical(value))
    with (output / "report.md").open("x", encoding="utf-8") as stream:
        stream.write(_markdown(report))
    for name, svg in figures.items():
        with (output / name).open("x", encoding="utf-8") as stream:
            stream.write(svg)
    with (output / "cases.csv").open("x", encoding="utf-8-sig", newline="") as case_file, \
            (output / "systems.csv").open("x", encoding="utf-8-sig", newline="") as system_file:
        case_writer = csv.DictWriter(case_file, fieldnames=["campaign_run_id", "job_id", "role", "observation_id",
            "case_id", "system", "trajectory_error", "sampled_energy_drift", "failed", "trusted_rhs_queries",
            "job_execution_status", "host_case_execution_status", "failed_basis"])
        system_writer = csv.DictWriter(system_file, fieldnames=["campaign_run_id", "job_id", "role", "observation_id",
            "system", "case_count", "failed_cases", "mean_trajectory_error", "mean_sampled_energy_drift_successful",
            "job_execution_status", "scored_count", "failed_cases_basis", "host_execution_completed_count",
            "host_execution_failed_count", "host_execution_not_completed_count"])
        case_writer.writeheader()
        system_writer.writeheader()
        for run in report["runs"]:
            for trial in run["control_trials"] + run["candidate_trials"]:
                # The explicitly executed trial is the candidate side, even for
                # the fixed control; avoid repeating its control-side summary.
                side = (trial.get("evidence") or {}).get("candidate") or {}
                common = {"campaign_run_id": run["run_id"], "job_id": trial["job"]["job_id"],
                    "role": trial["role"], "observation_id": side.get("observation_id"),
                    "job_execution_status": trial["job"].get("status")}
                for case in side.get("cases", []):
                    case_writer.writerow(common | {"case_id": case.get("case_id", case.get("id")),
                        "system": case.get("system"), "trajectory_error": case.get("trajectory_error"),
                        "sampled_energy_drift": case.get("energy_drift", case.get("sampled_energy_drift")),
                        "failed": case.get("failed"), "trusted_rhs_queries": case.get("trusted_rhs_queries", case.get("query_count")),
                        "host_case_execution_status": case.get("execution_status"),
                        "failed_basis": "scored_trajectory_validity_only"})
                for system, values in side.get("systems", {}).items():
                    system_writer.writerow(common | {"system": system} | {key: values.get(key) for key in
                        ("case_count", "failed_cases", "mean_trajectory_error", "mean_sampled_energy_drift_successful")}
                        | {"failed_cases_basis": "scored_cases_only"} | _csv_system_counts(side, system, values))
    files = {path.name: file_hash(path) for path in sorted(output.iterdir()) if path.is_file()}
    with (output / "files.json").open("x", encoding="utf-8") as stream:
        stream.write(canonical({"schema_version": 1, "report_sha256": report["report_sha256"], "files_sha256": files}))
    return {"output": str(output), "report_sha256": report["report_sha256"], "files_sha256": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ProtocolError("Refusing to overwrite an existing closeout directory")
    report, reproduction = build_closeout(args.campaign)
    print(json.dumps(write_closeout(report, reproduction, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
