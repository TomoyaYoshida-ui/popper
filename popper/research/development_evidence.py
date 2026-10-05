"""Read-only, evaluator-owned development diagnostics, never research state.

No Worker/Controller constructor, queue recovery, model call or directory creation
is performed here. Private references are used only in the host's existing scorer
and never included in the returned projection.
"""
from __future__ import annotations

import math
import re
from contextlib import closing
from pathlib import Path

from ..core import Experiment, ProtocolError, digest, file_hash, portable_path, read_json
from ..domains.oscillator_trajectory import PACK
from ..task_context import redact
from .evaluation_service import scoring_code_hash
from .execution_jobs import RevisionExecutionJobStore, TERMINAL
from .readout import read_research_state
from .revisions import RevisionStore
from .store import ResearchStore
from .workers.base import JobSpec, TERMINAL_JOB_STATUSES


class _Sources(dict):
    def __init__(self):
        super().__init__()
        self.paths = {}

    def capture(self, path, label, expected=None):
        path = Path(path)
        sha = file_hash(path)
        if expected is not None and sha != expected:
            raise ProtocolError("development evidence source changed: " + label)
        self[label] = sha
        self.paths[str(path.resolve())] = sha
        return sha

    def verify(self):
        if any(not Path(p).is_file() or file_hash(p) != sha for p, sha in self.paths.items()):
            raise ProtocolError("development evidence source changed while reading")


def _checked(path, expected, sources, label):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or file_hash(path) != expected:
        raise ProtocolError("development evidence artifact changed: " + label)
    sources[label] = expected
    if isinstance(sources, _Sources):
        sources.capture(path, label, expected)
    value = read_json(path)
    if file_hash(path) != expected:
        raise ProtocolError("development evidence changed while reading: " + label)
    return value


def _revision(run, revision_id, sources, label):
    if not isinstance(revision_id, str) or not re.fullmatch(r"REV-[0-9a-f]{24}", revision_id):
        raise ProtocolError("development evidence Revision identity invalid")
    root = (run / "revisions").resolve()
    if not root.is_dir():
        raise ProtocolError("development evidence immutable revisions missing")
    # verify() is side-effect-free; deliberately bypass the mkdir constructor.
    reader = RevisionStore.__new__(RevisionStore)
    reader.root = root
    target = root / revision_id
    if target.is_symlink() or any(p.is_symlink() for p in target.rglob("*")):
        raise ProtocolError("development evidence Revision symlink rejected")
    revision = reader.verify(revision_id)
    sources[label + ":revision"] = file_hash(target / "revision.json")
    if isinstance(sources, _Sources):
        sources.capture(target / "revision.json", label + ":revision")
    for name, sha in revision["files"].items():
        sources[label + ":code:" + name] = sha
        if isinstance(sources, _Sources):
            sources.capture(target / "code" / name, label + ":code:" + name, sha)
    return revision


def _queue(run):
    queue = RevisionExecutionJobStore.readonly(run / "revision-execution-jobs.sqlite")
    try:
        integrity = queue.verify()
        if not integrity["ok"]:
            raise ProtocolError("development evidence queue integrity failed")
        jobs = queue.list()
        for job in jobs:
            history = queue.history(job["job_id"])
            if (not history or history[0]["event_type"] != "queued"
                    or history[0]["payload"].get("revision_id") != job["revision_id"]
                    or history[0]["payload"].get("worktree_id") != job["worktree_id"]):
                raise ProtocolError("development evidence queue enqueue identity changed")
        return jobs, digest({"jobs": jobs, "integrity": integrity})
    finally:
        queue.close()


def _worker_artifacts(run, spec, manifest, sources, label):
    directory = (run / "jobs" / spec.job_id).resolve()
    root = (run / "jobs").resolve()
    if directory.parent != root or directory.is_symlink():
        raise ProtocolError("development evidence Worker path invalid")
    receipt = _checked(directory / "receipt.json", manifest.get("receipt_sha256"),
                       sources, label + ":receipt")
    if (receipt.get("job_id") != spec.job_id
            or receipt.get("revision_id") != spec.revision_id
            or receipt.get("revision_manifest_sha256") != spec.revision_manifest_sha256
            or receipt.get("status") != manifest["status"]
            or receipt["status"] not in TERMINAL_JOB_STATUSES):
        raise ProtocolError("development evidence Worker receipt ownership mismatch")
    artifacts = {}
    for name, sha in receipt.get("artifacts", {}).items():
        if portable_path(name) != name:
            raise ProtocolError("development evidence artifact path invalid")
        base = directory if name in {"stdout.log", "stderr.log"} else directory / "workspace"
        path = (base / name).resolve()
        if not path.is_relative_to(base.resolve()) or path.is_symlink() or not path.is_file() or file_hash(path) != sha:
            raise ProtocolError("development evidence Worker artifact changed")
        sources[label + ":artifact:" + name] = sha
        if isinstance(sources, _Sources):
            sources.capture(path, label + ":artifact:" + name, sha)
        artifacts[name] = path
    return receipt, artifacts


def _side(run, state, revision_id, run_id, observation, rows, sources, label, execution_status):
    revision = _revision(run, revision_id, sources, label)
    design_id = revision["identity"]["design_id"]
    registered = next((r for r in state["runs"] if r["run_id"] == run_id), None)
    if (registered is None or registered.get("scope", registered.get("split")) not in {None, "dev"}
            or registered.get("revision_id") != revision_id
            or registered.get("design_id") != design_id
            or registered.get("revision_manifest_sha256") != sources[label + ":revision"]):
        raise ProtocolError("development evidence Run ownership/scope mismatch")
    if observation is not None and (observation["scope"] != "dev"
            or observation["run_id"] != run_id or observation["design_id"] != design_id
            or observation.get("scorer_id") != PACK.evaluator_id):
        raise ProtocolError("development evidence Observation ownership/scope mismatch")
    request_id, predictions = None, {}
    if observation is not None:
        response_path = Path(observation["artifact_id"]).resolve()
        if not response_path.is_relative_to((run / "evaluations").resolve()):
            raise ProtocolError("development evidence scoring receipt outside this research")
        response = _checked(response_path, observation["artifact_sha256"], sources, label + ":evaluation")
        request_path = response_path.parent / "request.json"
        request = read_json(request_path)
        sources[label + ":evaluation_request"] = file_hash(request_path)
        if isinstance(sources, _Sources):
            sources.capture(request_path, label + ":evaluation_request")
        if (response.get("request_sha256") != digest(request)
                or response.get("mean") != observation["value"]
                or request.get("dataset_sha256") != sources["development_input"]
                or response.get("scoring_code_sha256") != sources["scoring_service"]
                or request.get("scoring_code_sha256") != sources["scoring_service"]
                or request.get("evaluator_id") != PACK.evaluator_id):
            raise ProtocolError("development evidence scoring receipt/input mismatch")
        request_id = response.get("request_id")
        if request_id != request.get("request_id") or request_id != response_path.parent.name:
            raise ProtocolError("development evidence scoring request identity mismatch")
        predictions = {p["seed"]: p["sha256"] for p in request["predictions"]}
        if len(predictions) != len(request["predictions"]):
            raise ProtocolError("development evidence duplicate scoring seeds")
    workers = []
    for path in sorted((run / "jobs").glob("JOB-*/job.json")):
        manifest = read_json(path)
        raw_spec = manifest.get("spec", {})
        key = raw_spec.get("idempotency_key", "")
        if not (key.startswith(run_id + ":seed:") or (request_id and key.startswith(request_id + ":seed:"))):
            continue
        spec = JobSpec.from_payload(raw_spec)
        if (path.parent.name != spec.job_id or manifest["job_id"] != spec.job_id
                or spec.revision_id != revision_id or spec.design_id != design_id
                or spec.revision_manifest_sha256 != sources[label + ":revision"]
                or not spec.sandboxed or manifest["status"] not in TERMINAL_JOB_STATUSES):
            raise ProtocolError("development evidence Worker specification mismatch")
        match = re.fullmatch(re.escape(run_id) + r":seed:(\d+):execution-v\d+", spec.idempotency_key)
        if match is None and request_id:
            match = re.fullmatch(re.escape(request_id) + r":seed:(\d+):dev-v\d+", spec.idempotency_key)
        if not match:
            raise ProtocolError("development evidence seed identity invalid")
        workers.append((int(match[1]), spec, manifest, path))
    if not workers or len({w[0] for w in workers}) != len(workers):
        raise ProtocolError("development evidence Worker missing or ambiguous")
    expected_seeds = state["manifest"].get("repeat_values") or [0]
    if observation is not None and set(expected_seeds) != {w[0] for w in workers}:
        raise ProtocolError("development evidence seed coverage mismatch")
    cases, scores, elapsed = [], [], 0.0
    worker_refs = []
    for seed, spec, manifest, path in workers:
        prefix = label + ":worker:" + str(seed)
        sources[prefix + ":manifest"] = file_hash(path)
        if isinstance(sources, _Sources):
            sources.capture(path, prefix + ":manifest")
        receipt, artifacts = _worker_artifacts(run, spec, manifest, sources, prefix)
        seconds = receipt.get("elapsed_seconds")
        if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0:
            raise ProtocolError("development evidence elapsed time invalid")
        elapsed += seconds
        if receipt.get("execution_backend") != "windows_low_integrity_metered_rhs":
            raise ProtocolError("development evidence trusted RHS backend missing")
        if receipt.get("worker_binding_sha256") != sources.get("worker_binding"):
            raise ProtocolError("development evidence frozen Worker binding mismatch")
        features = [i for i in spec.inputs if i.role == "development_features_without_labels"]
        configs = [i for i in spec.inputs if i.role == "frozen_intervention"]
        if len(features) != 1 or len(configs) != 1 or len(spec.inputs) != 2:
            raise ProtocolError("development evidence input contract invalid")
        if any(not Path(i.source).resolve().is_relative_to(run) for i in spec.inputs):
            raise ProtocolError("development evidence input source outside this research")
        feature_rows = _checked(Path(features[0].source), features[0].sha256, sources, prefix + ":features")
        config = _checked(Path(configs[0].source), configs[0].sha256, sources, prefix + ":config")
        if feature_rows != PACK.project_inputs(rows):
            raise ProtocolError("development evidence feature rows are not the frozen development split")
        rhs_path = artifacts.get("outputs/rhs-receipt.json")
        if rhs_path is None:
            # A process may fail before the broker can produce a receipt. No score
            # or candidate-reported query count is invented in this case.
            if observation is not None:
                raise ProtocolError("development evidence trusted RHS receipt missing")
            worker_refs.append({"job_id": spec.job_id, "seed": seed, "status": receipt["status"],
                                "receipt_sha256": sources[prefix + ":receipt"], "trusted_rhs_available": False})
            continue
        rhs = read_json(rhs_path)
        if (rhs.get("kind") != "host_metered_rhs_execution" or rhs.get("job_id") != spec.job_id
                or rhs.get("input_sha256") != digest(feature_rows)
                or rhs.get("source_sha256") != revision["files"]
                or rhs.get("config_sha256") != digest(config)):
            raise ProtocolError("development evidence RHS ownership/input/code mismatch")
        records = rhs.get("cases")
        if not isinstance(records, list) or len(records) != len(rows):
            raise ProtocolError("development evidence RHS case coverage invalid")
        by_id = {r.get("case_id"): r for r in records}
        if len(by_id) != len(rows) or set(by_id) != {r["id"] for r in rows}:
            raise ProtocolError("development evidence RHS case identities invalid")
        payload_path = artifacts.get("outputs/trajectories-" + str(seed) + ".json")
        payload = read_json(payload_path) if payload_path is not None else None
        if observation is not None and sources.get(prefix + ":artifact:outputs/trajectories-" + str(seed) + ".json") != predictions.get(seed):
            raise ProtocolError("development evidence scoring prediction binding mismatch")
        if payload is not None and payload != rhs.get("payload"):
            raise ProtocolError("development evidence trajectory/RHS payload mismatch")
        diagnostics = None
        if observation is not None:
            if payload is None or receipt["status"] != "succeeded":
                raise ProtocolError("development evidence Observation lacks successful scored trajectories")
            diagnostics = {r["id"]: r for r in PACK.diagnostics(rows, payload)}
            scores.append(PACK.score(rows, payload, "trajectory_error"))
        for row in rows:
            item = by_id[row["id"]]
            count = item.get("query_count")
            if (item.get("budget") != row["budget"] or type(count) is not int or not 0 <= count <= row["budget"]
                    or item.get("status") not in {"succeeded", "failed", "not_completed"}):
                raise ProtocolError("development evidence trusted RHS count/status invalid")
            diagnostic = diagnostics[row["id"]] if diagnostics is not None else {
                "id": row["id"], "system": row["system"], "failed": None,
                "trajectory_error": None, "energy_drift": None}
            if diagnostics is not None and diagnostic["failed"] != (item["status"] != "succeeded"):
                raise ProtocolError("development evidence trajectory validity disagrees with host receipt")
            cases.append({**diagnostic, "seed": seed, "execution_status": item["status"],
                          "query_count": count, "query_budget": row["budget"]})
        worker_refs.append({"job_id": spec.job_id, "seed": seed, "status": receipt["status"],
                            "receipt_sha256": sources[prefix + ":receipt"], "trusted_rhs_available": True})
    measured = math.fsum(scores) / len(scores) if scores else None
    if observation is not None and (measured is None or not math.isclose(measured, observation["value"], rel_tol=0, abs_tol=1e-12)):
        raise ProtocolError("development evidence rescore differs from authoritative Observation")
    systems = {}
    for system in sorted({r["system"] for r in cases}):
        subset = [r for r in cases if r["system"] == system]
        scored = [r for r in subset if r["trajectory_error"] is not None]
        energies = [r["energy_drift"] for r in scored if r["energy_drift"] is not None]
        systems[system] = {"case_count": len(subset), "failed_cases": sum(r["failed"] is True for r in scored),
            "mean_trajectory_error": math.fsum(r["trajectory_error"] for r in scored) / len(scored) if scored else None,
            "mean_sampled_energy_drift_successful": math.fsum(energies) / len(energies) if energies else None,
            "trusted_rhs_queries": sum(r["query_count"] for r in subset)}
    rhs_complete = all(w["trusted_rhs_available"] for w in worker_refs)
    known_queries = sum(r["query_count"] for r in cases)
    return {"revision_id": revision_id, "run_id": run_id, "design_id": design_id,
            "observation_id": observation["observation_id"] if observation is not None else None,
            "trajectory_error": measured, "execution_status": execution_status,
            "execution_cases": sum(r["execution_status"] == "succeeded" for r in cases),
            "trajectory_valid_cases": sum(r["failed"] is False for r in cases),
            "scored_cases": sum(r["trajectory_error"] is not None for r in cases),
            "total_cases": len(cases), "systems": systems, "cases": cases,
            "trusted_rhs_queries": known_queries if rhs_complete else None,
            "trusted_rhs_queries_known_partial": known_queries,
            "trusted_rhs_complete": rhs_complete,
            "elapsed_seconds": elapsed, "workers": worker_refs}


def build_development_evidence(project, run, job_id):
    """Return a verified, reference-free projection for one explicit dev Job."""
    project, run = Path(project).resolve(), Path(run).resolve()
    sources = _Sources()
    manifest = read_json(run / "research.json")
    if Path(manifest.get("project", "")).resolve() != project or manifest.get("scorer_id") != PACK.evaluator_id:
        raise ProtocolError("development evidence project/evaluator mismatch")
    state = read_research_state(run)
    if not state.get("ok") or not state["experiment_graph"]["integrity"]["ok"]:
        raise ProtocolError("development evidence graph integrity failed")
    store = ResearchStore(run / "research.sqlite", readonly=True)
    try:
        if not store.verify()["ok"]:
            raise ProtocolError("development evidence research event integrity failed")
    finally:
        store.close()
    sources.capture(run / "research.json", "research_manifest")
    binding_path = run / "oscillator-worker-binding.json"
    binding = read_json(binding_path)
    sources.capture(binding_path, "worker_binding")
    if binding.get("kind") != "windows_metered_rhs_v1" or binding.get("research_sha256") != sources["research_manifest"]:
        raise ProtocolError("development evidence metered Worker binding invalid")
    for index, (path, sha) in enumerate(sorted(binding.get("protected_files", {}).items())):
        sources.capture(path, "protected_source:" + str(index), sha)
    if manifest.get("scoring_code_sha256") != scoring_code_hash():
        raise ProtocolError("development evidence frozen scoring implementation changed")
    sources["scoring_service"] = scoring_code_hash()
    with closing(Experiment(project, readonly=True)) as exp:
        core = exp.verify_inputs()
    sources["development_input"] = core["input_hashes"]["dev.json"]
    rows = _checked(project / "dev.json", sources["development_input"], sources, "development_input")
    PACK.validate_rows(rows, "dev")
    jobs, queue_sha = _queue(run)
    sources["execution_queue"] = queue_sha
    job = next((j for j in jobs if j["job_id"] == job_id), None)
    if job is None or job["status"] not in TERMINAL:
        raise ProtocolError("development evidence requires this Study's terminal Job")
    revision = _revision(run, job["revision_id"], sources, "candidate")
    control_h = manifest["control_hypothesis_id"]
    if revision["identity"]["hypothesis_id"] == control_h:
        raise ProtocolError("development evidence candidate cannot be the fixed control")
    run_id = job.get("run_id") or "RUN-" + digest({"revision_id": job["revision_id"], "split": "dev"})[:24]
    observations = [o for o in state["observations"] if o["run_id"] == run_id and o["scope"] == "dev"]
    if len(observations) > 1 or (job["status"] == "succeeded" and len(observations) != 1):
        raise ProtocolError("development evidence candidate Observation missing or ambiguous")
    controls = [o for o in state["observations"] if o["hypothesis_id"] == control_h and o["scope"] == "dev"]
    if len(controls) != 1:
        raise ProtocolError("development evidence fixed RK4 Observation missing or ambiguous")
    control_obs = controls[0]
    control_run = next((r for r in state["runs"] if r["run_id"] == control_obs["run_id"]), None)
    if control_run is None:
        raise ProtocolError("development evidence control Run missing")
    candidate = _side(run, state, job["revision_id"], run_id, observations[0] if observations else None,
                      rows, sources, "candidate", job["status"])
    control = _side(run, state, control_run["revision_id"], control_obs["run_id"], control_obs,
                    rows, sources, "control", "succeeded")
    threshold = manifest["min_meaningful_effect"]
    improvement = control["trajectory_error"] - candidate["trajectory_error"] if candidate["trajectory_error"] is not None else None
    policy_path = project / "evaluation-policy.json"
    candidate_budget = None
    if policy_path.is_file():
        if "evaluation-policy.json" not in core["spec"].get("provenance_files", []):
            raise ProtocolError("development evidence candidate limit must be preregistered")
        limit = read_json(policy_path).get("candidate_development_limit")
        if type(limit) is not int or not 1 <= limit <= 6:
            raise ProtocolError("development evidence candidate limit invalid")
        used = len({j["revision_id"] for j in jobs if _revision(run, j["revision_id"], {}, "quota")["identity"]["hypothesis_id"] != control_h})
        candidate_budget = {"scope": "candidate_development", "limit": limit, "used": used,
                            "remaining": max(0, limit - used),
                            "unit": "distinct_non_control_revisions_with_execution_jobs"}
    result = {"schema_version": "1.0", "scope": "dev", "kind": "development_evidence",
        "study_id": manifest["study_id"], "state_version": state["state_version"],
        "job_id": job_id, "revision_id": job["revision_id"], "run_id": run_id,
        "metric": manifest["metric"], "source_digests": sources,
        "candidate": candidate, "control": control,
        "current_budget": {"kernel": state["budget"], "candidate_development": candidate_budget},
        "comparison": {"absolute_improvement": improvement,
            "relative_improvement": improvement / control["trajectory_error"] if improvement is not None and control["trajectory_error"] > 0 else None,
            "threshold": threshold, "development_threshold_met": improvement is not None and improvement >= threshold},
        "sampling": {"energy": "maximum relative drift over frozen output samples, not all solver steps",
                     "sample_counts": sorted({len(r["times"]) for r in rows})},
        "budget_scope": "trusted RHS queries, not total arithmetic",
        "authority": "derived development evidence; not a Claim, execution approval or theory proof"}
    # Read again after all artifacts: stale results cannot become current facts.
    latest = read_research_state(run)
    sources.verify()
    if latest.get("state_version") != state["state_version"] or _queue(run)[1] != queue_sha:
        raise ProtocolError("development evidence changed while reading; re-read required")
    safe = redact(result)
    return {**safe, "evidence_sha256": digest(safe)}
