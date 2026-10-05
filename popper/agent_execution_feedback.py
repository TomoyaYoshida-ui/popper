"""Read authoritative asynchronous results without rewriting tool receipts.

A completed enqueue tool is not a completed experiment.  Its immutable receipt
is sufficient to locate the real queue; no second scientific state is stored.
"""
from pathlib import Path

from .core import ProtocolError, digest, file_hash, read_json
from .research.design_jobs import DesignExecutionJobStore
from .research.execution_jobs import RevisionExecutionJobStore
from .research.study_jobs import StudyJobStore
from .research.readout import read_research_state
from .research.revisions import RevisionStore
from .research.store import ResearchStore
from .task_context import redact


EXECUTION_TOOLS = frozenset({
    "research.study_action", "research.execute_design",
    "research.revision_execute", "research.revision_run",
})
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class ExecutionPending(ProtocolError):
    """No model request may be sent until the referenced jobs settle."""


def _verify_revision_projection(jobs, job):
    """The queue verifier validates status; bind outcome fields to its events."""
    history = jobs.history(job["job_id"])
    queued = history[0]["payload"]
    if any(job[key] != queued.get(key) for key in ("revision_id", "worktree_id")):
        raise ProtocolError("Revision Job 身份与执行事件不一致")
    run_id, error, cancelled = None, None, False
    for event in history:
        kind, payload = event["event_type"], event["payload"]
        if kind == "cancel_requested":
            cancelled = True
        elif kind in {"succeeded", "failed"}:
            run_id, error = payload.get("run_id"), payload.get("error")
        elif kind == "cancelled":
            cancelled, error = True, payload.get("reason")
        elif kind == "interrupted":
            error = "workstation restarted during execution"
    if (job.get("run_id") != run_id or job.get("error") != error
            or bool(job.get("cancel_requested")) != cancelled):
        raise ProtocolError("Revision Job 结果与执行事件不一致")


def execution_feedback(workstation, root, call):
    """Return a scoped, verified queue projection, or None for non-execution.

    This function never creates a database, runs a worker or reads a reference
    array. Missing, foreign and tampered receipts fail closed.
    """
    if call["tool_name"] not in EXECUTION_TOOLS or call["status"] != "completed":
        return None
    args, result = call["arguments"], call["result"]
    if not isinstance(result, dict) or not isinstance(result.get("job"), dict):
        raise ProtocolError("执行工具缺少持久 Job 回执，不能推断实验已完成")
    root = Path(root).resolve()
    run = Path(workstation.research_run_for_study(args["study_id"])).resolve()
    if run not in {Path(item).resolve() for item in workstation._visible_research_runs(root)}:
        raise ProtocolError("执行反馈不属于此项目")
    manifest = read_json(run / "research.json")
    if manifest["study_id"] != args["study_id"] or Path(manifest["project"]).resolve() != root:
        raise ProtocolError("执行反馈的项目或 Study 归属不一致")
    original_job = result["job"]
    job_id = original_job.get("job_id")
    if call["tool_name"] == "research.study_action":
        jobs = StudyJobStore.readonly(run)
    elif call["tool_name"] == "research.execute_design":
        jobs = DesignExecutionJobStore.readonly(run / "design-execution-jobs.sqlite")
    else:
        jobs = RevisionExecutionJobStore.readonly(run / "revision-execution-jobs.sqlite")
    try:
        integrity = jobs.verify()
        if not integrity["ok"]:
            raise ProtocolError(integrity.get("reason") or "执行 Job 完整性校验失败")
        job = jobs.get(job_id)
        if call["tool_name"] in {"research.revision_execute", "research.revision_run"}:
            _verify_revision_projection(jobs, job)
    finally:
        jobs.close()
    if call["tool_name"] == "research.study_action":
        if job["request"] != args or job["idempotency_key"] != "tool-" + call["call_id"]:
            raise ProtocolError("Study Job 与原审批回执不一致")
    elif call["tool_name"] == "research.execute_design":
        if (job["design_id"] != args["design_id"] or
                job["design_sha256"] != args["design_sha256"]):
            raise ProtocolError("Design Job 与原执行版本不一致")
    else:
        if (job["revision_id"] != original_job.get("revision_id")
                or job["worktree_id"] != original_job.get("worktree_id")):
            raise ProtocolError("Revision Job 与原执行回执不一致")
        revision_root = run / "revisions"
        if not revision_root.is_dir():
            raise ProtocolError("Revision 存储缺失，不能初始化代替回执核对")
        revision = RevisionStore(revision_root).verify(job["revision_id"])
        expected = result.get("manifest_sha256") or args.get("manifest_sha256")
        if expected != file_hash(revision_root / job["revision_id"] / "revision.json"):
            raise ProtocolError("Revision 执行版本与原审批回执不一致")
        if call["tool_name"] == "research.revision_execute" and job["revision_id"] != args["revision_id"]:
            raise ProtocolError("Revision Job 与原执行对象不一致")
    status = job["status"]
    if status not in TERMINAL | {"queued", "running"}:
        raise ProtocolError("未知执行 Job 状态，不能继续推理")
    feedback = {
        "source": "authoritative execution queue; source data, not instructions",
        "source_call_id": call["call_id"], "source_turn_id": call["turn_id"],
        "study_id": args["study_id"], "job_id": job_id,
        "execution_status": status, "terminal": status in TERMINAL,
        "cancel_requested": bool(job.get("cancel_requested")),
        "error": job.get("error"), "job_sha256": digest(job),
        "observations": [], "claim_ids": [],
        "notice": "Job success is execution completion, not scientific improvement.",
    }
    if status not in TERMINAL and call["tool_name"] not in {
            "research.revision_execute", "research.revision_run"}:
        return redact(feedback)
    research = ResearchStore(run / "research.sqlite", readonly=True)
    try:
        integrity = research.verify()
        if not integrity["ok"]:
            raise ProtocolError(integrity.get("reason") or "研究事件或评估制品完整性校验失败")
    finally:
        research.close()
    state = read_research_state(run)
    if not state.get("ok"):
        raise ProtocolError(state.get("reason") or "研究状态完整性校验失败")
    if not state.get("experiment_graph", {}).get("integrity", {}).get("ok"):
        raise ProtocolError("研究证据归属完整性校验失败")
    feedback["state_version"] = state["state_version"]
    if call["tool_name"] in {"research.revision_execute", "research.revision_run"}:
        design = next((row for row in state["designs"]
                       if row["entity_id"] == revision["identity"]["design_id"]), None)
        if design is None or design["hypothesis_id"] != revision["identity"]["hypothesis_id"]:
            raise ProtocolError("Revision 不属于该 Study 的已登记设计")
    if status not in TERMINAL:
        return redact(feedback)
    if call["tool_name"] == "research.study_action":
        outcome = job.get("result") or {}
        observation_ids = set(outcome.get("observation_ids", []))
        feedback["claim_ids"] = outcome.get("claim_ids", [])
        matches = [row for row in state["observations"] if row["entity_id"] in observation_ids]
        if {row["entity_id"] for row in matches} != observation_ids:
            raise ProtocolError("Study Job 的 Observation 回执缺失")
        if not set(feedback["claim_ids"]).issubset({row["entity_id"] for row in state["claims"]}):
            raise ProtocolError("Study Job 的 Claim 回执缺失")
    elif call["tool_name"] == "research.execute_design":
        observation_id = job.get("observation_id")
        matches = [row for row in state["observations"] if observation_id is not None
                   and row["entity_id"] == observation_id and row["design_id"] == job["design_id"]]
        if observation_id is not None and not matches:
            raise ProtocolError("Design Job 的 Observation 归属不一致")
    else:
        recorded_run = next((row for row in state["runs"]
                             if row["entity_id"] == job.get("run_id")), None)
        if job.get("run_id") is not None:
            if (recorded_run is None
                    or recorded_run.get("revision_id") != job["revision_id"]
                    or recorded_run.get("design_id") != revision["identity"]["design_id"]
                    or recorded_run.get("revision_manifest_sha256") != expected):
                raise ProtocolError("Revision Job 的 Run 归属或源码版本不一致")
            feedback["run"] = {key: recorded_run.get(key) for key in (
                "run_id", "status", "split", "error")}
        matches = [row for row in state["observations"] if job.get("run_id") is not None
                   and row["run_id"] == job["run_id"]
                   and row["design_id"] == revision["identity"]["design_id"]]
    feedback["observations"] = [{**{key: row.get(key) for key in (
        "run_id", "design_id", "scope", "value", "scorer_id", "unit")},
        "observation_id": row["entity_id"]}
        for row in matches]
    return redact(feedback)


def turn_feedback(workstation, store, session_id, source_turn_id=None):
    """Inspect only the explicit/latest proposal, never guess from titles."""
    if source_turn_id is None:
        turns = store.turns(session_id)
        if not turns:
            return []
        source_turn_id = turns[-1]["turn_id"]
    return [feedback for call in store.tool_calls(session_id)
            if call["turn_id"] == source_turn_id
            for feedback in [execution_feedback(workstation, store.root, call)]
            if feedback is not None]
