"""Post-hoc blind-cell audit with score recomputation and conservative conclusions."""
from __future__ import annotations

import json
import math
import statistics
from copy import deepcopy
from pathlib import Path

from popper.core import EVALUATORS, ProtocolError, dataset, digest, file_hash, read_json, score
from popper.research.evaluation_service import _analysis_slices, _slice_rows, _slice_score
from popper.research.store import ResearchStore


def _close(a, b):
    return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)


def _recompute_evaluation(response_path, request, response):
    required = {"schema_version", "request_id", "dataset", "dataset_sha256",
                "evaluator_id", "evaluator_hash", "predictions", "expected_seeds",
                "scoring_code_sha256"}
    if not isinstance(request, dict) or request.get("schema_version") not in {"2.0", "3.0"}:
        raise ProtocolError("archived evaluation request schema mismatch")
    if request["schema_version"] == "3.0":
        required.add("analysis_slices")
    if set(request) != required:
        raise ProtocolError("archived evaluation request schema mismatch")
    evaluator_id = request["evaluator_id"]
    if evaluator_id not in EVALUATORS or request["evaluator_hash"] != digest(EVALUATORS[evaluator_id]):
        raise ProtocolError("archived evaluator identity mismatch")
    data_path = Path(request["dataset"])
    if not data_path.is_file() or file_hash(data_path) != request["dataset_sha256"]:
        raise ProtocolError("archived evaluation dataset changed")
    if response.get("request_sha256") != digest(request):
        raise ProtocolError("archived response is not bound to its request")
    for name in ("request_id", "evaluator_id", "evaluator_hash", "scoring_code_sha256"):
        if response.get(name) != request[name]:
            raise ProtocolError(f"archived response identity mismatch: {name}")
    expected_seeds = request["expected_seeds"]
    by_seed = {}
    for item in request["predictions"]:
        if not isinstance(item, dict) or set(item) != {"seed", "path", "sha256"}:
            raise ProtocolError("archived prediction manifest malformed")
        if item["seed"] in by_seed:
            raise ProtocolError("archived prediction seed duplicated")
        prediction_path = Path(item["path"])
        if not prediction_path.is_file() or file_hash(prediction_path) != item["sha256"]:
            raise ProtocolError("archived prediction artifact changed")
        by_seed[item["seed"]] = prediction_path
    if set(by_seed) != set(expected_seeds):
        raise ProtocolError("archived prediction seeds incomplete")
    rows = dataset(data_path, evaluator_id)
    prediction_rows = {seed: read_json(by_seed[seed]) for seed in expected_seeds}
    points = [{"seed": seed, "value": score(rows, prediction_rows[seed], evaluator_id)}
              for seed in expected_seeds]
    archived_points = response.get("per_seed")
    if not isinstance(archived_points, list) or len(archived_points) != len(points):
        raise ProtocolError("archived per-seed response incomplete")
    for actual, archived in zip(points, archived_points):
        if actual["seed"] != archived.get("seed") or not _close(actual["value"], archived.get("value")):
            raise ProtocolError("independent per-seed score differs from archived response")
    values = [point["value"] for point in points]
    mean = statistics.mean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    if not _close(mean, response.get("mean")) or not _close(std, response.get("std")):
        raise ProtocolError("independent summary score differs from archived response")
    recomputed_slices = []
    if request["schema_version"] == "3.0":
        registered = _analysis_slices(request["analysis_slices"], evaluator_id)
        archived_slices = response.get("slices")
        if not isinstance(archived_slices, list) or len(archived_slices) != len(registered):
            raise ProtocolError("archived slice response is incomplete")
        for index, item in enumerate(registered):
            selected = _slice_rows(rows, item["rule"])
            slice_points = [{"seed": seed,
                             "value": _slice_score(selected, prediction_rows[seed], evaluator_id)}
                            for seed in expected_seeds]
            archived = archived_slices[index]
            if archived.get("slice_id") != item["slice_id"] or archived.get("n") != len(selected):
                raise ProtocolError("archived slice identity or size differs")
            if len(archived.get("per_seed", [])) != len(slice_points):
                raise ProtocolError("archived slice seed response is incomplete")
            for actual, saved in zip(slice_points, archived["per_seed"]):
                if (actual["seed"] != saved.get("seed")
                        or not _close(actual["value"], saved.get("value"))):
                    raise ProtocolError("independent slice score differs from archived response")
            slice_values = [point["value"] for point in slice_points]
            slice_mean = statistics.mean(slice_values)
            slice_std = statistics.stdev(slice_values) if len(slice_values) > 1 else 0.0
            if (not _close(slice_mean, archived.get("mean"))
                    or not _close(slice_std, archived.get("std"))):
                raise ProtocolError("independent slice summary differs from archived response")
            recomputed_slices.append({"slice_id": item["slice_id"], "n": len(selected),
                                      "mean": slice_mean, "std": slice_std})
    return {"artifact": str(response_path), "request_id": request["request_id"],
            "seeds": expected_seeds, "mean": mean, "std": std,
            "slices": recomputed_slices,
            "archived_scoring_code_sha256": request["scoring_code_sha256"]}


def _infer_conclusion(manifest, observations, decisions, hypothesis_statuses, condition, phase):
    candidate_ids = manifest["candidate_hypothesis_ids"]
    control_id = manifest["control_hypothesis_id"]
    threshold = float(manifest["min_meaningful_effect"])
    direction = manifest["metric"]["direction"]
    by_scope = {}
    for observation in observations:
        by_scope.setdefault(observation["scope"], {})[observation["hypothesis_id"]] = float(
            observation["value"])

    def effects(scope):
        values = by_scope.get(scope, {})
        if control_id not in values:
            return None
        result = {}
        for hypothesis_id in candidate_ids:
            if hypothesis_id in values:
                raw = values[hypothesis_id] - values[control_id]
                result[hypothesis_id] = raw if direction == "max" else -raw
        return result

    dev_effects = effects("dev")
    confirmation_effects = effects("confirmation")
    slice_effects = {scope: effects(scope) for scope in sorted(by_scope)
                     if scope.startswith("dev:slice:")}
    confirmation_slice_effects = {scope: effects(scope) for scope in sorted(by_scope)
                                  if scope.startswith("confirmation:slice:")}
    inferred = "inconclusive"
    basis = "insufficient registered candidate evidence"
    observation_by_id = {row["observation_id"]: row for row in observations}

    def confirms_positive(candidate_id):
        required = {("confirmation", control_id), ("confirmation", candidate_id)}
        for decision in decisions:
            if decision.get("action") != "stop":
                continue
            covered = {(observation_by_id[ref]["scope"],
                        observation_by_id[ref]["hypothesis_id"])
                       for ref in decision.get("observation_refs", [])
                       if ref in observation_by_id}
            if not required <= covered:
                continue
            if decision.get("actor") == "confirmation_controller":
                return True
            if decision.get("actor") == "external_confirmation_controller":
                try:
                    rationale = json.loads(decision.get("rationale", ""))
                except (TypeError, ValueError):
                    continue
                if (rationale.get("confirmation_kind", "positive_effect") == "positive_effect"
                        and rationale.get("status") == "succeeded"
                        and rationale.get("passed") is True):
                    return True
        return False

    confirmed_positive = [candidate_id for candidate_id, value in
                          (confirmation_effects or {}).items()
                          if value >= threshold and confirms_positive(candidate_id)]
    boundary_basis = None
    if confirmed_positive:
        inferred, basis = "positive_effect", "independent confirmation met the frozen threshold"
    elif phase in {"concluded", "budget_exhausted"} and dev_effects is not None:
        boundary_candidates = []
        for candidate_id, global_effect in (confirmation_effects or {}).items():
            candidate_slices = {scope: values[candidate_id]
                                for scope, values in confirmation_slice_effects.items()
                                if values is not None and candidate_id in values}
            if (abs(global_effect) < threshold and len(candidate_slices) >= 2
                    and max(candidate_slices.values()) >= threshold
                    and min(candidate_slices.values()) < threshold):
                boundary_candidates.append((candidate_id, candidate_slices))
        # Explicit boundary declared by the research strategy against the evidence.
        boundary_decision = None
        for decision in decisions:
            if decision.get("action") != "conclude_scope_boundary":
                continue
            refs = set(decision.get("observation_refs", []))
            referenced = [observation_by_id[ref] for ref in refs if ref in observation_by_id]
            for candidate_id, candidate_slices in boundary_candidates:
                required_scopes = {"confirmation", *candidate_slices}
                covered = {(row["scope"], row["hypothesis_id"]) for row in referenced}
                required = {(scope, hypothesis_id) for scope in required_scopes
                            for hypothesis_id in (control_id, candidate_id)}
                if required <= covered:
                    boundary_decision = {"decision_id": decision.get("decision_id"),
                                         "candidate_id": candidate_id,
                                         "slice_effects": candidate_slices}
                    break
            if boundary_decision:
                break
        if boundary_decision:
            inferred = "scope_boundary_or_counterexample"
            basis = "explicit boundary conclusion cites signed holdout global and preregistered slice evidence"
            boundary_basis = {"level": "explicit",
                              "candidate_id": boundary_decision["candidate_id"],
                              "slice_effects": boundary_decision["slice_effects"]}
        elif boundary_candidates:
            # Root-cause decoupling: pre-registered slice rules plus a signed holdout
            # already establish that the evidence itself is boundary-shaped, so an
            # evidence-blind fixed-plan strategy is not structurally barred from an
            # evidence-supported boundary. Kept as a distinct "derived" level so it
            # is never reported as an explicit declaration.
            candidate_id, candidate_slices = boundary_candidates[0]
            inferred = "scope_boundary_or_counterexample"
            basis = ("signed holdout global below threshold while preregistered "
                     "subgroup slices straddle the threshold (evidence-derived; "
                     "strategy did not explicitly declare a boundary)")
            boundary_basis = {"level": "derived", "candidate_id": candidate_id,
                              "slice_effects": candidate_slices}
        all_tested = set(dev_effects) == set(candidate_ids)
        no_supported = max(dev_effects.values(), default=-math.inf) < threshold
        if inferred == "inconclusive" and all_tested and no_supported:
            if all(abs(value) < threshold for value in dev_effects.values()):
                inferred, basis = "near_zero_effect", "all registered global effects were within threshold"
            elif any(value <= -threshold for value in dev_effects.values()):
                inferred, basis = "negative_effect", "no candidate was supported and at least one contradicted the direction"
    matched = inferred == condition
    return {"expected": condition, "inferred": inferred, "matched": matched,
            "basis": basis, "dev_effects": dev_effects,
             "slice_effects": slice_effects,
             "confirmation_slice_effects": confirmation_slice_effects,
             "confirmation_effects": confirmation_effects,
             "boundary_basis": boundary_basis,
            "hypothesis_statuses": hypothesis_statuses}


def audit_cell(cell_dir, gold_dir):
    """Verify one archived cell without executing candidate code or calling a model."""
    cell_dir, gold_dir = Path(cell_dir), Path(gold_dir)
    errors = []
    recomputed = []
    observations = []
    decisions = []
    statuses = {}
    try:
        gold = read_json(gold_dir / "task-manifest.json")
        project = cell_dir / "project"
        for name, expected in gold["public_file_hashes"].items():
            path = project / name
            if not path.is_file() or file_hash(path) != expected:
                raise ProtocolError(f"frozen task input changed: {name}")
        manifest = read_json(cell_dir / "research" / "research.json")
        if manifest["input_identity"]["input_hashes"]["experiment.json"] != gold[
                "public_file_hashes"]["experiment.json"]:
            raise ProtocolError("research manifest is not bound to the blind task")
        store = ResearchStore(cell_dir / "research" / "research.sqlite")
        try:
            integrity = store.verify()
            if not integrity["ok"]:
                raise ProtocolError(integrity["reason"])
            observation_rows = store.list("observation")
            observations = observation_rows
            decisions = store.list("decision")
            statuses = {row["hypothesis_id"]: row["status"] for row in store.list("hypothesis")}
        finally:
            store.close()
        seen = set()
        for observation in observations:
            artifact = Path(observation["artifact_id"])
            if artifact in seen:
                continue
            seen.add(artifact)
            # Signed external receipts are already hash/selector checked by ResearchStore.
            # Development and local-confirmation evaluator responses have a sibling request.
            request_path = artifact.with_name("request.json")
            if request_path.is_file():
                recomputed.append(_recompute_evaluation(
                    artifact, read_json(request_path), read_json(artifact)))
            elif observation["scope"] == "confirmation" or observation["scope"].startswith(
                    "confirmation:slice:"):
                enrollment = manifest.get("external_confirmation")
                bundle = cell_dir / "research" / "external-confirmation" / "bundle"
                if not enrollment or not bundle.is_dir():
                    raise ProtocolError("confirmation observation lacks replayable signed enrollment")
                from popper.research.confirmation_bundle import verify_bundle
                from popper.research.confirmation_contracts import validate_result
                submission, contract = verify_bundle(bundle, enrollment["pinned_public_key"])
                validate_result(read_json(artifact), enrollment["contract"], submission,
                                enrollment["pinned_public_key"])
                if contract != enrollment["contract"]["payload"]:
                    raise ProtocolError("confirmation contract differs from enrolled contract")
                recomputed.append({"artifact": str(artifact), "kind": "signed_confirmation",
                                   "contract_id": contract["contract_id"]})
            else:
                raise ProtocolError("non-confirmation observation lacks a replayable request")
        conclusion = _infer_conclusion(manifest, observations, decisions, statuses, gold["condition"],
                                       read_json(cell_dir / "cell-summary.json").get("phase"))
    except Exception as error:
        errors.append(f"{type(error).__name__}: {error}")
        conclusion = {"expected": None, "inferred": "unverifiable", "matched": False,
                      "basis": "evidence replay failed", "dev_effects": None,
                      "slice_effects": None,
                      "confirmation_slice_effects": None,
                      "confirmation_effects": None, "boundary_basis": None,
                      "hypothesis_statuses": statuses}
    return {"schema_version": "1.0", "ok": not errors, "errors": errors,
            "observations": len(observations), "evaluation_artifacts_recomputed": len(recomputed),
            "recomputed": recomputed, "conclusion": conclusion}


def audited_summary(cell_dir, gold_dir, summary=None):
    summary = deepcopy(summary if summary is not None else read_json(Path(cell_dir) / "cell-summary.json"))
    replay = audit_cell(cell_dir, gold_dir)
    summary["evidence_replay"] = replay
    summary["conclusion_matched"] = replay["conclusion"]["matched"]
    summary["conclusion_assessment"] = replay["conclusion"]
    return summary
