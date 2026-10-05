"""Explicitly audited application binding of unchanged model-authored code."""
import argparse
from contextlib import closing
from pathlib import Path
import re
import time

from popper.core import digest, file_hash, read_json, write_json
from popper.research.agent_revisions import execute as query_job
from popper.research.oscillator_campaign import load_campaign
from popper.research.readout import read_research_state
from popper.research.revision_drafts import preview_initial_revision, submit_initial_revision
from popper.server import Workstation
from popper.sessions import AgentSessionStore


def extract_authored_source(text, candidate_index, *, explicit_repair_scope=False):
    if type(candidate_index) is not int or not 0 <= candidate_index < 6:
        raise ValueError("Invalid explicit candidate index")
    prose = text.split("```", 1)[0]
    if re.search(r"(?:^|\n)\s*(?:#+\s*)?(?:Decision\s*:\s*Stop\b|决策\s*[：:]\s*停止)", prose, re.I):
        raise RuntimeError("Model chose stop; displayed source is not an execution proposal")
    explicit_index = bool(re.search(
        r"(?:candidate[_ ]index|候选\s*index)\s*[=:]?\s*" + str(candidate_index) + r"\b",
        text.replace("**", ""), re.I))
    if not explicit_index and not (explicit_repair_scope and re.search(r"Decision:\s*Repair", prose, re.I)):
        raise RuntimeError("No unambiguous candidate or explicitly scoped repair decision")
    blocks = re.findall(r"```python\r?\n(.*?)\r?\n```", text, re.S)
    if len(blocks) != 1:
        raise RuntimeError("No inference of missing code or merging multiple sources")
    replacement = blocks[0] + "\n"
    compile(replacement, "model.py", "exec")  # Never execute candidate in host.
    return replacement, explicit_index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id", choices=[f"popper_loop-{i}" for i in range(1, 4)])
    parser.add_argument("--candidate-index", type=int, required=True)
    parser.add_argument("--execute-existing-binding", action="store_true")
    parser.add_argument("--repair-request-version", type=int)
    parser.add_argument("--projection-feedback", action="store_true")
    args = parser.parse_args()
    assert 0 <= args.candidate_index < 6
    root = Path(__file__).parent / "runs/agent-oscillator-2026-10-01-v3/formal-campaign-qwen-v2"
    manifest, run = load_campaign(root, args.run_id)
    folder = root / args.run_id
    repair = args.repair_request_version
    assert not (repair and args.projection_feedback)
    prefix = "projection-next-candidate" if args.projection_feedback else "repair-candidate" if repair else "decision-candidate"
    target = folder / f"{prefix}-binding-v1.json"
    if target.exists() and not args.execute_existing_binding:
        raise RuntimeError("Binding already recorded; reconcile receipts rather than redispatch")
    if args.execute_existing_binding and not target.exists():
        raise RuntimeError("No existing binding to reconcile")
    source_result = "projection-feedback-result-v1.json" if args.projection_feedback else f"failed-candidate-decision-result-v{repair}.json" if repair else "development-decision-result-v1.json"
    authored = read_json(folder / source_result)
    assert authored["turn"]["status"] == "completed" and not authored["tool_calls"]
    text = authored["turn"]["assistant_message"]
    replacement, explicit_index = extract_authored_source(text, args.candidate_index, explicit_repair_scope=bool(repair))
    if not explicit_index:
        # The current request explicitly identifies this failed candidate. Do
        # not infer ownership from method names or fabricate a model tool call.
        previous_binding = read_json(folder / "decision-candidate-binding-v1.json")
        assert previous_binding["candidate_index"] == args.candidate_index
    with closing(AgentSessionStore.readonly(run["project"])) as store:
        assert not store.queued_agent_jobs()
        assert all(j["status"] not in {"queued", "running"} for j in store.agent_jobs(authored["turn"]["session_id"]))
    state = read_research_state(run["research"])
    registered = read_json(Path(run["research"]) / "research.json")
    hypothesis_id = registered["candidate_hypothesis_ids"][args.candidate_index]
    design = next(d for d in state["designs"] if d["hypothesis_id"] == hypothesis_id)
    assert not any(o["design_id"] == design["design_id"] for o in state["observations"])
    spec = {"study_id": state["study"]["entity_id"], "hypothesis_id": hypothesis_id,
            "design_id": design["design_id"], "expected_state_version": state["state_version"],
            "path": "model.py", "expected_sha256": file_hash(Path(run["project"]) / "model.py"),
            "replacement": replacement}
    if repair:
        previous_binding = read_json(folder / "decision-candidate-binding-v1.json")
        assert previous_binding["candidate_index"] == args.candidate_index
        parent = previous_binding["result"]["revision_id"]
        previous_evidence = read_json(folder / "decision-candidate-evidence-v1.json")
        assert previous_evidence["job"]["status"] == "failed" and not previous_evidence["observations"]
        spec["parent_revision_id"] = parent
        spec["expected_sha256"] = file_hash(Path(run["research"]) / "revisions" / parent / "code/model.py")
    if args.execute_existing_binding:
        prior = read_json(target)
        assert prior["source_response_sha256"] == digest(text)
        assert prior["spec"]["replacement"] == replacement
        assert prior["candidate_index"] == args.candidate_index
        created = prior["result"]
    else:
        preview = preview_initial_revision(run["research"], run["project"], spec)
        review = {"source_turn_id": authored["turn"]["turn_id"], "source_response_sha256": digest(text),
              "code_source": "single complete Python fence; no numerical edits", "candidate_index": args.candidate_index,
              "operator_intervention": "review final model decision and bind it to the registered candidate identity, then submit application creation/execution",
              "native_model_tool_call": False, "unassisted_execution": False,
              "model_emitted_candidate_index": bool(explicit_index),
              "mechanism_claim_alignment": "not established by this binding; record actual repair code, do not claim old method label was validated",
              "approval": "user delegated all task-scoped approvals; original science and caps unchanged",
              "spec": spec, "preview": preview}
        write_json(folder / f"{prefix}-preview-v1.json", review)
        created = submit_initial_revision(run["research"], run["project"], spec, preview["proposal_sha256"], actor="agent")
        write_json(target, {**review, "result": created})
    ws = Workstation(("127.0.0.1", 0), run["project"], trusted_local=True,
                     research_runs=[run["research"]],
                     chat_base_url=manifest["base_url"], chat_model=manifest["model"],
                     evaluation_budget_path=manifest["budget_ledger"])
    try:
        dispatched = ws.execute_agent_revision(created["revision_id"], created["manifest_sha256"])
        write_json(folder / f"{prefix}-dispatch-v1.json", dispatched)
        print("dispatched", dispatched["job"]["job_id"], flush=True)
        while ws.wait_for_jobs(timeout=1):
            time.sleep(0.1)
        evidence = query_job(ws, {"tool_name": "research.revision_job",
                                 "arguments": {"study_id": spec["study_id"], "job_id": dispatched["job"]["job_id"]}},
                             Path(run["project"]))
        write_json(folder / f"{prefix}-evidence-v1.json", evidence)
        print(evidence["job"]["status"], evidence["observations"], flush=True)
    finally:
        ws.server_close()


if __name__ == "__main__":
    main()
