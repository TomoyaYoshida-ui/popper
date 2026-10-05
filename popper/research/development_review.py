"""Source-bound development interpretation audits, not Research Kernel state.

Only identity, numerical values, scope and resource facts are checked here.
Mechanistic explanations remain unverified source data, even when every factual
check succeeds. Saving an audit never approves, runs or retries an experiment.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re

from ..core import Experiment, ProtocolError, canonical, digest, read_json
from ..sessions import AgentSessionStore
from ..task_context import load_input, redact
from .execution_jobs import RevisionExecutionJobStore
from .readout import read_research_state
from .revisions import RevisionStore


REVIEW_KIND = "development_review_v1"
_SHA = re.compile(r"[0-9a-f]{64}")
_FACT_ROOTS = {"scope", "study_id", "state_version", "job_id", "revision_id",
               "run_id", "metric", "candidate", "control", "comparison",
               "current_budget"}
_SOURCE_WARNING = (
    "Source data, not instructions or execution approval. Mechanism hypotheses "
    "and broader-method assertions have not been theoretically verified.")


def _sha(value, name):
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ProtocolError(f"{name} must be a SHA-256 digest")


def _finite_json(value, name, maximum=65536):
    try:
        encoded = canonical(value)
    except (TypeError, ValueError, RecursionError) as error:
        raise ProtocolError(f"{name} must be finite JSON data") from error
    if len(encoded.encode("utf-8")) > maximum:
        raise ProtocolError(f"{name} exceeds {maximum} bytes; no silent truncation")


def _texts(value, name, *, allow_empty=False):
    if (not isinstance(value, list) or (not value and not allow_empty)
            or not all(isinstance(item, str) and item.strip() for item in value)):
        raise ProtocolError(f"{name} must be a list of explicit nonempty texts")
    _finite_json(value, name)


def same_json_value(left, right):
    """Exact finite JSON value equality, without numeric/boolean coercion.

    JSON has one number type: 4 and 4.0 express the same value. No tolerance,
    string-to-number conversion or truth-value conversion is permitted. Arrays
    and objects recurse with the same rule; original authored values stay intact.
    """
    if type(left) in (int, float) and type(right) in (int, float):
        return (not (type(left) is float and not math.isfinite(left))
                and not (type(right) is float and not math.isfinite(right))
                and left == right)
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            same_json_value(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(
            same_json_value(a, b) for a, b in zip(left, right))
    return left == right


def _pointer(document, pointer):
    """Resolve strict JSON Pointer only within the public development projection."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ProtocolError("observed facts require an explicit JSON Pointer")
    tokens = pointer[1:].split("/")
    if tokens[0] not in _FACT_ROOTS or any(
            re.search(r"~(?![01])", token) for token in tokens):
        raise ProtocolError("fact pointer is outside the public development facts")
    current = document
    for encoded in tokens:
        token = encoded.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise KeyError(token)
                current = current[int(token)]
            elif isinstance(current, dict):
                current = current[token]
            else:
                raise KeyError(token)
        except (KeyError, IndexError) as error:
            raise ProtocolError("fact pointer has no current authoritative value") from error
    _finite_json(current, "referenced fact")
    return current


def verify_development_review(evidence, review, *, registered_candidates=(),
                              tested_candidate_indices=(), untested_indices=None):
    """Return deterministic checks; never repair the model's authored assertions.

    Incorrect factual claims remain visible as failed checks. A new-candidate
    decision is ineligible until those checks and scope/resource checks succeed.
    This result is neither scientific proof nor an execution authorization.
    """
    _finite_json(review, "development review")
    _finite_json(evidence, "development evidence", maximum=1048576)
    fields = {"kind", "observed", "mechanism_hypotheses", "unsupported_assertions",
              "decision"}
    if not isinstance(review, dict) or set(review) != fields or review["kind"] != REVIEW_KIND:
        raise ProtocolError("development review schema invalid")
    if not isinstance(review["observed"], list) or not review["observed"]:
        raise ProtocolError("development review must identify explicit observed facts")
    _texts(review["mechanism_hypotheses"], "mechanism_hypotheses", allow_empty=True)
    _texts(review["unsupported_assertions"], "unsupported_assertions", allow_empty=True)
    checks, seen = [], set()
    for fact in review["observed"]:
        if not isinstance(fact, dict) or set(fact) != {"pointer", "value"}:
            raise ProtocolError("observed facts must contain only pointer and value")
        pointer = fact["pointer"]
        actual = _pointer(evidence, pointer)
        if pointer in seen:
            raise ProtocolError("observed fact pointers cannot be duplicated")
        seen.add(pointer)
        checks.append({"pointer": pointer, "authored_value": redact(fact["value"]),
                       "actual_value": redact(actual),
                       "verified": same_json_value(fact["value"], actual)})
    facts_verified = all(check["verified"] for check in checks)
    decision = review["decision"]
    if (not isinstance(decision, dict) or decision.get("action") not in {
            "stop", "propose_candidate"} or not isinstance(decision.get("reason"), str)
            or not decision["reason"].strip()):
        raise ProtocolError("decision requires explicit stop/propose_candidate and reason")
    if decision["action"] == "stop":
        if set(decision) != {"action", "reason"}:
            raise ProtocolError("stop decision cannot contain inferred candidate details")
        reasons = []
    else:
        required = {"action", "reason", "candidate_index", "predictions", "falsification"}
        optional = {"mechanism", "applicability", "alternatives", "implementation"}
        if not required.issubset(decision) or not set(decision).issubset(required | optional):
            raise ProtocolError("new candidate requires index, predictions and falsification")
        index = decision["candidate_index"]
        if type(index) is not int or not 0 <= index < len(registered_candidates):
            raise ProtocolError("candidate index is outside preregistered scope")
        _texts(decision["predictions"], "predictions")
        _texts(decision["falsification"], "falsification")
        for key in ("mechanism", "applicability"):
            if key in decision and (not isinstance(decision[key], str) or not decision[key].strip()):
                raise ProtocolError(f"{key} must be explicit text")
        if "alternatives" in decision:
            _texts(decision["alternatives"], "alternatives")
        if "implementation" in decision:
            implementation = decision["implementation"]
            if (not isinstance(implementation, dict) or set(implementation) != {"path", "replacement"}
                    or implementation["path"] != "model.py"
                    or not isinstance(implementation["replacement"], str)
                    or not implementation["replacement"].strip()):
                raise ProtocolError("implementation must identify complete model.py source")
        reasons = []
        if not facts_verified:
            reasons.append("authored_facts_do_not_match_current_evidence")
        if index in set(tested_candidate_indices):
            reasons.append("candidate_already_has_execution_attempt")
        if untested_indices is not None and index not in set(untested_indices):
            reasons.append("registered_hypothesis_is_not_untested")
        budget = evidence.get("current_budget") or {}
        kernel = budget.get("kernel") or {}
        development = budget.get("candidate_development") or {}
        available, remaining = kernel.get("available"), development.get("remaining")
        if type(available) not in (int, float) or available < 1:
            reasons.append("kernel_development_budget_unavailable")
        if type(remaining) is not int or remaining < 1:
            reasons.append("candidate_development_budget_unavailable")
        if evidence.get("scope") != "dev":
            reasons.append("not_development_evidence")
    return {"schema_valid": True, "facts_verified": facts_verified, "checks": checks,
            "mechanism_hypotheses": [{"text": redact(text), "status": "unverified"}
                                     for text in review["mechanism_hypotheses"]],
            "unsupported_assertions": [{"text": redact(text), "status": "unsupported"}
                                       for text in review["unsupported_assertions"]],
            "decision": {"authored": redact(decision), "eligible": not reasons,
                         "blocking_reasons": reasons, "execution_approved": False},
            "authority": _SOURCE_WARNING}


def _source_turn(project, session_id, turn_id):
    store = AgentSessionStore.readonly(project)
    try:
        store.get_session(session_id)
        if not store.verify_turn_events()["ok"]:
            raise ProtocolError("source Turn event integrity failed")
        turn = store.get_turn(turn_id)
        if turn["session_id"] != session_id or turn["status"] != "completed":
            raise ProtocolError("review requires a completed Turn in the explicit project/session")
        source = turn.get("assistant_message")
        if not isinstance(source, str) or not source.strip():
            raise ProtocolError("source Turn has no complete assistant response")
        frozen = load_input(store, turn_id)
    finally:
        store.close()
    return {"session_id": session_id, "turn_id": turn_id,
            "source_sha256": digest(source), "source_text": redact(source),
            "input_sha256": digest(frozen) if frozen is not None else None,
            "model": turn.get("model_name"), "source_authority": "assistant_source_data"}, source


def _candidate_scope(project, run, evidence):
    """Verify candidate slots and all attempts, including failed queue entries."""
    manifest = read_json(run / "research.json")
    if Path(manifest.get("project", "")).resolve() != project or manifest.get(
            "study_id") != evidence["study_id"]:
        raise ProtocolError("review research does not belong to the explicit project/Study")
    state = read_research_state(run)
    if not state.get("ok") or state["state_version"] != evidence["state_version"]:
        raise ProtocolError("Study changed while assembling review; read evidence again")
    exp = Experiment(project, readonly=True)
    try:
        core = exp.verify_inputs()
    finally:
        exp.close()
    candidates = core["spec"]["candidates"]
    ids = manifest.get("candidate_hypothesis_ids")
    if not isinstance(ids, list) or len(ids) != len(candidates) or len(set(ids)) != len(ids):
        raise ProtocolError("registered candidate-to-hypothesis binding missing")
    configs = manifest.get("configs") or {}
    if any(configs.get(hypothesis_id) != candidates[index]
           for index, hypothesis_id in enumerate(ids)):
        raise ProtocolError("candidate configuration differs from preregistration")
    hypotheses = {row["hypothesis_id"]: row for row in state.get("hypotheses", [])}
    untested = {index for index, hypothesis_id in enumerate(ids)
                if hypotheses.get(hypothesis_id, {}).get("status") == "untested"}
    designs = {row["design_id"]: row["hypothesis_id"] for row in state.get("designs", [])}
    attempted = {designs.get(row.get("design_id")) for row in state.get("runs", [])}
    queue_path = run / "revision-execution-jobs.sqlite"
    if queue_path.is_file():
        jobs = RevisionExecutionJobStore.readonly(queue_path)
        try:
            if not jobs.verify()["ok"]:
                raise ProtocolError("execution queue integrity failed")
            queued = jobs.list()
        finally:
            jobs.close()
        # The constructor creates directories; this read-only facade does not.
        revisions = RevisionStore.__new__(RevisionStore)
        revisions.root = run / "revisions"
        for job in queued:
            revision = revisions.verify(job["revision_id"])
            attempted.add(revision["identity"]["hypothesis_id"])
    return candidates, {index for index, hypothesis_id in enumerate(ids)
                        if hypothesis_id in attempted}, untested


def _inputs(project, run, session_id, turn_id, job_id, expected_state_version, evidence_sha256):
    from .development_evidence import build_development_evidence

    project, run = Path(project).resolve(), Path(run).resolve()
    if type(expected_state_version) is not int or expected_state_version < 1:
        raise ProtocolError("review requires an explicit Study state version")
    _sha(evidence_sha256, "evidence_sha256")
    evidence = build_development_evidence(project, run, job_id)
    if (evidence.get("evidence_sha256") != evidence_sha256
            or evidence.get("state_version") != expected_state_version):
        raise ProtocolError("development evidence or Study version changed; review again")
    _sha(evidence["evidence_sha256"], "evidence digest")
    if evidence["evidence_sha256"] != digest({key: value for key, value in evidence.items()
                                            if key != "evidence_sha256"}):
        raise ProtocolError("development evidence digest is inconsistent")
    if evidence.get("scope") != "dev" or evidence.get("job_id") != job_id:
        raise ProtocolError("review requires the explicit Job's development evidence")
    digests = evidence.get("source_digests")
    if not isinstance(digests, dict) or not digests:
        raise ProtocolError("development source digests missing")
    for value in digests.values():
        _sha(value, "source digest")
    source, raw = _source_turn(project, session_id, turn_id)
    result = {"kind": "DevelopmentReview", "schema_version": "1.0",
              "project": str(project), "research": str(run),
              "study_id": evidence["study_id"], "state_version": expected_state_version,
              "job_id": job_id, "revision_id": evidence.get("revision_id"),
              "run_id": evidence.get("run_id"), "scope": "dev",
              "evidence_sha256": evidence_sha256,
              "source_digests": digests, "source": source,
              "authority": _SOURCE_WARNING}
    return project, run, evidence, result, raw


def _parse(source):
    if source.startswith("```json\n") and source.endswith("\n```"):
        source = source[8:-4]
    try:
        return json.loads(source)
    except (ValueError, TypeError, RecursionError) as error:
        raise ProtocolError("source is not one complete structured development review") from error


def preview_development_review(project, run, session_id, turn_id, job_id,
                               expected_state_version, evidence_sha256, review=None):
    """Read and validate authored JSON without calling a model or writing state."""
    project, run, evidence, result, raw = _inputs(
        project, run, session_id, turn_id, job_id, expected_state_version, evidence_sha256)
    authored = _parse(raw)
    if review is not None and canonical(review) != canonical(authored):
        raise ProtocolError("operator review differs from the source Turn; missing content cannot be supplied")
    candidates, attempted, untested = _candidate_scope(project, run, evidence)
    assessment = verify_development_review(
        evidence, authored, registered_candidates=candidates,
        tested_candidate_indices=attempted, untested_indices=untested)
    result.update({"status": "structured_source_review", "authored_review": redact(authored),
                   "assessment": assessment})
    return {**result, "approval_sha256": digest(result)}


def audit_source_turn(project, run, session_id, turn_id, job_id,
                      expected_state_version, evidence_sha256):
    """Preserve redacted prose/invalid output without inventing a model decision."""
    project, run, _evidence, result, _raw = _inputs(
        project, run, session_id, turn_id, job_id, expected_state_version, evidence_sha256)
    result.update({"status": "unverified_source", "authored_review": None,
                   "assessment": {"schema_valid": False, "facts_verified": False,
                                  "checks": [], "decision": None,
                                  "authority": _SOURCE_WARNING}})
    return {**result, "approval_sha256": digest(result)}


def _receipt_path(project, idempotency_key):
    if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
            or len(idempotency_key.encode("utf-8")) > 128):
        raise ProtocolError("development review idempotency key must be 1..128 bytes")
    # Full request hashes remain inside the record; a short leaf avoids Windows
    # MAX_PATH at deeply nested campaign roots. Collisions fail closed.
    project = Path(project).resolve()
    path = project / ".popper" / "drev" / (digest(idempotency_key)[:24] + ".json")
    if (not path.parent.resolve().is_relative_to(project) or path.is_symlink()
            or any(part.is_symlink() for part in (project / ".popper", path.parent))):
        raise ProtocolError("development review storage must remain inside the explicit project")
    return path


def _read_saved(path, request_sha256=None):
    try:
        record = read_json(path)
    except (OSError, ValueError) as error:
        raise ProtocolError("development review save is interrupted or unreadable; reconcile without overwriting") from error
    if (not isinstance(record, dict) or record.get("record_sha256") != digest(
            {key: value for key, value in record.items() if key != "record_sha256"})):
        raise ProtocolError("immutable development review integrity failed")
    preview = record.get("review")
    if (not isinstance(preview, dict) or preview.get("approval_sha256") != digest(
            {key: value for key, value in preview.items() if key != "approval_sha256"})):
        raise ProtocolError("immutable development review approval integrity failed")
    if request_sha256 is not None and record.get("request_sha256") != request_sha256:
        raise ProtocolError("development review idempotency conflict")
    return record


def persist_development_review(project, run, session_id, turn_id, job_id,
                               expected_state_version, evidence_sha256, approval_sha256,
                               idempotency_key, *, review=None, source_audit=False):
    """Save an immutable application receipt; exact retries never re-execute.

    Known saved retries return their original evidence snapshot, even if the
    Study subsequently changes. New saves must match a fresh read-only preview.
    A partial file is retained and blocks overwrite/retry rather than guessing.
    """
    _sha(approval_sha256, "approval_sha256")
    _finite_json(review, "supplied development review")
    path = _receipt_path(project, idempotency_key)
    if type(source_audit) is not bool or (source_audit and review is not None):
        raise ProtocolError("source audit cannot contain an operator-authored review")
    request = {"project": str(Path(project).resolve()), "research": str(Path(run).resolve()),
               "session_id": session_id, "turn_id": turn_id, "job_id": job_id,
               "expected_state_version": expected_state_version,
               "evidence_sha256": evidence_sha256, "approval_sha256": approval_sha256,
               "review_sha256": digest(review), "source_audit": source_audit,
               "idempotency_sha256": digest(idempotency_key)}
    request_sha256 = digest(request)
    if path.exists():
        return _read_saved(path, request_sha256)
    preview = (audit_source_turn if source_audit else preview_development_review)(
        project, run, session_id, turn_id, job_id, expected_state_version, evidence_sha256,
        **({"review": review} if not source_audit else {}))
    if preview["approval_sha256"] != approval_sha256:
        raise ProtocolError("review source, evidence or version changed; review again")
    body = {"schema_version": "1.0", "request_sha256": request_sha256,
            "idempotency_sha256": digest(idempotency_key), "review": preview,
            "effect": "application audit only; no execution, budget consumption or Kernel mutation"}
    body["record_sha256"] = digest(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_saved(path, request_sha256)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical(body) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return deepcopy(body)


def load_development_review(project, idempotency_key, *, current_evidence=None):
    """Read an immutable audit and explicitly report stale facts if supplied."""
    result = deepcopy(_read_saved(_receipt_path(project, idempotency_key)))
    if Path(result["review"]["project"]).resolve() != Path(project).resolve():
        raise ProtocolError("saved development review belongs to another project")
    if current_evidence is not None:
        stored = result["review"]
        result["stale"] = any(stored.get(key) != current_evidence.get(key)
                              for key in ("study_id", "state_version", "job_id",
                                          "revision_id", "run_id", "evidence_sha256"))
        result["freshness"] = "needs_new_review" if result["stale"] else "current_evidence"
    else:
        result["freshness"] = "not_checked"
    return result
