"""Re-score or explicitly replay a new, hash-bound development closeout bundle.

verify reads retained artifacts only. replay separately invokes the existing
Windows Low-integrity/RHS broker in fresh directories. Neither command invokes
a model, changes research/core databases or consumes confirmation resources.
Replays are labelled new local reproduction evidence, never original scores.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import re
import sys
from pathlib import Path

from popper.core import ProtocolError, canonical, digest, file_hash, portable_path, read_json
from popper.domains.oscillator_trajectory import PACK
from popper.research.development_evidence import _worker_artifacts
from popper.research.evaluation_service import scoring_code_hash
from popper.research.oscillator_execution import execute_metered, require_windows_boundary
from popper.research.workers.base import JobSpec
from popper.task_context import redact

if __package__:
    from .closeout_development import _immutable_revision
else:
    # Absolute script invocation from outside the checkout allows a clean venv
    # to import its installed Popper instead of the repository's source package.
    from closeout_development import _immutable_revision


def load_bundle(directory):
    directory = Path(directory).resolve()
    index = read_json(directory / "files.json")
    for name, expected in index["files_sha256"].items():
        if not isinstance(name, str) or Path(name).name != name or portable_path(name) != name:
            raise ProtocolError("Closeout file index path invalid")
        path = directory / name
        if path.is_symlink() or not path.is_file() or file_hash(path) != expected:
            raise ProtocolError("Closeout bundle file changed: " + name)
    if not {"report.json", "reproduction-manifest.json"}.issubset(index["files_sha256"]):
        raise ProtocolError("Closeout bundle index missing required manifests")
    report = read_json(directory / "report.json")
    manifest = read_json(directory / "reproduction-manifest.json")
    if (report.get("scope") != "dev" or manifest.get("scope") != "dev"
            or report.get("report_sha256") != digest({k: v for k, v in report.items() if k != "report_sha256"})
            or manifest.get("manifest_sha256") != digest({k: v for k, v in manifest.items() if k != "manifest_sha256"})
            or manifest.get("report_sha256") != report["report_sha256"]
            or index.get("report_sha256") != report["report_sha256"]):
        raise ProtocolError("Closeout report/manifest identity mismatch")
    campaign_path = Path(report["campaign"]["path"])
    if file_hash(campaign_path) != manifest["campaign_sha256"]:
        raise ProtocolError("Original campaign changed")
    campaign = read_json(campaign_path)
    if file_hash(Path(campaign["study_root"]) / "protocol.json") != campaign["protocol_sha256"]:
        raise ProtocolError("Original scientific protocol changed")
    scorer = Path(sys.modules[PACK.__class__.__module__].__file__).resolve()
    frozen_scorers = [sha for name, sha in campaign["implementation_sha256"].items()
        if name.replace("\\", "/").endswith("/popper/domains/oscillator_trajectory.py")]
    if len(frozen_scorers) != 1 or file_hash(scorer) != frozen_scorers[0]:
        raise ProtocolError("Frozen oscillator scorer changed")
    run_index = {run["run_id"]: run for run in report["runs"]}
    expected = []
    for run in report["runs"]:
        for trial in run["candidate_trials"]:
            expected.append((run["run_id"], "candidate", trial["job"]["job_id"], trial["job"]["revision_id"]))
        for trial in run["control_trials"]:
            if trial.get("has_observation"):
                expected.append((run["run_id"], "fixed_control", trial["job"]["job_id"], trial["job"]["revision_id"]))
    actual = [(entry["campaign_run_id"], entry["role"], entry.get("job_id"), entry["revision"]["revision_id"])
        for entry in manifest["entries"]]
    if sorted(expected, key=str) != sorted(actual, key=str):
        raise ProtocolError("Reproduction manifest dropped, duplicated or changed a cohort execution")
    campaign_runs = {row["run_id"]: row for row in campaign["runs"]}
    for entry in manifest["entries"]:
        run = run_index[entry["campaign_run_id"]]
        original = campaign_runs[entry["campaign_run_id"]]
        if (entry["study_id"] != run["study"]["study_id"]
                or entry["state_version"] != run["study"]["state_version"]
                or any(Path(entry[key]).resolve() != Path(original[key]).resolve() for key in ("project", "research"))):
            raise ProtocolError("Reproduction entry project/Study ownership mismatch")
    return report, manifest, campaign


def _expected_side(report, entry):
    run = next(row for row in report["runs"] if row["run_id"] == entry["campaign_run_id"])
    if entry["role"] == "candidate":
        trial = next(row for row in run["candidate_trials"] if row["job"]["job_id"] == entry["job_id"])
    else:
        trial = next(row for row in run["control_trials"] if row["job"]["revision_id"] == entry["revision"]["revision_id"])
    return (trial.get("evidence") or {}).get("candidate") or {}


def _verified_entry(entry, expected_side):
    research, project = Path(entry["research"]).resolve(), Path(entry["project"]).resolve()
    revision = _immutable_revision(research / "revisions", entry["revision"]["revision_id"])
    if revision != entry["revision"]:
        raise ProtocolError("Retained immutable Revision changed")
    sources = entry.get("source_digests", {})
    expected_input = sources.get("development_input")
    if expected_input is not None and file_hash(project / "dev.json") != expected_input:
        raise ProtocolError("Frozen development rows changed")
    if sources.get("scoring_service") is not None and sources["scoring_service"] != scoring_code_hash():
        raise ProtocolError("Frozen scoring service changed")
    rows = read_json(project / "dev.json")
    PACK.validate_rows(rows, "dev")
    features = PACK.project_inputs(rows)
    workers, scores, counts, incomplete_metering = [], [], 0, False
    prefix = "candidate" if entry["role"] == "candidate" else "control"
    for ref in entry.get("workers", []):
        job_id = ref["job_id"]
        if not isinstance(job_id, str) or not re.fullmatch(r"JOB-[0-9a-f]{24}", job_id):
            raise ProtocolError("Retained Worker ID invalid")
        path = research / "jobs" / job_id / "job.json"
        manifest = read_json(path)
        spec = JobSpec.from_payload(manifest["spec"])
        label = prefix + ":worker:" + str(ref["seed"])
        expected_manifest = sources.get(label + ":manifest")
        expected_run_id = entry.get("run_id")
        if (expected_run_id is None and entry["role"] == "candidate"
                and entry["status"] in {"failed", "cancelled", "interrupted"}
                and expected_manifest is not None):
            # Early closeout manifests copied queue.run_id, which remains null
            # when finish(error=...) records a failed job. The real execution
            # Run is content-addressed by immutable Revision + dev scope. Keep
            # the old bundle untouched and require its anchored Worker key to
            # match that exact identity; no Observation or score is inferred.
            expected_run_id = "RUN-" + digest({"revision_id": revision["revision_id"], "split": "dev"})[:24]
        run_key = bool(expected_run_id and re.fullmatch(re.escape(expected_run_id)
            + r":seed:" + str(ref["seed"]) + r":execution-v\d+", spec.idempotency_key))
        # Historical fixed RK4 jobs were keyed by their scoring request rather
        # than the research Run. Their exact JobSpec was already verified by the
        # host diagnostics and is anchored here by its manifest digest.
        legacy_control_key = (entry["role"] == "fixed_control" and expected_manifest is not None
            and re.fullmatch(r"[0-9a-f]{32}:seed:" + str(ref["seed"]) + r":dev-v\d+", spec.idempotency_key))
        if (spec.job_id != job_id or manifest["job_id"] != job_id or spec.revision_id != revision["revision_id"]
                or spec.revision_manifest_sha256 != revision["manifest_sha256"]
                or not (run_key or legacy_control_key)):
            raise ProtocolError("Retained Worker specification ownership changed")
        if expected_manifest is not None and file_hash(path) != expected_manifest:
            raise ProtocolError("Retained Worker manifest changed")
        actual_sources = {}
        receipt, artifacts = _worker_artifacts(research, spec, manifest, actual_sources, label)
        if actual_sources[label + ":receipt"] != ref["receipt_sha256"]:
            raise ProtocolError("Retained Worker receipt changed")
        input_items = [item for item in spec.inputs if item.role == "development_features_without_labels"]
        config_items = [item for item in spec.inputs if item.role == "frozen_intervention"]
        if len(spec.inputs) != 2 or len(input_items) != 1 or len(config_items) != 1:
            raise ProtocolError("Retained Worker input contract changed")
        for item in spec.inputs:
            if file_hash(item.source) != item.sha256:
                raise ProtocolError("Retained Worker input artifact changed")
        if read_json(input_items[0].source) != features:
            raise ProtocolError("Retained feature rows differ from the frozen development split")
        config = read_json(config_items[0].source)
        rhs_path = artifacts.get("outputs/rhs-receipt.json")
        if rhs_path is None:
            incomplete_metering = True
        else:
            rhs = read_json(rhs_path)
            if (rhs.get("kind") != "host_metered_rhs_execution" or rhs["job_id"] != job_id
                    or rhs["input_sha256"] != digest(features) or rhs["source_sha256"] != revision["files"]
                    or rhs["config_sha256"] != digest(config)):
                raise ProtocolError("Retained host RHS receipt ownership changed")
            count_rows = rhs.get("cases") or []
            if len(count_rows) != len(rows) or {item["case_id"] for item in count_rows} != {row["id"] for row in rows}:
                raise ProtocolError("Retained host RHS case coverage changed")
            row_index = {row["id"]: row for row in rows}
            for item in count_rows:
                if (type(item["query_count"]) is not int or not 0 <= item["query_count"] <= row_index[item["case_id"]]["budget"]
                        or item["budget"] != row_index[item["case_id"]]["budget"]):
                    raise ProtocolError("Retained host RHS query count invalid")
                counts += item["query_count"]
        payload_path = artifacts.get("outputs/trajectories-" + str(ref["seed"]) + ".json")
        if expected_side.get("observation_id"):
            if payload_path is None or receipt["status"] != "succeeded" or rhs_path is None:
                raise ProtocolError("Original Observation has no valid retained scored artifacts")
            payload = read_json(payload_path)
            if payload != rhs["payload"]:
                raise ProtocolError("Retained trajectory and host payload differ")
            scores.append(PACK.score(rows, payload, "trajectory_error"))
        workers.append({"ref": ref, "spec": spec, "receipt": receipt, "config": config})
    measured = math.fsum(scores) / len(scores) if scores else None
    if expected_side.get("observation_id"):
        if measured is None or not math.isclose(measured, expected_side["trajectory_error"], rel_tol=0, abs_tol=1e-12):
            raise ProtocolError("Reproduced score differs from original Observation")
    elif measured is not None:
        raise ProtocolError("Cannot manufacture an original score without an Observation")
    queries = None if incomplete_metering or not workers else counts
    if queries != expected_side.get("trusted_rhs_queries"):
        raise ProtocolError("Reproduced trusted queries differ from recorded development evidence")
    return {"revision": revision, "rows": rows, "features": features, "workers": workers,
        "score": measured, "trusted_rhs_queries": queries, "trusted_rhs_queries_known_partial": counts,
        "trusted_rhs_complete": queries is not None}


def _environment():
    import popper
    return {"python_executable": sys.executable, "python": platform.python_version(), "platform": platform.platform(),
        "installed_package": str(Path(popper.__file__).resolve()), "reproduction_source_sha256": file_hash(__file__)}


def verify_retained(directory):
    report, manifest, _ = load_bundle(directory)
    result = {"schema_version": 1, "scope": "dev", "kind": "retained_artifact_reproduction",
        "report_sha256": report["report_sha256"], "manifest_sha256": manifest["manifest_sha256"],
        "no_model_calls": True, "no_confirmation_consumption": True, "no_solver_reexecution": True,
        "not_third_party_verification": True, "environment": _environment(), "entries": []}
    for entry in manifest["entries"]:
        side = _expected_side(report, entry)
        verified = _verified_entry(entry, side)
        result["entries"].append({"campaign_run_id": entry["campaign_run_id"], "job_id": entry.get("job_id"),
            "revision_id": entry["revision"]["revision_id"], "role": entry["role"], "original_status": entry["status"],
            "observation_id": entry.get("observation_id"), "score": verified["score"],
            "trusted_rhs_queries": verified["trusted_rhs_queries"],
            "trusted_rhs_queries_known_partial": verified["trusted_rhs_queries_known_partial"],
            "trusted_rhs_complete": verified["trusted_rhs_complete"],
            "verified_worker_receipts": [{"job_id": row["ref"]["job_id"], "sha256": row["ref"]["receipt_sha256"],
                "status": row["receipt"]["status"]} for row in verified["workers"]],
            "no_observation_reason": entry.get("no_observation_reason")})
    result = redact(result)
    return result | {"reproduction_sha256": digest(result)}


def _protected_files(campaign):
    # Protection refers to actual original private files plus the currently
    # installed scorer/service/reference code and its existing bytecode caches.
    from popper.research import evaluation_service, oscillator_study
    paths = list((Path(campaign["study_root"]) / "evaluator-private").glob("*.json"))
    paths += [Path(run["project"]) / (split + ".json") for run in campaign["runs"] for split in ("train", "dev", "test")]
    paths += [Path(name) for name in campaign.get("inherited_protected_files_sha256", {})]
    sources = [Path(sys.modules[PACK.__class__.__module__].__file__), Path(evaluation_service.__file__), Path(oscillator_study.__file__)]
    paths += sources
    paths += [path for source in sources for path in source.parent.joinpath("__pycache__").glob(source.stem + ".*.pyc")]
    paths = sorted({path.resolve() for path in paths}, key=str)
    if not paths or any(not path.is_file() for path in paths):
        raise ProtocolError("Actual original private/scoring files required for isolated replay")
    return paths


def replay_isolated(directory, output):
    """Explicit separate development replay. No queue or Study is constructed."""
    output = Path(output).resolve()
    if output.exists():
        raise ProtocolError("Refusing to overwrite or resume a previous isolated replay")
    report, manifest, campaign = load_bundle(directory)
    if any(output.is_relative_to(Path(run[key]).resolve()) for run in campaign["runs"] for key in ("project", "research")):
        raise ProtocolError("Replay output must be outside original project/research roots")
    require_windows_boundary()
    verified = [(entry, _verified_entry(entry, _expected_side(report, entry))) for entry in manifest["entries"]]
    protected = _protected_files(campaign)
    source_hashes = {str(path): file_hash(path) for path in protected}
    from popper.research.oscillator_worker import _sealing_lock
    from popper.research.execution import assess_execution
    output.mkdir(parents=True, exist_ok=False)
    intent = {"scope": "dev", "kind": "isolated_development_replay", "report_sha256": report["report_sha256"],
        "manifest_sha256": manifest["manifest_sha256"], "environment": _environment(),
        "no_model_calls": True, "no_confirmation_consumption": True, "no_research_database_writes": True,
        "not_third_party_verification": True, "network_kernel_blocked": False,
        "protected_files_sha256": source_hashes, "entries": []}
    write_exclusive(output / "intent.json", intent)
    for index, (entry, data) in enumerate(verified):
        row = {"campaign_run_id": entry["campaign_run_id"], "role": entry["role"], "original_job_id": entry.get("job_id"),
            "revision_id": entry["revision"]["revision_id"], "original_observation_id": entry.get("observation_id"),
            "original_score": entry.get("trajectory_error"), "new_replay_not_original_observation": True,
            "workers": []}
        for worker in data["workers"]:
            spec, seed = worker["spec"], worker["ref"]["seed"]
            if Path(spec.entrypoint).name != spec.entrypoint:
                raise ProtocolError("Replay entrypoint must match the frozen metered domain root contract")
            destination = output / f"entry-{index + 1:02d}" / f"seed-{seed}"
            job_id = "JOB-" + digest({"closeout": report["report_sha256"], "original_worker": worker["ref"]["job_id"], "output": str(output)})[:24]
            code_root = Path(data["revision"]["manifest_path"]).parent / "code"
            try:
                with _sealing_lock(spec.timeout_seconds):
                    receipt = execute_metered(code_root / spec.entrypoint, data["features"], worker["config"],
                        destination, job_id=job_id, protected_files=protected, timeout=spec.timeout_seconds,
                        code_files={name: code_root / name for name in data["revision"]["files"]},
                        trace_execution=spec.require_edit_coverage)
                # Scored only as a NEW reproduction result, never backfilled into
                # a historical failed experiment that lacks an Observation.
                score = PACK.score(data["rows"], receipt["payload"], "trajectory_error")
                gate = None
                if spec.require_edit_coverage:
                    trace = destination / "execution-trace.json"
                    gate = assess_execution(data["revision"]["identity"]["execution_changes"], read_json(trace)) if trace.is_file() else {"passed": False, "reason": "missing_trace"}
                count = sum(case["query_count"] for case in receipt["cases"])
                row["workers"].append({"original_worker_job_id": worker["ref"]["job_id"], "replay_job_id": job_id,
                    "seed": seed, "receipt_path": str(destination / "receipt.json"), "receipt_sha256": file_hash(destination / "receipt.json"),
                    "replay_score": score, "failed_cases": sum(case["failed"] for case in PACK.diagnostics(data["rows"], receipt["payload"])),
                    "trusted_rhs_queries": count, "execution_error": receipt.get("execution_error"),
                    "execution_gate": gate, "read_isolation": receipt["read_isolation"],
                    "matches_original_observation": (math.isclose(score, entry["trajectory_error"], rel_tol=0, abs_tol=1e-12)
                        if entry.get("observation_id") else None)})
            except Exception as error:
                # Preserve the first attempt and stop this Worker: no retry or
                # fabricated score on infrastructure, isolation or protocol errors.
                row["workers"].append({"original_worker_job_id": worker["ref"]["job_id"], "seed": seed,
                    "replay_score": None, "trusted_rhs_queries": None, "status": "replay_failed",
                    "error": redact(str(error)), "error_type": type(error).__name__, "not_retried": True})
                intent["entries"].append(row)
                write_exclusive(output / "result.json", intent | {"status": "stopped_after_failure"})
                raise ProtocolError("Isolated replay stopped after failure; preserve its output for audit") from error
        if not data["workers"]:
            row.update(status="not_replayed", reason="No original Worker specification; original failure retained without guessing a config or invocation")
        intent["entries"].append(row)
        write_exclusive(output / f"entry-{index + 1:02d}.json", row)
    if source_hashes != {str(path): file_hash(path) for path in protected}:
        raise ProtocolError("Original protected files changed during isolated replay")
    result = redact(intent | {"status": "replay_attempts_completed",
        "scientific_gain_verdict": "not_established", "boundaries": [
            "Local isolated solver re-execution, not third-party independent validation.",
            "Windows candidate read-denial and trusted RHS metering; no kernel-level network block.",
            "New replay measurements do not create or repair original Observations, Claims or predictions.",
            "RHS queries are not total arithmetic or equalized model cost."]})
    result["reproduction_sha256"] = digest(result)
    write_exclusive(output / "result.json", result)
    return result


def write_exclusive(path, value):
    path = Path(path)
    if path.exists():
        raise ProtocolError("Refusing to overwrite reproduction evidence")
    with path.open("x", encoding="utf-8") as stream:
        stream.write(canonical(redact(value)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "replay"))
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ProtocolError("Reproduction output already exists; do not overwrite or auto-retry")
    if args.action == "verify":
        result = verify_retained(args.bundle)
        write_exclusive(args.output, result)
    else:
        result = replay_isolated(args.bundle, args.output)
    print(json.dumps({"scope": result["scope"], "entries": len(result["entries"]),
        "output": str(args.output.resolve()), "no_model_calls": True, "no_confirmation_consumption": True}))


if __name__ == "__main__":
    main()
