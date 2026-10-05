"""Real localhost SSE/backend; only the development projection is simulated.

The domain is the existing quadratic engineering fixture, not an oscillator
validation. No paid model, invented Observation, patched execution or forced
reflection loop is used. The deterministic provider is an explicitly simulated
Agent whose stop and new-candidate decisions exercise the transport contract.
"""
import copy
import json
import runpy
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from popper.api_contracts import new_trace_id
from popper.chat import make_chat_client
from popper.core import Experiment, digest, file_hash, initialize, read_json
from popper.research.controller import ResearchController
from popper.research.development_review import verify_development_review
from popper.research.execution_jobs import RevisionExecutionJobStore
from popper.research.readout import read_research_state
from popper.research.revision_drafts import preview_initial_revision, submit_initial_revision
from popper.server import Workstation
from popper.sessions import AgentSessionStore, ToolCallConflict
from popper.task_context import load_input
from popper.workspace_files import ProjectFiles


@pytest.mark.parametrize("decision", ["stop", "propose_candidate"])
def test_development_feedback_real_sse_separate_approvals_and_authored_stop(tmp_path, monkeypatch, decision):
    project, run = tmp_path / "project", tmp_path / "research"
    project.mkdir()
    example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(example / name, project / name)
    runpy.run_path(str(example / "generate_data.py"))["generate"](project)
    initialize(project)
    ResearchController.initialize(project, run)
    source = ProjectFiles(project).read("model.py")
    exp = Experiment(project, readonly=True)
    try:
        candidates = exp.verify_inputs()["spec"]["candidates"]
    finally:
        exp.close()

    def specification(index, marker):
        state = read_research_state(run)
        registered = read_json(run / "research.json")
        hypothesis = next(row for row in state["hypotheses"] if row["status"] == "untested"
            and registered["configs"].get(row["hypothesis_id"]) == candidates[index])
        design = next(row for row in state["designs"] if row["hypothesis_id"] == hypothesis["hypothesis_id"])
        return {"study_id": state["manifest"]["study_id"], "expected_state_version": state["state_version"],
                "hypothesis_id": hypothesis["hypothesis_id"], "design_id": design["design_id"],
                "path": "model.py", "expected_sha256": source["sha256"],
                "replacement": source["content"].replace("def main", f"# {marker}\ndef main")}

    requests, review_sources, projections = [], [], {}
    identity = {}

    def native_calls():
        store = AgentSessionStore.readonly(project)
        try:
            return store.tool_calls(identity["session_id"])
        finally:
            store.close()

    def review_for(evidence, action):
        value = {"kind": "development_review_v1", "observed": [
            {"pointer": "/scope", "value": "dev"},
            {"pointer": "/study_id", "value": evidence["study_id"]},
            {"pointer": "/job_id", "value": evidence["job_id"]},
            {"pointer": "/state_version", "value": evidence["state_version"]},
            {"pointer": "/candidate/observation_id", "value": evidence["candidate"]["observation_id"]},
            {"pointer": "/candidate/trajectory_error", "value": evidence["candidate"]["trajectory_error"]},
            {"pointer": "/candidate/execution_status", "value": "succeeded"},
            {"pointer": "/current_budget/candidate_development/used", "value": evidence["current_budget"]["candidate_development"]["used"]}],
            "mechanism_hypotheses": ["Synthetic fixture hypothesis; it remains unverified."],
            "unsupported_assertions": ["Transport success does not prove general scientific capability."],
            "decision": {"action": action, "reason": "Simulated provider chooses this bounded transport-test branch."}}
        if action == "propose_candidate":
            spec = specification(1, "simulated provider authored next transport fixture")
            value["decision"].update({"candidate_index": 1,
                "predictions": ["The fixture submits a new registered candidate and records its own Observation."],
                "falsification": ["The Job fails or no development Observation is recorded."],
                "implementation": {"path": spec["path"], "replacement": spec["replacement"]}})
        review_sources.append(copy.deepcopy(value))
        return value

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            calls = native_calls()
            index = len(requests)
            delta = {}
            name, arguments = None, None
            if index == 1:
                name, arguments = "research_revision_job", {"study_id": identity["study_id"], "job_id": identity["initial_job"]}
            elif index == 2:
                diagnostic = next(call for call in calls if call["tool_name"] == "research.revision_job")["result"]["development_diagnostics"]
                authored = review_for(diagnostic, decision)
                delta["content"] = json.dumps(authored)
                if decision == "propose_candidate":
                    spec = specification(1, "simulated provider authored next transport fixture")
                    name, arguments = "research_revision_preview", spec
            elif index == 3:
                preview = next(call for call in calls if call["tool_name"] == "research.revision_preview")
                name, arguments = "research_revision_commit", {"study_id": identity["study_id"], "preview_call_id": preview["call_id"]}
            elif index == 4:
                created = next(call for call in calls if call["tool_name"] == "research.revision_commit")
                name, arguments = "research_revision_run", {"study_id": identity["study_id"], "create_call_id": created["call_id"]}
            elif index == 5:
                executed = next(call for call in calls if call["tool_name"] == "research.revision_run")
                name, arguments = "research_revision_job", {"study_id": identity["study_id"], "job_id": executed["result"]["job"]["job_id"]}
            elif index == 6:
                diagnostic = [call for call in calls if call["tool_name"] == "research.revision_job"][-1]["result"]["development_diagnostics"]
                delta["content"] = json.dumps(review_for(diagnostic, "stop"))
            else:
                delta["content"] = "Unexpected extra request; transport test must fail."
            if name:
                delta["tool_calls"] = [{"index": 0, "id": "simulated-provider-" + str(index), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]
            frames = [{"choices": [{"delta": delta}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}]
            body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("POPPER_API_KEY", "local-only-development-feedback-test")
    client = make_chat_client(f"http://127.0.0.1:{provider.server_port}/v1", "simulated-development-provider", enable_file_tools=True)
    ws = Workstation(("127.0.0.1", 0), project, trusted_local=True, research_runs=[run], chat_llm=client)
    try:
        # Establish a genuine first execution before the simulated Agent reads
        # feedback. This is test-fixture setup, not a paid or autonomous proposal.
        initial_spec = specification(0, "isolated development feedback setup")
        preview = preview_initial_revision(run, project, initial_spec)
        created = submit_initial_revision(run, project, initial_spec, preview["proposal_sha256"], actor="fixture_agent")
        enqueued = ws.execute_agent_revision(created["revision_id"], created["manifest_sha256"])
        ws.wait_for_jobs(timeout=30)
        real_payload = ws.revision_job_payload
        assert real_payload(enqueued["job"]["job_id"])["job"]["status"] == "succeeded"

        def simulated_projection(job_id):
            result = real_payload(job_id)
            if result["job"]["status"] == "succeeded":
                state = read_research_state(run)
                job = result["job"]
                observation = next(row for row in state["observations"] if row["run_id"] == job["run_id"] and row["scope"] == "dev")
                queue = RevisionExecutionJobStore.readonly(run / "revision-execution-jobs.sqlite")
                try:
                    used = len({row["revision_id"] for row in queue.list()})
                finally:
                    queue.close()
                projection = {"kind": "development_evidence", "schema_version": "1.0", "scope": "dev", "simulation_only": True,
                    "simulation_note": "Quadratic engineering fixture; simulated diagnostics, not oscillator scoring or RHS attestation.",
                    "study_id": state["manifest"]["study_id"], "state_version": state["state_version"],
                    "job_id": job_id, "revision_id": job["revision_id"], "run_id": job["run_id"], "metric": state["manifest"]["metric"],
                    "source_digests": {"revision_manifest": file_hash(run / "revisions" / job["revision_id"] / "revision.json"),
                                       "evaluation": observation["artifact_sha256"]},
                    "candidate": {"observation_id": observation["observation_id"], "trajectory_error": observation["value"],
                        "execution_status": job["status"], "systems": {}, "cases": [], "trusted_rhs_queries": None},
                    "control": {"observation_id": None, "trajectory_error": None},
                    "comparison": {"absolute_improvement": None, "relative_improvement": None,
                                   "threshold": state["manifest"]["min_meaningful_effect"], "development_threshold_met": None},
                    "current_budget": {"kernel": state["budget"], "candidate_development": {"limit": 6, "used": used, "remaining": 6 - used}}}
                projection["evidence_sha256"] = digest(projection)
                result["development_diagnostics"] = projection
                projections[job_id] = projection
            return result

        # Passthrough preserves real Job retrieval, events and integrity. Only
        # the new oscillator-specific diagnostic projection is simulated.
        monkeypatch.setattr(ws, "revision_job_payload", simulated_projection)
        identity.update(ws.create_agent_session("isolated development feedback SSE"))
        identity.update(study_id=initial_spec["study_id"], initial_job=enqueued["job"]["job_id"])
        ws.launch_session_turn(identity["session_id"], "Read the declared simulation diagnostics, then choose stop or an explicit registered candidate.", "development-feedback-root", new_trace_id())
        approvals = []
        for _ in range(7):
            for _ in range(3):
                ws.wait_for_jobs(timeout=30)
            pending = [call for call in native_calls() if call["status"] == "pending_approval"]
            if not pending:
                break
            assert len(pending) == 1
            call = pending[0]
            assert ws._agent_tool_public(call)["preview"]["ok"]
            approvals.append(call["tool_name"])
            if call["tool_name"] in {"research.revision_commit", "research.revision_run"}:
                with pytest.raises(ToolCallConflict):
                    ws.execute_agent_tool(call["call_id"])
            if call["tool_name"] == "research.revision_preview":
                initial_diagnostic = projections[identity["initial_job"]]
                assessment = verify_development_review(initial_diagnostic, review_sources[0], registered_candidates=candidates,
                                                       tested_candidate_indices={0}, untested_indices={1, 2})
                assert assessment["facts_verified"] and assessment["decision"]["eligible"]
                assert assessment["decision"]["execution_approved"] is False
            ws.decide_agent_tool(call["call_id"], "approve", "Isolated simulation: approve only this declared operation, no scientific advice.")
            done = ws.execute_agent_tool(call["call_id"])
            assert done["status"] == "completed", done.get("error")
            if call["tool_name"] == "research.revision_run":
                assert done["result"]["status"] == "enqueued_not_completed"
        expected = ["research.revision_job"] if decision == "stop" else ["research.revision_job", "research.revision_preview",
            "research.revision_commit", "research.revision_run", "research.revision_job"]
        assert approvals == expected
        assert len(requests) == (2 if decision == "stop" else 6)
        store = AgentSessionStore.readonly(project)
        try:
            turns = store.turns(identity["session_id"])
            assert all(turn["status"] == "completed" for turn in turns)
            final = turns[-1]
            authored = json.loads(final["assistant_message"])
            final_query = [call for call in store.tool_calls(identity["session_id"]) if call["tool_name"] == "research.revision_job"][-1]
            diagnostic = final_query["result"]["development_diagnostics"]
            assessment = verify_development_review(diagnostic, authored, registered_candidates=candidates,
                tested_candidate_indices={0} if decision == "stop" else {0, 1}, untested_indices={1, 2} if decision == "stop" else {2})
            assert assessment["facts_verified"]
            assert assessment["decision"]["authored"]["action"] == "stop"
            assert assessment["mechanism_hypotheses"][0]["status"] == "unverified"
            assert assessment["unsupported_assertions"][0]["status"] == "unsupported"
            assert assessment["decision"]["execution_approved"] is False
            frozen = load_input(store, final["turn_id"])
            summary = next(json.loads(message["content"]) for message in frozen["messages"]
                if message["role"] == "tool" and message["tool_call_id"] == final_query["call_id"])
            assert summary["status"] == "completed"
            summary = summary["result"]
            assert summary["development_diagnostics"] == diagnostic
            for field in ("study_id", "state_version", "job_id", "revision_id", "run_id", "source_digests", "candidate", "current_budget", "evidence_sha256"):
                assert summary["development_diagnostics"][field] == diagnostic[field]
            assistant = next(message for message in frozen["messages"] if message.get("tool_calls")
                and any(tool["id"] == final_query["call_id"] for tool in message["tool_calls"]))
            assert any(tool["id"] == final_query["call_id"] for tool in assistant["tool_calls"])
            assert frozen["tools"]
            assert store.usage_summary(identity["session_id"])["total_tokens"] == len(requests) * 12
            assert not store.running_tool_calls()
        finally:
            store.close()
        final_state = read_research_state(run)
        assert len(final_state["observations"]) == (1 if decision == "stop" else 2)
        assert not final_state["claims"]
        assert ProjectFiles(project).read("model.py")["sha256"] == source["sha256"]
    finally:
        ws.wait_for_jobs(timeout=30)
        ws.server_close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
