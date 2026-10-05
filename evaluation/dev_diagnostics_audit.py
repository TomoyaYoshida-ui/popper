"""Read verified development artifacts; never export reference arrays."""
import json
import math
from pathlib import Path

from popper.core import read_json, write_json, file_hash
from popper.domains.oscillator_trajectory import PACK
from popper.research.readout import read_research_state
from popper.research.workers.local import LocalWorker


def main():
    root = Path(__file__).parent / "runs/agent-oscillator-2026-10-01-v3/formal-campaign-qwen-v2"
    for entry in read_json(root / "campaign.json")["runs"]:
        if entry["arm"] != "popper_loop":
            continue
        target = root / entry["run_id"] / "development-diagnostics-v1.json"
        if target.exists():
            raise RuntimeError(f"Refusing to overwrite {target}")
        run = Path(entry["research"])
        worker = LocalWorker(run / "jobs", run / "revisions")
        state = read_research_state(run)
        control = read_json(root / entry["run_id"] / "baseline-control-result-v1.json")["observation"]
        control_run = next(r for r in state["runs"] if r["run_id"] == control["run_id"])
        matches = [j for j in worker.list_jobs() if j["status"] == "succeeded"
                   and worker.read_manifest(j["job_id"])["spec"]["revision_id"] == control_run["revision_id"]]
        assert len(matches) == 1
        evidence_name = "development-tool-evidence-v2.json" if entry["run_id"] == "popper_loop-3" else "development-tool-evidence-v1.json"
        evidence = read_json(root / entry["run_id"] / evidence_name)
        rows = read_json(Path(entry["project"]) / "dev.json")
        report = {"run_id": entry["run_id"], "scope": "dev", "reference_disclosed": False,
                  "scorer_sha256": file_hash(Path("popper/domains/oscillator_trajectory.py")),
                  "energy_sampling": "65 prescribed trajectory samples, not all internal steps", "results": {}}
        for label, jid, observation in (
            ("candidate", evidence["executions"][0]["job_id"], evidence["observations"][0]),
            ("fixed_rk4_control", matches[0]["job_id"], control),
        ):
            receipt = worker.collect(jid)
            payload = json.loads(worker.read_artifact(jid, "outputs/trajectories-0.json"))
            cases = PACK.diagnostics(rows, payload)
            score = PACK.score(rows, payload, "trajectory_error")
            assert abs(score - observation["value"]) < 1e-12
            rhs = json.loads(worker.read_artifact(jid, "outputs/rhs-receipt.json"))
            systems = {}
            for system in sorted({c["system"] for c in cases}):
                group = [c for c in cases if c["system"] == system]
                energy = [c["energy_drift"] for c in group if not c["failed"]]
                systems[system] = {"case_count": len(group), "failed_cases": sum(c["failed"] for c in group),
                                   "mean_trajectory_error": math.fsum(c["trajectory_error"] for c in group) / len(group),
                                   "mean_sampled_energy_drift_successful": math.fsum(energy) / len(energy) if energy else None}
            report["results"][label] = {"observation_id": observation["observation_id"], "worker_job_id": jid,
                "mean_trajectory_error": score, "systems": systems, "cases": cases,
                "trusted_rhs_queries": sum(c["query_count"] for c in rhs["cases"]),
                "receipt": receipt}
        write_json(target, report)
        print(entry["run_id"], {k: v["mean_trajectory_error"] for k, v in report["results"].items()})


if __name__ == "__main__":
    main()
