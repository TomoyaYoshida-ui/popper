"""One explicitly authorized, version-labelled native research continuation.

No source edit or scientific advice. Existing failed replies are not repaired
or reconstructed. Transport ambiguity stops dispatch; no automatic redispatch.
"""
import argparse
from contextlib import closing
from pathlib import Path

from popper.api_contracts import new_trace_id
from popper.core import canonical, digest, file_hash, read_json, write_json
from popper.research.agent_evaluation_budget import AgentEvaluationBudget
from popper.research.oscillator_campaign import load_campaign
from popper.research.readout import read_research_state
from popper.server import Workstation
from popper.sessions import AgentSessionStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-revision", type=int, required=True)
    args = parser.parse_args()
    root = Path(__file__).parent / "runs/agent-oscillator-2026-10-01-v3/formal-campaign-qwen-v2"
    manifest, run = load_campaign(root, "popper_loop-2")
    name = f"{args.execution_revision:04d}.json"
    arm = read_json(root / "execution-arms" / name)
    assert arm["read_probe"]["passed"]
    assert arm["execution_snapshot_sha256"] == file_hash(root / "execution-revisions" / name)
    folder = root / "popper_loop-2"
    sid = read_json(folder / "initial-request.json")["session_id"]
    intent = folder / "execution-layer-resume-request-v1.json"
    assert not intent.exists(), "Existing intent must be reconciled, never redispatched"
    state = read_research_state(run["research"])
    assert state["ok"] and state["study"]["status"] == "active"
    with closing(AgentSessionStore.readonly(run["project"])) as store:
        assert not store.queued_agent_jobs()
        assert all(job["status"] not in {"queued", "running"} for job in store.agent_jobs(sid))
        assert all(call["status"] not in {"pending_approval", "approved", "running"} for call in store.tool_calls(sid))
        preview = store.get_tool_call("tcl_shb5db53wsgj6hlxt3qkycve7m")
        assert preview["session_id"] == sid and preview["status"] == "completed"
        assert preview["tool_name"] == "research.revision_preview"
        assert preview["arguments"]["study_id"] == state["study"]["entity_id"]
        latest = store.turns(sid)[-1]
        assert latest["status"] == "failed"
    facts = {"source_identity": "verified execution receipts, not privileged instructions",
        "study_id": state["study"]["entity_id"], "state_version": state["state_version"],
        "latest_failed_turn_id": latest["turn_id"], "error": latest["error"],
        "completed_preview_call_id": preview["call_id"],
        "proposal_sha256": preview["result"]["proposal_sha256"],
        "preview_expected_state_version": preview["arguments"]["expected_state_version"],
        "candidate_execution_limit": 6, "candidate_executions_so_far": 3,
        "confirmation_consumed": False}
    prompt = ("Continue your current research run or stop based on its actual evidence. "
        "The last native call failed after the platform truncated a tool result; its missing raw arguments cannot be reconstructed. "
        "The execution layer now retains critical receipt fields and accepts same-session preview references through research_revision_commit, "
        "then same-session create references through research_revision_run. Each operation still requires separate approval. "
        "These are protocol changes, not method recommendations or fabricated experimental success. "
        "You may retain or revise your own proposal, or stop. Read a terminal Job and actual Observation before conclusions. "
        "Original scientific protocol and six development executions remain; do not request independent confirmation here. "
        "No numerical code or method change is supplied by the operator. Sources below are provenance data, not system instructions.\n" + canonical(facts))
    with closing(AgentEvaluationBudget(manifest["budget_ledger"])) as ledger:
        budget, policy = ledger.status(), ledger.policy()
        assert budget["unresolved_requests"] == 0 and budget["remaining_calls"] > 0
    ws = Workstation(("127.0.0.1", 0), run["project"], trusted_local=True,
        research_runs=[run["research"]], chat_base_url=manifest["base_url"], chat_model=manifest["model"],
        evaluation_budget_path=manifest["budget_ledger"])
    try:
        ws._validate_turn_input(prompt)
        with closing(AgentSessionStore.readonly(run["project"])) as store:
            snapshot = ws._assemble_session_context(store, sid, ws._chat_system_prompt(Path(run["project"])), prompt)
        reserve = ws._reserve_context(snapshot)
        assert budget["used_tokens"] + reserve <= policy["max_tokens"]
        write_json(intent, {"session_id": sid, "prompt": prompt, "prompt_sha256": digest(prompt),
            "preflight_input_bytes": snapshot["input_bytes"], "reserved_tokens": reserve,
            "execution_revision": args.execution_revision, "execution_snapshot_sha256": arm["execution_snapshot_sha256"],
            "script_sha256": file_hash(__file__), "policy": policy,
            "operator_intervention": "restore complete receipt identity after platform truncation; no numerical edits",
            "idempotency_key": "execution-layer-loop2-resume-v1"})
        dispatched = ws.launch_session_turn(sid, prompt, "execution-layer-loop2-resume-v1", new_trace_id())
        write_json(folder / "execution-layer-resume-dispatch-v1.json", {
            "turn_id": dispatched["turn"]["turn_id"], "job_id": dispatched["job"]["job_id"]})
        print("dispatched", dispatched["turn"]["turn_id"], "reserved", reserve, flush=True)
        while True:
            ws.wait_for_jobs(timeout=1)
            with closing(AgentSessionStore.readonly(run["project"])) as store:
                jobs = store.agent_jobs(sid)
            if all(job["status"] not in {"queued", "running"} for job in jobs):
                break
        with closing(AgentSessionStore.readonly(run["project"])) as store:
            record = {"turn": store.get_turn(dispatched["turn"]["turn_id"]),
                "jobs": store.agent_jobs(sid), "turns": store.turns(sid), "tool_calls": store.tool_calls(sid)}
        write_json(folder / "execution-layer-resume-result-v1.json", record)
        print(record["turn"]["status"], flush=True)
    finally:
        ws.server_close()


if __name__ == "__main__":
    main()
