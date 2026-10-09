"""Real local provider SSE + persistent backend, with no paid model calls."""
import json
import runpy
import shutil
import threading
import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from popper.api_contracts import new_trace_id
from popper.chat import make_chat_client
from popper.core import initialize
from popper.server import Workstation
from popper.sessions import AgentSessionStore
from popper.core import ProtocolError
from popper.identity import EntityKind, new_id
from unittest.mock import Mock, patch
from popper.research.controller import ResearchController
from popper.research.readout import read_research_state


@pytest.mark.parametrize("invalid_count,tool_name,arguments", [
    (1, "workspace_task_create", "{bad-json"),
    (1, "unregistered_tool", "{}"),
    (1, "workspace_file_read", '{"path":42}'),
    (3, "workspace_task_create", "{bad-json"),
])
def test_completed_invalid_json_is_feedback_then_corrected(tmp_path, monkeypatch, invalid_count, tool_name, arguments):
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if len(requests) <= invalid_count:
                delta = {"tool_calls": [{"index": 0, "id": "native-1", "type": "function",
                    "function": {"name": tool_name, "arguments": arguments}}]}
            else:
                delta = {"content": "参数错误已核对；没有执行工具。"}
            frames = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
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
    project = tmp_path / "project"
    project.mkdir()
    example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(example / name, project / name)
    runpy.run_path(str(example / "generate_data.py"))["generate"](project)
    initialize(project)
    monkeypatch.setenv("POPPER_API_KEY", "local-test-credential")
    client = make_chat_client(f"http://127.0.0.1:{provider.server_port}/v1", "test-model")
    ws = Workstation(("127.0.0.1", 0), project, trusted_local=True, chat_llm=client)
    try:
        session = ws.create_agent_session("SSE correction")
        result = ws.launch_session_turn(session["session_id"], "请提交工具", "sse-invalid-1", new_trace_id())
        for _ in range(3):
            ws.wait_for_jobs(timeout=30)
        store = AgentSessionStore(project)
        try:
            calls = store.tool_calls(session["session_id"])
            assert store.get_turn(result["turn"]["turn_id"])["status"] == "completed", store.get_turn(result["turn"]["turn_id"])["error"]
            assert len(calls) == invalid_count and all(call["status"] == "invalid" for call in calls)
            assert all(call["error"]["executed"] is False and call["result"] is None for call in calls)
            assert store.get_turn(result["turn"]["turn_id"])["status"] == "completed"
            assert store.usage_summary(session["session_id"])["total_tokens"] == (24 if invalid_count == 1 else 36)
            if invalid_count == 1:
                assert ws._chat_payload(store)["continuation"] is None
            if invalid_count == 3:
                state = store.get_continuation_state(store.turns(session["session_id"])[-1]["turn_id"])
                assert state["status"] == "blocked" and state["correction_count"] == 2
        finally:
            store.close()
        assert len(requests) == (2 if invalid_count == 1 else 3)
        messages = requests[1]["messages"]
        assistant = next(m for m in messages if m.get("tool_calls"))
        tool = next(m for m in messages if m["role"] == "tool")
        assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"]
        assert json.loads(tool["content"])["status"] == "invalid"
        if arguments == "{bad-json":
            assert "bad-json" in json.dumps(messages)
    finally:
        ws.wait_for_jobs(timeout=30)
        ws.server_close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


def test_tool_detail_scope_and_explicit_paging(tmp_path):
    source_id = new_id(EntityKind.TOOL_CALL)
    session = new_id(EntityKind.SESSION)
    source = {"call_id": source_id, "session_id": session, "status": "completed",
              "result": {"diff": "x" * 9000, "api_key": "private-credential"}}
    call = {"session_id": session, "arguments": {"source_call_id": source_id, "field_path": "/diff"}}
    store = Mock()
    store.get_tool_call.return_value = source
    ws = object.__new__(Workstation)
    with patch.object(AgentSessionStore, "readonly", return_value=store):
        first = ws._tool_result_for_call(tmp_path, call)
        assert len(first["text"]) == 4096 and first["next_offset"] == 4096
        second = ws._tool_result_for_call(tmp_path, {**call, "arguments": {**call["arguments"], "offset": 4096}})
        assert first["sha256"] == second["sha256"]
        secret = ws._tool_result_for_call(tmp_path, {**call, "arguments": {**call["arguments"], "field_path": "/api_key"}})
        assert "private-credential" not in secret["text"]
        with pytest.raises(ProtocolError):
            ws._tool_result_for_call(tmp_path, {**call, "session_id": new_id(EntityKind.SESSION)})
        source["status"] = "invalid"
        with pytest.raises(ProtocolError):
            ws._tool_result_for_call(tmp_path, call)


def test_sse_correction_separate_revision_approvals_and_real_job(tmp_path, monkeypatch):
    project, run = tmp_path / "project", tmp_path / "research"
    project.mkdir()
    example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(example / name, project / name)
    runpy.run_path(str(example / "generate_data.py"))["generate"](project)
    initialize(project)
    ResearchController.initialize(project, run)
    state = read_research_state(run)
    hypothesis = next(row for row in state["hypotheses"] if row["status"] == "untested" and not row["hypothesis_id"].startswith("H-control-"))
    design = next(row for row in state["designs"] if row["hypothesis_id"] == hypothesis["hypothesis_id"])
    from popper.workspace_files import ProjectFiles
    source = ProjectFiles(project).read("model.py")
    spec = {"study_id": state["manifest"]["study_id"], "expected_state_version": state["state_version"],
            "hypothesis_id": hypothesis["hypothesis_id"], "design_id": design["design_id"],
            "path": "model.py", "expected_sha256": source["sha256"],
            "replacement": source["content"].replace("def main", "# isolated transport test\ndef main")}
    requests = []
    identity = {}
    final_response_sent = threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            store = AgentSessionStore.readonly(project)
            try:
                calls = store.tool_calls(identity["session_id"])
            finally:
                store.close()
            count = len(requests)
            if count == 1:
                name, args = "research_revision_create", {**spec, "approval_sha256": "bad-digest"}
            elif count == 2:
                name, args = "research_revision_preview", spec
            elif count == 3:
                preview = next(call for call in calls if call["tool_name"] == "research.revision_preview")
                name, args = "research_revision_commit", {"study_id": spec["study_id"], "preview_call_id": preview["call_id"]}
            elif count == 4:
                created = next(call for call in calls if call["tool_name"] == "research.revision_commit")
                name, args = "research_revision_run", {"study_id": spec["study_id"], "create_call_id": created["call_id"]}
            elif count == 5:
                executed = next(call for call in calls if call["tool_name"] == "research.revision_run")
                name, args = "research_revision_job", {"study_id": spec["study_id"], "job_id": executed["result"]["job"]["job_id"]}
            else:
                name, args = None, None
            delta = ({"tool_calls": [{"index": 0, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}]} if name else {"content": "已读取真实执行终态，停止。"})
            frames = [{"choices": [{"delta": delta}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}]
            body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())
            self.wfile.flush()
            if name is None:
                final_response_sent.set()

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("POPPER_API_KEY", "local-test-credential")
    client = make_chat_client(f"http://127.0.0.1:{provider.server_port}/v1", "test-model", enable_file_tools=True)
    ws = Workstation(("127.0.0.1", 0), project, trusted_local=True, research_runs=[run], chat_llm=client)
    try:
        identity.update(ws.create_agent_session("real revision transport"))
        ws.launch_session_turn(identity["session_id"], "提出修订", "sse-revision-1", new_trace_id())
        approvals = []
        for _ in range(5):
            for _ in range(3):
                ws.wait_for_jobs(timeout=30)
            store = AgentSessionStore.readonly(project)
            try:
                pending = [call for call in store.tool_calls(identity["session_id"]) if call["status"] == "pending_approval"]
            finally:
                store.close()
            if not pending:
                break
            assert len(pending) == 1
            call = pending[0]
            approvals.append(call["tool_name"])
            assert ws._agent_tool_public(call)["preview"]["ok"]
            ws.decide_agent_tool(call["call_id"], "approve", "isolated test: approve only this operation")
            done = ws.execute_agent_tool(call["call_id"])
            assert done["status"] == "completed", done.get("error")
        assert approvals == ["research.revision_preview", "research.revision_commit", "research.revision_run", "research.revision_job"]
        assert final_response_sent.wait(30), "The real final continuation did not send its response within 30 seconds"
        assert ws.wait_for_jobs(timeout=30) == 0, "The final response was sent but its local continuation did not finish"
        store = AgentSessionStore.readonly(project)
        try:
            final_turn = store.turns(identity["session_id"])[-1]
            assert final_turn["status"] == "completed", final_turn.get("error")
        finally:
            store.close()
        assert len(requests) == 6
        assert read_research_state(run)["observations"]
        assert any("observation" in json.dumps(message).lower() for message in requests[-1]["messages"] if message["role"] == "tool")
        assert ProjectFiles(project).read("model.py")["sha256"] == source["sha256"]
    finally:
        ws.wait_for_jobs(timeout=30)
        ws.server_close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
