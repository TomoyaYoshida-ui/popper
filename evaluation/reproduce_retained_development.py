"""Offline re-score of retained development artifacts, not a solver re-execution.

Run with an installed Popper package from a fresh virtual environment. Never
calls a model, dispatches a Job, or consumes independent-confirmation resources.
"""
import argparse
import json
import math
from pathlib import Path

from popper.core import file_hash, read_json, write_json
from popper.domains.oscillator_trajectory import PACK
from popper.research.workers.local import LocalWorker


def reproduce(root):
    audit = read_json(root / "cohort-development-audit-v1.json")
    campaign = read_json(root / "campaign.json")
    assert file_hash(root / "campaign.json") == audit["campaign_sha256"]
    entries = {row["run_id"]: row for row in campaign["runs"]}
    results = []
    for run in audit["runs"]:
        entry = entries[run["run_id"]]
        worker = None
        for trial in run["candidate_trials"]:
            evidence_path = Path(trial["evidence_file"])
            assert file_hash(evidence_path) == trial["evidence_sha256"]
            if not trial["observations"]:
                results.append({"run_id": run["run_id"], "job_id": trial["job"]["job_id"],
                                "status": trial["job"]["status"], "score": None,
                                "reason": "No Observation; failed/incomplete execution is not scored as success"})
                continue
            if worker is None:
                research = Path(entry["research"])
                worker = LocalWorker(research / "jobs", research / "revisions")
            evidence = read_json(evidence_path)
            if "result" in evidence and "job" not in evidence:
                evidence = evidence["result"]
            assert len(evidence["executions"]) == 1
            job_id = evidence["executions"][0]["job_id"]
            receipt = worker.collect(job_id)
            payload = json.loads(worker.read_artifact(job_id, "outputs/trajectories-0.json"))
            rows = read_json(Path(entry["project"]) / "dev.json")
            score = PACK.score(rows, payload, "trajectory_error")
            assert math.isclose(score, trial["observations"][0]["value"], rel_tol=0, abs_tol=1e-12)
            rhs = json.loads(worker.read_artifact(job_id, "outputs/rhs-receipt.json"))
            count = sum(case["query_count"] for case in rhs["cases"])
            assert count == trial["accepted_rhs_queries"]
            results.append({"run_id": run["run_id"], "job_id": trial["job"]["job_id"],
                            "worker_job_id": job_id, "status": trial["job"]["status"],
                            "score": score, "accepted_rhs_queries": count,
                            "verified_receipt": receipt})
    return {"scope": "Offline retained-artifact scoring and receipt reproduction only",
            "not_solver_reexecution": True, "not_third_party_verification": True,
            "no_model_calls": True, "no_confirmation_consumption": True,
            "campaign_sha256": audit["campaign_sha256"], "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("Refusing to overwrite reproduction evidence")
    result = reproduce(args.root.resolve())
    import popper
    import sys
    result["python_executable"] = sys.executable
    result["installed_package"] = str(Path(popper.__file__).resolve())
    write_json(args.output, result)
    print(json.dumps({"cases": len(result["results"]),
                      "scored_observations": sum(r["score"] is not None for r in result["results"]),
                      "installed_package": result["installed_package"]}))
