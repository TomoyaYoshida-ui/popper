"""Derived, read-only audit of all planned runs; not a Research Kernel projection."""
from contextlib import closing
from pathlib import Path

from popper.core import Experiment, ProtocolError, file_hash, read_json, write_json
from popper.research.agent_evaluation_budget import AgentEvaluationBudget
from popper.research.oscillator_campaign import load_campaign
from popper.research.readout import read_research_state
from popper.research.study_jobs import preview_study_action
from popper.sessions import AgentSessionStore


def main():
    root = Path(__file__).parent / "runs/agent-oscillator-2026-10-01-v3/formal-campaign-qwen-v2"
    target = root / "cohort-development-audit-v1.json"
    assert not target.exists(), "Preserve immutable previous audits"
    manifest, _ = load_campaign(root)
    with closing(AgentEvaluationBudget(manifest["budget_ledger"])) as ledger:
        budget = ledger.status()
    assert not budget["unresolved_requests"]
    result = {"scope": "development only; no scientific gain or full autonomy verdict",
              "derived_not_kernel_state": True, "budget": budget, "budget_token_limit": manifest["token_limit"],
              "budget_tokens_remaining": manifest["token_limit"] - budget["used_tokens"],
              "campaign_sha256": file_hash(root / "campaign.json"), "runs": []}
    evidence_names = ("development-tool-evidence-v1.json", "development-tool-evidence-v2.json",
                      "decision-candidate-evidence-v1.json", "repair-candidate-evidence-v1.json",
                      "projection-next-candidate-evidence-v1.json")
    for entry in manifest["runs"]:
        folder = root / entry["run_id"]
        initial = read_json(folder / "initial-request.json")
        with closing(AgentSessionStore.readonly(entry["project"])) as store:
            jobs = store.agent_jobs(initial["session_id"])
            assert all(j["status"] not in {"queued", "running"} for j in jobs)
            assert not store.queued_agent_jobs()
        trials = []
        seen = set()
        for name in evidence_names:
            path = folder / name
            if not path.exists():
                continue
            evidence = read_json(path)
            if "job" not in evidence and "result" in evidence:
                assert evidence.get("read_only") and not evidence.get("rerun")
                evidence = evidence["result"]
            # v2 of the original failed readout may be a receipt correction,
            # whereas loop-3 v2 is a different real candidate execution.
            if evidence["job"]["job_id"] in seen:
                continue
            seen.add(evidence["job"]["job_id"])
            assert evidence["job"]["status"] in {"succeeded", "failed", "cancelled"}
            cases = [case for execution in evidence["executions"] for case in execution.get("cases", [])]
            observations = evidence["observations"]
            assert len(observations) <= 1
            trials.append({"job": evidence["job"], "observations": observations,
                           "host_cases": cases,
                           "case_status_counts": {s: sum(c["status"] == s for c in cases)
                                                  for s in ("succeeded", "failed", "not_completed")},
                           "accepted_rhs_queries": sum(c["query_count"] for c in cases),
                           "elapsed_seconds": sum(e["elapsed_seconds"] for e in evidence["executions"]),
                           "evidence_file": str(path.resolve()), "evidence_sha256": file_hash(path)})
        assert len(trials) <= (6 if entry["arm"] == "popper_loop" else 1)
        with closing(Experiment(entry["project"], readonly=True)) as experiment:
            core = experiment.state()
        assert not core.get("test_consumed")
        row = {"run_id": entry["run_id"], "arm": entry["arm"], "candidate_trials": trials,
               "candidate_trial_count": len(trials), "confirmation_consumed": False,
               "model_jobs": [{k: job.get(k) for k in ("job_id", "turn_id", "status", "model_name", "total_tokens", "error")} for job in jobs]}
        if (Path(entry["research"]) / "research.json").exists():
            state = read_research_state(entry["research"])
            assert state["ok"] and state["experiment_graph"]["integrity"]["ok"]
            row["state_version"] = state["state_version"]
            row["study_id"] = state["study"]["entity_id"]
            row["claims_count"] = len(state["claims"])
            row["study_budget"] = state["budget"]
            row["fixed_control"] = read_json(folder / "baseline-control-result-v1.json")["observation"]
            try:
                row["confirmation_preview"] = preview_study_action(entry["research"], "confirm")
                row["confirmation_eligible"] = True
            except ProtocolError as error:
                row["confirmation_eligible"] = False
                row["confirmation_refusal"] = str(error)
        else:
            row.update({"study_id": None, "claims_count": 0, "confirmation_eligible": False,
                        "confirmation_refusal": "No Study: incomplete authored initial proposal, no operator-filled hypotheses"})
        result["runs"].append(row)
    result["orchestration_sources_current_checkpoint"] = {
        str(path): {"sha256": file_hash(path), "source": path.read_text(encoding="utf-8")}
        for path in Path(__file__).parent.glob("*decision*.py")}
    result["checkpoint_limitation"] = "Captured after execution, not proof that earlier edited helper versions were preregistered; exact paid input snapshots and immutable numerical Revision sources are retained separately."
    write_json(target, result)
    print("audited", len(result["runs"]), "runs", sum(r["candidate_trial_count"] for r in result["runs"]),
          "candidate jobs", "eligible confirmations", sum(r["confirmation_eligible"] for r in result["runs"]),
          "remaining tokens", result["budget_tokens_remaining"])


if __name__ == "__main__":
    main()
