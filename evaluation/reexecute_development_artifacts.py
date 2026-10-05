"""Re-execute frozen candidate code offline, outside original Study/Job state.

No models, candidate selection, feedback delivery, or confirmation inputs.
"""
import argparse
from pathlib import Path

from popper.core import digest, file_hash, read_json, write_json
from popper.research.oscillator_execution import execute_metered
from popper.research.workers.local import LocalWorker


def main(root, output):
    if output.exists():
        raise RuntimeError("Reproduction output must be new; interrupted runs require audit")
    audit = read_json(root / "cohort-development-audit-v1.json")
    campaign = read_json(root / "campaign.json")
    assert file_hash(root / "campaign.json") == audit["campaign_sha256"]
    entries = {row["run_id"]: row for row in campaign["runs"]}
    output.mkdir(parents=True)
    results = []
    for run in audit["runs"]:
        entry = entries[run["run_id"]]
        research = Path(entry["research"])
        if not run["candidate_trials"]:
            continue
        worker = LocalWorker(research / "jobs", research / "revisions")
        binding = read_json(research / "oscillator-worker-binding.json")
        for path, sha in binding["protected_files"].items():
            assert file_hash(Path(path)) == sha
        for index, trial in enumerate(run["candidate_trials"]):
            evidence_file = Path(trial["evidence_file"])
            assert file_hash(evidence_file) == trial["evidence_sha256"]
            evidence = read_json(evidence_file)
            if "job" not in evidence:
                evidence = evidence["result"]
            assert len(evidence["executions"]) == 1
            old_job = evidence["executions"][0]["job_id"]
            spec = worker.read_manifest(old_job)["spec"]
            revision = worker.revisions.verify(spec["revision_id"])
            code = worker.revisions.path(spec["revision_id"]) / "code"
            assert file_hash(code.parent / "revision.json") == spec["revision_manifest_sha256"]
            inputs = {item["role"]: Path(item["source"]) for item in spec["inputs"]}
            for item in spec["inputs"]:
                assert file_hash(Path(item["source"])) == item["sha256"]
            import json
            old_receipt = json.loads(worker.read_artifact(old_job, "outputs/rhs-receipt.json"))
            destination = output / run["run_id"] / str(index + 1)
            receipt = execute_metered(code / spec["entrypoint"],
                read_json(inputs["development_features_without_labels"]),
                read_json(inputs["frozen_intervention"]), destination,
                job_id=f"offline-reproduction-{run['run_id']}-{index + 1}",
                protected_files=list(binding["protected_files"]), timeout=spec["timeout_seconds"],
                code_files={name: code / name for name in revision["files"]},
                trace_execution=spec["require_edit_coverage"])
            signature = lambda r: [(c["status"], c["query_count"]) for c in r["cases"]]
            result = {"run_id": run["run_id"], "original_worker_job": old_job,
                "revision_id": spec["revision_id"],
                "payload_identical": digest(receipt["payload"]) == digest(old_receipt["payload"]),
                "case_status_and_counts_identical": signature(receipt) == signature(old_receipt),
                "receipt_sha256": file_hash(destination / "receipt.json")}
            results.append(result)
            write_json(output / "progress.json", {"results": results, "complete": False})
            print(result, flush=True)
    write_json(output / "result.json", {"results": results, "complete": True,
        "scope": "Local fresh-Python development-only code replay, not third-party verification",
        "no_model_calls": True, "no_kernel_writes": True, "no_confirmation_consumption": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    main(args.root.resolve(), args.output.resolve())
