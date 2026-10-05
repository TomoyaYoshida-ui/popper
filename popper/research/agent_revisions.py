"""Approved conversation adapter for existing Revision application services."""
from pathlib import Path
from contextlib import nullcontext

from ..core import Experiment, ProtocolError, digest, file_hash, read_json, write_json
from .. import revision_tools
from ..sessions import AgentSessionStore
from .readout import read_research_state
from .revision_drafts import (preview_initial_revision, resolve_initial_revision,
                              submit_initial_revision, failed_repair_parent)
from .revisions import RevisionStore
from .execution_jobs import RevisionExecutionJobStore
from .workers.local import LocalWorker


def _run(workstation, args, root):
    if not workstation.trusted_local:
        raise ProtocolError("Revision tools require trusted-local workstation")
    if Path(root).resolve() != workstation.workspace.root.resolve():
        raise ProtocolError("project changed; old Revision tool cannot execute")
    run = workstation.research_run_for_study(args["study_id"])
    if run not in workstation._visible_research_runs(root):
        raise ProtocolError("Study does not belong to this project")
    return run


REFERENCE_TOOLS = {"research.revision_commit", "research.revision_run"}


def _reference_source(call, args, root):
    """Read complete native receipts in this project, never a model-supplied copy."""
    if not isinstance(call, dict) or not call.get("session_id") or not call.get("call_id"):
        raise ProtocolError("Revision references require native call/session context")
    field = "preview_call_id" if call["tool_name"] == "research.revision_commit" else "create_call_id"
    store = AgentSessionStore.readonly(root)
    try:
        current = store.get_tool_call(call["call_id"])
        source = store.get_tool_call(args[field])
    finally:
        store.close()
    if (current["session_id"] != call["session_id"] or current["tool_name"] != call["tool_name"]
            or current["arguments"] != args):
        raise ProtocolError("Revision native call context mismatch")
    expected_tools = ({"research.revision_preview"} if field == "preview_call_id" else
                      {"research.revision_create", "research.revision_commit"})
    if (source["session_id"] != current["session_id"] or source["status"] != "completed"
            or source["proposed_by"] != "agent" or source["tool_name"] not in expected_tools
            or source["arguments"].get("study_id") != args["study_id"]
            or not isinstance(source["result"], dict)):
        raise ProtocolError("Revision reference requires a completed Agent receipt in the same project/session/Study")
    revision_tools.validate(source["tool_name"], source["arguments"])
    return source


def _resolve_reference(workstation, call, args, root, run):
    source = _reference_source(call, args, root)
    if call["tool_name"] == "research.revision_commit":
        spec = revision_tools.specification(source["arguments"])
        resolved = {**spec, "approval_sha256": source["result"].get("proposal_sha256")}
        revision_tools.validate("research.revision_create", resolved)
        return "research.revision_create", resolved, source
    result = source["result"]
    version = result.get("state_version")
    if type(version) is not int or version < 1:
        raise ProtocolError("Revision create receipt has no reviewed Study version; cannot infer a new approval")
    resolved = {"study_id": args["study_id"], "revision_id": result.get("revision_id"),
                "manifest_sha256": result.get("manifest_sha256"), "expected_state_version": version}
    revision_tools.validate("research.revision_execute", resolved)
    revision = RevisionStore(run / "revisions").verify(resolved["revision_id"])
    if revision["identity"]["actor"] != "agent":
        raise ProtocolError("Revision create receipt is not Agent-authored")
    return "research.revision_execute", resolved, source


def _reference_receipt_path(run, call):
    # Keep the full identity digest, but avoid a redundant prefix: write_json's
    # atomic temporary suffix otherwise reaches Windows MAX_PATH in real runs.
    return run / "agent-revision-receipts" / (digest({
        "tool": call["tool_name"], "session_id": call["session_id"], "arguments": call["arguments"]}) + ".json")


def _verify_result(run, tool, result, args):
    if tool == "research.revision_preview":
        if not isinstance(result.get("proposal_sha256"), str):
            raise ProtocolError("Revision preview receipt missing proposal digest")
        return
    revision_id = result.get("revision_id") or result.get("job", {}).get("revision_id")
    revision = RevisionStore(run / "revisions").verify(revision_id)
    expected = result.get("manifest_sha256") or args.get("manifest_sha256")
    if file_hash(run / "revisions" / revision_id / "revision.json") != expected:
        raise ProtocolError("Revision receipt manifest changed")
    if tool in {"research.revision_create", "research.revision_commit"} and revision["identity"]["actor"] != "agent":
        raise ProtocolError("Revision author does not match Agent tool")


def _recover_prepared_reference(workstation, run, call, source, receipt, root):
    """Reconcile existing immutable writes only; never enqueue or create here."""
    tool, resolved, _ = _resolve_reference(workstation, call, call["arguments"], root, run)
    if receipt.get("resolved_arguments") != resolved:
        raise ProtocolError("Revision prepared reference arguments changed")
    if tool == "research.revision_create":
        projection = source["result"]
        replacement_sha = projection.get("replacement_sha256")
        if replacement_sha is None and "replacement" in resolved:
            replacement_sha = digest(resolved["replacement"])
        expected_edit = [{"path": resolved["path"], "original_sha256": resolved["expected_sha256"],
                          "replacement_sha256": replacement_sha}]
        matches = []
        for path in sorted((run / "revisions").glob("REV-*/revision.json")):
            revision = RevisionStore(run / "revisions").verify(path.parent.name)
            identity = revision["identity"]
            if (identity["hypothesis_id"] == resolved["hypothesis_id"]
                    and identity["design_id"] == resolved["design_id"]
                    and identity["parent_revision_id"] == resolved.get("parent_revision_id")
                    and identity["actor"] == "agent" and identity["edits"] == expected_edit
                    and identity["diff_sha256"] == digest(projection["diff"])
                    and identity["source_control"] == projection["source_control"]):
                matches.append(revision)
        if not matches:
            return None
        if len(matches) != 1:
            raise ProtocolError("Revision prepared reference has ambiguous immutable receipts")
        revision_id = matches[0]["revision_id"]
        return {"revision_id": revision_id, "manifest_sha256": file_hash(run / "revisions" / revision_id / "revision.json"),
                "study_id": resolved["study_id"], "design_id": resolved["design_id"],
                "status": "immutable_unexecuted", "proposal_sha256": resolved["approval_sha256"],
                "state_version": resolved["expected_state_version"],
                "create_call_id": receipt["call_id"], "preview_call_id": source["call_id"]}
    revision = RevisionStore(run / "revisions").verify(resolved["revision_id"])
    if file_hash(run / "revisions" / resolved["revision_id"] / "revision.json") != resolved["manifest_sha256"]:
        raise ProtocolError("Revision prepared execution manifest changed")
    path = run / "revision-execution-jobs.sqlite"
    if not path.is_file():
        return None
    jobs = RevisionExecutionJobStore.readonly(path)
    try:
        if not jobs.verify()["ok"]:
            raise ProtocolError("Revision execution queue integrity failed")
        key = "approved" if revision["identity"]["parent_revision_id"] is not None else "initial"
        worktree = f"{key}:{resolved['revision_id']}"
        job = jobs.for_worktree(worktree)
        if job is None:
            return None
        history = jobs.history(job["job_id"])
        if (job["revision_id"] != resolved["revision_id"] or not history
                or history[0]["event_type"] != "queued"
                or history[0]["payload"].get("revision_id") != resolved["revision_id"]
                or history[0]["payload"].get("worktree_id") != worktree):
            raise ProtocolError("Revision execution receipt ownership mismatch")
    finally:
        jobs.close()
    return {"study_id": resolved["study_id"], "job": job,
            "manifest_sha256": resolved["manifest_sha256"], "status": "enqueued_not_completed",
            "create_call_id": source["call_id"]}


def _read_reference_receipt(workstation, run, call, root, *, persist=False):
    source = _reference_source(call, call["arguments"], root)
    path = _reference_receipt_path(run, call)
    if not path.is_file():
        # Old receipts remain readable and are never removed. Reconciliation
        # writes the compact path, not another overlong temporary filename.
        legacy = path.with_name("reference-" + path.name)
        if not legacy.is_file():
            return None
        receipt = read_json(legacy)
    else:
        receipt = read_json(path)
    if (receipt.get("arguments_sha256") != digest(call["arguments"])
            or receipt.get("tool") != call["tool_name"]
            or receipt.get("session_id") != call["session_id"]
            or receipt.get("source_call_id") != source["call_id"]
            or receipt.get("source_sha256") != digest({"arguments": source["arguments"], "result": source["result"]})):
        raise ProtocolError("Revision reference receipt conflict")
    if receipt.get("status") == "prepared":
        result = _recover_prepared_reference(workstation, run, call, source, receipt, root)
        if result is None:
            return None
        if persist:
            write_json(path, {**receipt, "status": "completed", "result": result})
    else:
        result = receipt["result"]
    _verify_result(run, call["tool_name"], result, call["arguments"])
    return result


def preview(workstation, tool, args, root, *, call=None, _locked=False):
    revision_tools.validate(tool, args)
    with nullcontext() if _locked else workstation.ws_lock:
        run = _run(workstation, args, root)
        if tool in REFERENCE_TOOLS:
            if not isinstance(call, dict) or call.get("tool_name") != tool:
                raise ProtocolError("Revision references require native call/session context")
            prior = _read_reference_receipt(workstation, run, call, root)
            if prior is not None:
                return {"ok": True, "replayed": True, "revision_preview": {
                    "study_id": args["study_id"], "revision_id": prior.get("revision_id") or prior.get("job", {}).get("revision_id"),
                    "manifest_sha256": prior.get("manifest_sha256"),
                    "effect": "return existing immutable receipt; no new execution or resource consumption"}}
            resolved_tool, resolved, source = _resolve_reference(workstation, call, args, root, run)
            projection = preview(workstation, resolved_tool, resolved, root, _locked=True)
            projection["reference"] = {"source_call_id": source["call_id"], "session_id": call["session_id"],
                                       "study_id": args["study_id"], "source_tool": source["tool_name"]}
            return projection
        if tool in {"research.revision_preview", "research.revision_create"}:
            proposal = preview_initial_revision(run, root, revision_tools.specification(args))
            matches = tool.endswith("preview") or proposal["proposal_sha256"] == args["approval_sha256"]
            return {"ok": matches, "revision_preview": proposal,
                    "state_version": proposal["expected_state_version"], "version_matches": True,
                    "approval_matches": matches, "reason": None if matches else "Revision proposal changed"}
        if tool == "research.revision_job":
            result = workstation.revision_job_payload(args["job_id"])
            if Path(result["research_run"]).resolve() != run.resolve() or not result["integrity"]["ok"]:
                raise ProtocolError("Revision Job ownership/integrity mismatch")
            return {"ok": True, "job": result["job"]}
        detail = workstation.revision_payload(args["revision_id"])
        if len(detail["research_runs"]) != 1 or Path(detail["research_runs"][0]).resolve() != run.resolve():
            raise ProtocolError("Revision does not belong to this Study")
        state = read_research_state(run)
        if not state.get("ok") or not state.get("experiment_graph", {}).get("integrity", {}).get("ok"):
            raise ProtocolError("Study provenance invalid")
        revision = RevisionStore(run / "revisions").verify(args["revision_id"])
        if detail["parent_revision_id"] is not None:
            failed_repair_parent(run, state, detail["parent_revision_id"],
                                revision["identity"]["hypothesis_id"], revision["identity"]["design_id"])
        design = next((d for d in state["designs"] if d["design_id"] == revision["identity"]["design_id"]), None)
        # Do not create a Controller during a read-only approval preview.
        if design is None or design["status"] != "frozen" or state["study"]["status"] != "active":
            raise ProtocolError("Revision execution requires an active Study and frozen design")
        if state["budget"]["available"] < 1:
            raise ProtocolError("development budget insufficient")
        version_matches = state["state_version"] == args["expected_state_version"]
        matches = detail["manifest_sha256"] == args["manifest_sha256"]
        return {"ok": version_matches and matches, "state_version": state["state_version"],
                "version_matches": version_matches, "approval_matches": matches,
                "revision_preview": {"study_id": args["study_id"], "revision_id": args["revision_id"],
                                     "scope": "development", "budget": state["budget"],
                                     "effect": "enqueue development Job; failures consume budget",
                                     "manifest_sha256": detail["manifest_sha256"]},
                "reason": None if version_matches and matches else "Study version or Revision digest changed"}


def _receipt_path(run, call_id):
    return run / "agent-revision-receipts" / (digest(call_id) + ".json")


def _candidate_development_budget(workstation, run, root):
    """Read the same distinct-Revision ceiling enforced before queueing.

    This is separate from the Research Kernel's resource budget. A queued
    non-control Revision counts once regardless of its Job's current status.
    """
    policy_path = Path(root) / "evaluation-policy.json"
    if workstation.evaluation_budget_path is None or not policy_path.is_file():
        return None
    exp = Experiment(root, readonly=True)
    try:
        core = exp.verify_inputs()
    finally:
        exp.close()
    if "evaluation-policy.json" not in core["spec"].get("provenance_files", []):
        raise ProtocolError("评测开发限制必须是冻结的预注册输入")
    limit = read_json(policy_path).get("candidate_development_limit")
    if type(limit) is not int or not 1 <= limit <= 6:
        raise ProtocolError("评测开发执行限制不合法")
    candidate_revisions = set()
    queue_path = run / "revision-execution-jobs.sqlite"
    if queue_path.is_file():
        jobs = RevisionExecutionJobStore.readonly(queue_path)
        try:
            if not jobs.verify()["ok"]:
                raise ProtocolError("Revision execution queue integrity failed")
            queued = jobs.list()
        finally:
            jobs.close()
        if queued:
            revision_root = run / "revisions"
            if not revision_root.is_dir():
                raise ProtocolError("Revision execution queue has no immutable revisions")
            manifest = read_json(run / "research.json")
            revisions = RevisionStore(revision_root)
            for job in queued:
                revision = revisions.verify(job["revision_id"])
                if revision["identity"]["hypothesis_id"] != manifest["control_hypothesis_id"]:
                    candidate_revisions.add(job["revision_id"])
    used = len(candidate_revisions)
    return {"scope": "candidate_development",
            "unit": "distinct_non_control_revisions_with_execution_jobs",
            "limit": limit, "used": used, "remaining": max(0, limit - used)}


def reconcile(workstation, call, root, *, _locked=False):
    tool, args = call["tool_name"], call["arguments"]
    revision_tools.validate(tool, args)
    with nullcontext() if _locked else workstation.ws_lock:
        run = _run(workstation, args, root)
        if tool in REFERENCE_TOOLS:
            return _read_reference_receipt(workstation, run, call, root, persist=True)
        path = _receipt_path(run, call["call_id"])
        if path.is_file():
            receipt = read_json(path)
            if receipt["arguments_sha256"] != digest(args) or receipt["tool"] != tool:
                raise ProtocolError("Revision tool receipt conflict")
            result = receipt["result"]
            _verify_result(run, tool, result, args)
            return result
        return None


def execute(workstation, call, root):
    tool, args = call["tool_name"], call["arguments"]
    revision_tools.validate(tool, args)
    with workstation.ws_lock:
        run = _run(workstation, args, root)
        prior = reconcile(workstation, call, root, _locked=True) if tool in {
            "research.revision_create", "research.revision_execute", *REFERENCE_TOOLS} else None
        if prior is not None:
            return prior
        projection = preview(workstation, tool, args, root, call=call, _locked=True)
        if not projection["ok"]:
            raise ProtocolError(projection.get("reason") or "Revision preview invalid")
        if tool == "research.revision_preview":
            result = {**projection["revision_preview"], "preview_call_id": call["call_id"]}
            _, resolved = resolve_initial_revision(run, root, revision_tools.specification(args))
            path = _receipt_path(run, call["call_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, {"arguments_sha256": digest(args), "tool": tool, "result": result,
                              "specification": revision_tools.specification(args), "resolved_specification": resolved})
            return result
        if tool == "research.revision_job":
            result = workstation.revision_job_payload(args["job_id"])
            state = read_research_state(run)
            revision = RevisionStore(run / "revisions").verify(result["job"]["revision_id"])
            design_id = revision["identity"]["design_id"]
            run_id = result["job"].get("run_id")
            if run_id is None:
                # A failed execution has no Observation, but the Controller still
                # persisted its deterministic development Run and Worker receipts.
                expected = "RUN-" + digest({
                    "revision_id": result["job"]["revision_id"], "split": "dev"})[:24]
                registered = next((row for row in state["runs"] if row.get("run_id") == expected), None)
                if (registered is not None
                        and registered.get("revision_id") == result["job"]["revision_id"]
                        and registered.get("design_id") == design_id
                        and registered.get("revision_manifest_sha256") == file_hash(
                            run / "revisions" / result["job"]["revision_id"] / "revision.json")):
                    run_id = expected
            metric = state["manifest"]["metric"]
            metric_direction = {"direction": metric["direction"]} if "direction" in metric else {}
            observations = [{**{key: observation.get(key) for key in (
                "observation_id", "run_id", "scope", "value", "scorer_id", "unit", "design_id")},
                "metric": metric["name"], **metric_direction}
                for observation in state["observations"]
                if observation.get("design_id") == design_id and observation.get("scope") == "dev"
                and run_id is not None and observation.get("run_id") == run_id]
            worker = LocalWorker(run / "jobs", run / "revisions")
            executions = []
            if run_id:
                for worker_job in worker.list_jobs():
                    spec = worker.read_manifest(worker_job["job_id"])["spec"]
                    # Bind the query to this development Run, not all executions
                    # of a Revision (which may later include held-out runs).
                    if not spec["idempotency_key"].startswith(run_id + ":seed:"):
                        continue
                    if (spec["revision_id"] != result["job"]["revision_id"]
                            or spec["design_id"] != design_id
                            or spec["revision_manifest_sha256"] != file_hash(
                                run / "revisions" / result["job"]["revision_id"] / "revision.json")):
                        raise ProtocolError("Worker execution ownership/digest mismatch")
                    if worker_job["status"] not in {
                            "succeeded", "implementation_failed", "infrastructure_failed", "cancelled"}:
                        continue
                    receipt = worker.collect(worker_job["job_id"])
                    item = {"job_id": worker_job["job_id"], "status": receipt["status"],
                            "execution_backend": receipt["execution_backend"],
                            "elapsed_seconds": receipt["elapsed_seconds"],
                            "artifacts": receipt["artifacts"]}
                    if receipt["status"] != "succeeded":
                        item["failure_context"] = worker.failure_context(worker_job["job_id"])
                    if "outputs/rhs-receipt.json" in receipt["artifacts"]:
                        import json
                        rhs = json.loads(worker.read_artifact(worker_job["job_id"], "outputs/rhs-receipt.json"))
                        item["cases"] = rhs["cases"]
                        item["execution_error"] = rhs["execution_error"]
                        item["budget_scope"] = "RHS queries, not total arithmetic"
                    executions.append(item)
            metadata = {key: state[key] for key in ("state_version", "budget") if key in state}
            candidate_budget = _candidate_development_budget(workstation, run, root)
            if candidate_budget is not None:
                metadata["candidate_development_budget"] = candidate_budget
            for field in ("development_diagnostics", "development_diagnostics_error"):
                if field in result:
                    metadata[field] = result[field]
            diagnostics = metadata.get("development_diagnostics")
            if diagnostics is not None and diagnostics["state_version"] != state["state_version"]:
                raise ProtocolError("Development diagnostics changed while reading; re-read required")
            return {"study_id": args["study_id"], "job": result["job"],
                    "observations": observations, "executions": executions, **metadata}
        resolved_tool, resolved, source = (_resolve_reference(workstation, call, args, root, run)
                                          if tool in REFERENCE_TOOLS else (tool, args, None))
        reference_receipt = None
        if source is not None:
            reference_receipt = {"arguments_sha256": digest(args), "tool": tool,
                "status": "prepared", "call_id": call["call_id"], "session_id": call["session_id"],
                "source_call_id": source["call_id"],
                "source_sha256": digest({"arguments": source["arguments"], "result": source["result"]}),
                "resolved_arguments": resolved}
            path = _reference_receipt_path(run, call)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, reference_receipt)
        if resolved_tool == "research.revision_create":
            result = submit_initial_revision(run, root, revision_tools.specification(resolved),
                                             resolved["approval_sha256"], actor="agent")
            result = {**result, "state_version": read_research_state(run)["state_version"],
                      "create_call_id": call["call_id"]}
            if source is not None:
                result["preview_call_id"] = source["call_id"]
        else:
            result = workstation.execute_agent_revision(resolved["revision_id"], resolved["manifest_sha256"])
            result = {"study_id": args["study_id"], "job": result["job"],
                      "manifest_sha256": resolved["manifest_sha256"], "status": "enqueued_not_completed"}
            if source is not None:
                result["create_call_id"] = source["call_id"]
        path = _reference_receipt_path(run, call) if tool in REFERENCE_TOOLS else _receipt_path(run, call["call_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {"arguments_sha256": digest(args), "tool": tool, "result": result}
        if reference_receipt is not None:
            receipt.update({**reference_receipt, "status": "completed", "result": result})
        write_json(path, receipt)
        return result
