"""One attributed native proposal request, never an inferred research decision.

An existing complete source JSON may identify a candidate even when it failed
the DevelopmentReview format. This helper preserves that rejection, checks only
source data against a real tool receipt, and asks the model whether it still
wishes to propose its candidate through native tools. STOP remains valid. No
source edit, approval, execution, retry or confirmation is performed here.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time

from evaluation.review_development_request import (
    _read_budget, _research_quiet, _result, _session_quiet, _validate_arm, _write_new)
from popper.api_contracts import new_trace_id
from popper.core import Experiment, ProtocolError, canonical, digest, file_hash, read_json
from popper.research.development_evidence import build_development_evidence
from popper.research.development_review import (
    _candidate_scope, same_json_value, verify_development_review)
from popper.research.oscillator_campaign import load_campaign
from popper.research.readout import controller_routing_readiness, read_research_state
from popper.server import Workstation
from popper.sessions import AgentSessionStore
from popper.task_context import load_input, redact


IDEMPOTENCY_KEY = "authored-candidate-native-proposal-v1"
_PREFIX = "authored-candidate-native"
_ALLOWED_RUN = "popper_loop-1"
_CANDIDATE_INDEX = 4
_FACT_ROOTS = {"scope", "study_id", "state_version", "job_id", "revision_id",
               "run_id", "metric", "candidate", "control", "comparison", "current_budget"}


def _complete_json(source):
    """Parse one full object, never recover a prefix or repair source JSON."""
    if not isinstance(source, str) or not source.strip():
        raise ProtocolError("source Turn has no complete response")
    if source.startswith("```json\n") and source.endswith("\n```"):
        source = source[8:-4]

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ProtocolError("source JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(source, object_pairs_hook=pairs)
        encoded = canonical(value)
    except (ValueError, TypeError, RecursionError) as error:
        raise ProtocolError("source must be one complete finite JSON object; no repair") from error
    if not isinstance(value, dict) or len(encoded.encode("utf-8")) > 65536:
        raise ProtocolError("source JSON must be an object within 64 KiB")
    return value


def _envelope_pointer(result, pointer):
    """Resolve the unchanged authored pointer against actual call.result."""
    if not isinstance(pointer, str) or not pointer.startswith("/development_diagnostics/"):
        raise ProtocolError("source facts must target the public development result envelope")
    tokens = pointer[1:].split("/")
    if tokens[1] not in _FACT_ROOTS or any(re.search(r"~(?![01])", token) for token in tokens):
        raise ProtocolError("source fact pointer is outside public development facts")
    current = result
    for token in tokens:
        key = token.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, dict):
                current = current[key]
            elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                current = current[int(key)]
            else:
                raise KeyError(key)
        except (KeyError, IndexError) as error:
            raise ProtocolError("source fact pointer has no value in the actual read receipt") from error
    return current


def _quiet_registries(project, research):
    """Constructing Workstation must not recover another registered root/run."""
    project, research = Path(project).resolve(), Path(research).resolve()
    workspace = project / ".popper" / "workspaces.json"
    if workspace.is_file():
        registry = read_json(workspace)
        rows = registry.get("roots") if isinstance(registry, dict) else None
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or not isinstance(rows[0].get("root"), str)
                or Path(rows[0]["root"]).resolve() != project
                or registry.get("current") != rows[0].get("wid")):
            raise ProtocolError("foreign workspace registry could recover unrelated model requests")
    saved = project / ".popper" / "research-runs.json"
    if saved.is_file():
        registry = read_json(saved)
        if (not isinstance(registry, dict) or set(registry) != {"schema_version", "runs"}
                or registry["schema_version"] != "1.0" or not isinstance(registry["runs"], list)
                or any(not isinstance(item, str) or Path(item).resolve() != research
                       for item in registry["runs"])):
            raise ProtocolError("foreign research registry could recover unrelated work")


def _source_data(project, session_id, turn_id, job_id, evidence, ledger):
    with closing(AgentSessionStore.readonly(project)) as store:
        if not store.verify_turn_events()["ok"] or not store.verify_tool_events()["ok"]:
            raise ProtocolError("source Turn/tool event integrity failed")
        turn = store.get_turn(turn_id)
        if turn["session_id"] != session_id or turn["status"] != "completed":
            raise ProtocolError("source must be a completed Turn in this explicit project/session")
        if store.turns(session_id)[-1]["turn_id"] != turn_id:
            raise ProtocolError("source is no longer the latest Turn; do not override a later decision")
        if any(call["turn_id"] == turn_id for call in store.tool_calls(session_id)):
            raise ProtocolError("source already has native tools; settle that original chain instead")
        source = turn["assistant_message"]
        authored = _complete_json(source)
        snapshot = load_input(store, turn_id)
        model_job = store.agent_job_for_turn(turn_id)
        if snapshot is None or model_job is None or model_job["status"] != "succeeded":
            raise ProtocolError("source needs a verified persistent input and successful model job")
        calls = []
        for message in snapshot.get("messages", []):
            if message.get("role") != "tool":
                continue
            call = store.get_tool_call(message["tool_call_id"])
            if call["tool_name"] != "research.revision_job" or call["arguments"] != {
                    "study_id": evidence["study_id"], "job_id": job_id}:
                continue
            if (call["session_id"] != session_id or call["turn_id"] != turn["parent_turn_id"]
                    or call["status"] != "completed" or not isinstance(call["result"], dict)):
                raise ProtocolError("source read is not its same-session completed native parent receipt")
            envelope = _complete_json(message.get("content"))
            declarations = [tool for entry in snapshot["messages"] if entry.get("role") == "assistant"
                            for tool in entry.get("tool_calls", []) if tool.get("id") == call["call_id"]]
            if (len(declarations) != 1 or declarations[0].get("function", {}).get("name") != "research_revision_job"
                    or _complete_json(declarations[0]["function"]["arguments"]) != call["arguments"]
                    or envelope.get("call_id") != call["call_id"] or envelope.get("status") != "completed"
                    or not same_json_value(envelope.get("result"), redact(call["result"]))):
                raise ProtocolError("source input lacks an intact real assistant/tool message group")
            actual = call["result"]
            if (actual.get("study_id") != evidence["study_id"]
                    or actual.get("state_version") != evidence["state_version"]
                    or actual.get("job", {}).get("job_id") != job_id
                    or canonical(actual.get("development_diagnostics")) != canonical(evidence)):
                raise ProtocolError("read diagnostics or state version changed; no sourced continuation")
            calls.append(call)
        if len(calls) != 1:
            raise ProtocolError("source needs exactly one identified development read in its input snapshot")
        call = calls[0]
    path = Path(ledger).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        charge = connection.execute("SELECT * FROM requests WHERE id=?", (turn_id,)).fetchone()
    if (charge is None or charge["state"] not in {"settled", "usage_over_reservation"}
            or type(charge["used"]) is not int or charge["used"] < 0
            or charge["used"] != model_job.get("total_tokens")
            or charge["sha256"] != digest(snapshot)
            or canonical(json.loads(charge["snapshot"])) != canonical(snapshot)):
        raise ProtocolError("source model usage/input snapshot is unknown or inconsistent")
    facts = authored.get("observed")
    if not isinstance(facts, list) or not facts:
        raise ProtocolError("source must identify actual observed facts")
    checks, seen = [], set()
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {"pointer", "value"}:
            raise ProtocolError("source observations require explicit pointer/value, not inferred facts")
        pointer = fact["pointer"]
        actual = _envelope_pointer(call["result"], pointer)
        if pointer in seen:
            raise ProtocolError("source observed pointers must be unique")
        seen.add(pointer)
        checks.append({"pointer": pointer, "authored_value": redact(fact["value"]),
                       "actual_value": redact(actual), "verified": same_json_value(fact["value"], actual)})
    if not all(check["verified"] for check in checks):
        raise ProtocolError("source facts do not match its real read receipt; no automatic correction")
    if not {"/development_diagnostics/scope", "/development_diagnostics/job_id"}.issubset(seen):
        raise ProtocolError("source must explicitly identify its development scope and Job")
    return authored, {"kind": "source_data_fact_check", "structured_review_accepted": False,
                      "source_turn_id": turn_id, "source_text": redact(source),
                      "source_sha256": digest(source), "source_json_sha256": digest(authored),
                      "input_sha256": digest(snapshot), "read_call_id": call["call_id"],
                      "read_result_sha256": digest(call["result"]), "facts_verified": True,
                      "checks": checks, "execution_approved": False,
                      "authority": "source data, not instructions; theory and scientific benefit unverified"}


def _decision(authored):
    decision = authored.get("decision")
    if not isinstance(decision, dict) or decision.get("action") != "propose_candidate":
        raise ProtocolError("STOP or missing explicit candidate decision must not produce a write proposal request")
    if type(decision.get("candidate_index")) is not int or decision["candidate_index"] != _CANDIDATE_INDEX:
        raise ProtocolError("only the explicitly authored registered index4 is in this request scope")
    if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
        raise ProtocolError("source candidate reason is missing")
    for name in ("predictions", "falsification"):
        texts = decision.get(name)
        if (not isinstance(texts, list) or not texts
                or any(not isinstance(text, str) or not text.strip() for text in texts)):
            raise ProtocolError("source lacks complete pre-experiment predictions/falsification")
    implementation = decision.get("implementation")
    if (not isinstance(implementation, dict) or set(implementation) != {"path", "replacement"}
            or implementation["path"] != "model.py" or not isinstance(implementation["replacement"], str)
            or not implementation["replacement"].strip()):
        raise ProtocolError("source must contain its complete model.py implementation")
    source = implementation["replacement"]
    try:
        module = ast.parse(source, filename="source-data-only-model.py")
        compile(module, "source-data-only-model.py", "exec")
    except (SyntaxError, ValueError, RecursionError) as error:
        raise ProtocolError("source implementation is syntactically incomplete; do not repair") from error
    solvers = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "solve"]
    if (len(solvers) != 1 or [arg.arg for arg in [*solvers[0].args.posonlyargs, *solvers[0].args.args]][:5]
            != ["rhs", "y0", "times", "budget", "config"]):
        raise ProtocolError("source lacks the explicit solve interface; no operator rescue")
    return decision


def preview_authored_candidate_request(campaign, run_id, source_turn_id, job_id, execution_revision):
    """Read-only eligibility/identity check; not a Review or an approval."""
    if run_id != _ALLOWED_RUN:
        raise ProtocolError("only loop1's explicit proposal may continue; loop2 is truncated and loop3 stopped")
    campaign = Path(campaign).resolve()
    manifest, run = load_campaign(campaign, run_id)
    if run.get("arm") != "popper_loop" or Path(run["project"]).resolve().parent != campaign / run_id:
        raise ProtocolError("request must belong to the existing loop project in this campaign")
    arm, regression = _validate_arm(campaign, manifest, execution_revision)
    project, research = Path(run["project"]).resolve(), Path(run["research"]).resolve()
    _quiet_registries(project, research)
    session_id = read_json(campaign / run_id / "initial-request.json")["session_id"]
    _session_quiet(project, session_id)
    _research_quiet(research)
    budget = _read_budget(manifest["budget_ledger"])
    evidence = build_development_evidence(project, research, job_id)
    if (evidence.get("scope") != "dev" or evidence.get("job_id") != job_id
            or evidence.get("evidence_sha256") != digest({key: value for key, value in evidence.items()
                                                         if key != "evidence_sha256"})):
        raise ProtocolError("explicit current development evidence required")
    authored, audit = _source_data(project, session_id, source_turn_id, job_id, evidence, manifest["budget_ledger"])
    decision = _decision(authored)
    candidates, attempted, untested = _candidate_scope(project, research, evidence)
    index = decision["candidate_index"]
    if index >= len(candidates) or index in attempted or index not in untested:
        raise ProtocolError("authored index4 must be registered and have no execution attempt")
    current = evidence.get("current_budget") or {}
    kernel, cap = current.get("kernel") or {}, current.get("candidate_development") or {}
    if (type(kernel.get("available")) not in (int, float) or kernel["available"] < 1
            or type(cap.get("limit")) is not int or cap["limit"] != 6
            or type(cap.get("used")) is not int or not 0 <= cap["used"] < 6
            or type(cap.get("remaining")) is not int or cap["remaining"] != 6 - cap["used"]):
        raise ProtocolError("current development budget/six-attempt cap unavailable or inconsistent")
    state = read_research_state(research)
    research_manifest = read_json(research / "research.json")
    if (not state.get("ok") or state.get("state_version") != evidence["state_version"]
            or state.get("confirmation_resource_consumed") is not False):
        raise ProtocolError("research version changed or confirmation resource already consumed")
    hypothesis_id = research_manifest["candidate_hypothesis_ids"][index]
    designs = [row for row in state["designs"] if row.get("hypothesis_id") == hypothesis_id
               and row.get("study_id") == evidence["study_id"] and row.get("status") == "frozen"]
    if len(designs) != 1 or not controller_routing_readiness(research, state, designs[0])["controller_routing_ready"]:
        raise ProtocolError("authored candidate needs its current same-Study frozen Design route")
    with closing(Experiment(project, readonly=True)) as experiment:
        core = experiment.verify_inputs()
    source_path = project / "model.py"
    if (core.get("phase") != "searching" or "model.py" not in core["spec"]["code_files"]
            or core["input_hashes"].get("model.py") != file_hash(source_path)):
        raise ProtocolError("registered source contract changed or development search closed")
    try:
        verify_development_review(evidence, authored, registered_candidates=candidates,
                                  tested_candidate_indices=attempted, untested_indices=untested)
    except ProtocolError as error:
        audit["structured_review_rejection"] = str(error)
    else:
        raise ProtocolError("source is already a structured Review; this format-rejected follow-up is not needed")
    audit.update({"evidence_sha256": evidence["evidence_sha256"],
                  "state_version": evidence["state_version"], "source_decision_sha256": digest(decision),
                  "source_implementation_sha256": digest(decision["implementation"]["replacement"]),
                  "schema_unchanged": True, "observed_pointers_unchanged": True})
    identity = {"study_id": evidence["study_id"], "expected_state_version": evidence["state_version"],
                "hypothesis_id": hypothesis_id, "hypothesis_version": designs[0]["hypothesis_version"],
                "design_id": designs[0]["design_id"], "candidate_index": index,
                "path": "model.py", "expected_sha256": file_hash(source_path), "scope": "dev"}
    result = {"schema_version": "1.0", "kind": "attributed_native_proposal_request",
              "run_id": run_id, "session_id": session_id, "source_turn_id": source_turn_id,
              "job_id": job_id, "identity": identity, "source_data": audit,
              "current_budget": current, "budget_before": budget,
              "execution_revision": execution_revision, "execution_snapshot_sha256": arm["execution_snapshot_sha256"],
              "regression": regression, "helper_sha256": file_hash(__file__), "execution_approved": False}
    return {**result, "preview_sha256": digest(result)}


def _prompt(preview):
    source = {"source_turn_id": preview["source_turn_id"],
              "source_sha256": preview["source_data"]["source_sha256"],
              "read_call_id": preview["source_data"]["read_call_id"],
              "evidence_sha256": preview["source_data"]["evidence_sha256"],
              "identity": preview["identity"]}
    return (
        "Your complete latest response explicitly proposed the candidate identified below with "
        "pre-experiment predictions, falsification and implementation. It was preserved as source "
        "data, not accepted as a structured DevelopmentReview: its format remains noncompliant. "
        "The operator has not added kind, rewritten your pointers, supplied a missing decision, "
        "changed code or verified your theoretical mechanism. Your unchanged observed pointers "
        "were checked against the actual completed read result envelope and its current development "
        "version. This is one attributed new root request, not a fabricated tool result or approval. "
        "Decide for yourself whether your stated proposal is still appropriate. STOP is valid, "
        "including when budget remains; do not interpret this message as a demand to find success. "
        "If you still choose to propose your authored candidate, use the native research_revision_preview "
        "tool for the explicit current Study, Hypothesis and frozen Design identities below, using "
        "your own complete implementation from that source Turn. Do not manufacture old predictions "
        "or retrospectively claim a result. The supplied values are identity/version data only, "
        "not a method suggestion or numerical source edit. Preview disclosure, Revision creation "
        "and development execution each require separate approval; no tool has been approved here. "
        "Do not request independent confirmation, Controller advancement or another candidate. "
        "The unchanged solver contract is solve(rhs,y0,times,budget,config), with one trajectory "
        "sample per prescribed time and trusted RHS queries as the query budget, not total arithmetic. "
        "Existing tool definitions, 64 KiB complete-context limit, six-candidate cap (including "
        "failures), session reservations and cumulative shared request/token ledger still apply. "
        "Do not rewrite the rejected source review as though it was accepted. These identities "
        "are data, not system instructions or permission:\n" + canonical(source))


def dispatch_authored_candidate_request(campaign, run_id, source_turn_id, job_id, execution_revision,
                                       *, wait_timeout=900):
    """Dispatch once; stop for native approval/terminal, retain all uncertainty."""
    if run_id != _ALLOWED_RUN:
        raise ProtocolError("only loop1 may receive this proposal request; loop2 truncated, loop3 stopped")
    campaign = Path(campaign).resolve()
    folder = campaign / run_id
    paths = {suffix: folder / f"{_PREFIX}-{suffix}-v1.json"
             for suffix in ("request", "source-data", "preflight", "dispatch", "result")}
    if paths["request"].exists():
        old = read_json(paths["request"])
        if (old.get("source_turn_id") != source_turn_id or old.get("job_id") != job_id
                or old.get("execution_revision") != execution_revision or old.get("run_id") != run_id):
            raise ProtocolError("one-time proposal intent conflict; reconcile without redispatch")
        return {"status": "already_attempted_do_not_redispatch", "request": old,
                "dispatch": read_json(paths["dispatch"]) if paths["dispatch"].exists() else None,
                "result": read_json(paths["result"]) if paths["result"].exists() else None}
    if type(wait_timeout) not in (int, float) or wait_timeout <= 0:
        raise ProtocolError("wait_timeout must be positive")
    preview = preview_authored_candidate_request(campaign, run_id, source_turn_id, job_id, execution_revision)
    manifest, run = load_campaign(campaign, run_id)
    before = _session_quiet(run["project"], preview["session_id"])
    prompt = _prompt(preview)
    if len(prompt) > 8000 or redact(prompt) != prompt:
        raise ProtocolError("identity-only proposal prompt is overlong or contains credentials")
    request = {"phase": "intent_claimed_not_dispatched", "idempotency_key": IDEMPOTENCY_KEY,
               "run_id": run_id, "source_turn_id": source_turn_id, "job_id": job_id,
               "execution_revision": execution_revision, "preview": preview,
               "prompt": prompt, "prompt_sha256": digest(prompt),
               "operator_intervention": "format rejection/source data audit and current identities only; no method, source edit or approval"}
    try:
        _write_new(paths["request"], request)
    except FileExistsError:
        raise ProtocolError("proposal intent already claimed; reconcile, never redispatch") from None
    ws, dispatched, phase = None, None, "preflight"
    try:
        _write_new(paths["source-data"], preview["source_data"])
        # Recheck before the constructor's persistent-queue recovery, then again
        # after construction before any native launch. Never widen root registries.
        checked = preview_authored_candidate_request(campaign, run_id, source_turn_id, job_id, execution_revision)
        if checked["preview_sha256"] != preview["preview_sha256"]:
            raise ProtocolError("source/registration/version/budget changed after intent")
        ws = Workstation(("127.0.0.1", 0), run["project"], trusted_local=True,
                         research_runs=[run["research"]], chat_base_url=manifest["base_url"],
                         chat_model=manifest["model"], evaluation_budget_path=manifest["budget_ledger"])
        checked = preview_authored_candidate_request(campaign, run_id, source_turn_id, job_id, execution_revision)
        if checked["preview_sha256"] != preview["preview_sha256"]:
            raise ProtocolError("source/registration/version/budget changed before native dispatch")
        ws._validate_turn_input(prompt)
        with closing(AgentSessionStore.readonly(run["project"])) as store:
            snapshot = ws._assemble_session_context(
                store, preview["session_id"], ws._chat_system_prompt(Path(run["project"])), prompt)
        if not any(message.get("role") == "assistant" and message.get("content") == preview["source_data"]["source_text"]
                   for message in snapshot.get("messages", [])):
            raise ProtocolError("complete authored source omitted from native context; no truncated substitute")
        reservation = ws._reserve_context(snapshot)
        budget = _read_budget(manifest["budget_ledger"])
        if (type(reservation) is not int or reservation < 1
                or budget["status"]["used_tokens"] + reservation > budget["policy"]["max_tokens"]):
            raise ProtocolError("shared budget cannot reserve the unchanged complete input/output contract")
        _write_new(paths["preflight"], {"input_sha256": digest(snapshot), "input_bytes": snapshot["input_bytes"],
                                      "reserved_tokens": reservation, "source_turn_id": source_turn_id,
                                      "source_complete_in_native_context": True,
                                      "note": "native launch revalidates/persists its own exact input; no fake parent/tool result"})
        phase = "dispatch"
        dispatched = ws.launch_session_turn(preview["session_id"], prompt, IDEMPOTENCY_KEY, new_trace_id())
        _write_new(paths["dispatch"], {"turn_id": dispatched["turn"]["turn_id"],
                                      "job_id": dispatched["job"]["job_id"],
                                      "source_turn_id": source_turn_id, "native_root_request": True,
                                      "replayed": dispatched.get("replayed", False)})
        if dispatched.get("replayed"):
            raise ProtocolError("native receipt already exists; reconcile without redispatch")
        phase, deadline = "waiting", time.monotonic() + wait_timeout
        while True:
            threads_active = bool(ws.wait_for_jobs(timeout=1))
            observed = _result(run["project"], preview["session_id"], before, dispatched)
            active = threads_active or any(job["status"] in {"queued", "running"} for job in observed["jobs"])
            if not active:
                observed["status"] = "needs_native_approval" if any(
                    call["status"] == "pending_approval" for call in observed["tool_calls"]) else "terminal_source_unreviewed"
                break
            if time.monotonic() >= deadline:
                observed["status"] = "timed_out_uncertain_no_retry"
                observed["note"] = "no cancellation, redispatch or inferred outcome; reconcile persisted receipts"
                break
        observed["budget_after"] = _read_budget(manifest["budget_ledger"], require_known=False)
        if observed["budget_after"]["status"]["unresolved_requests"]:
            observed["status"] = "timed_out_uncertain_no_retry" if active else "usage_unknown_blocked"
        observed["source_turn_id"] = source_turn_id
        observed["structured_source_review_accepted"] = False
        observed["execution_approved"] = False
        _write_new(paths["result"], observed)
        return observed
    except Exception as error:
        if not paths["result"].exists():
            _write_new(paths["result"], {"status": "dispatch_uncertain_no_retry" if phase != "preflight" else "preflight_failed_no_dispatch",
                                         "phase": phase, "source_turn_id": source_turn_id,
                                         "error": {"type": type(error).__name__, "message": str(error)},
                                         "native_dispatch": dispatched,
                                         "note": "intent retained; no repair, approval, cancellation or automatic retry"})
        raise
    finally:
        if ws is not None:
            ws.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--run-id", choices=[_ALLOWED_RUN], required=True)
    parser.add_argument("--source-turn-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--execution-revision", type=int, required=True)
    args = parser.parse_args()
    try:
        result = dispatch_authored_candidate_request(args.campaign, args.run_id, args.source_turn_id,
                                                    args.job_id, args.execution_revision)
    except Exception as error:
        print(canonical(redact({"status": "stopped_no_retry", "error": {
            "type": type(error).__name__, "message": str(error)}})), flush=True)
        return 1
    print(result["status"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
