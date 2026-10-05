"""Real loopback HTTP/SSE contract tests; never starts OpenCode or a model."""
from __future__ import annotations

import base64
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from popper.harness.opencode_runtime import OpenCodeRuntime
from popper.core import ProtocolError
from popper.harness.runtime import (AgentRuntime, RuntimeBinding, RuntimeCursor,
                                   RuntimeDeliveryUnknown, RuntimeError,
                                   RuntimeStreamError)


class FakeService:
    def __init__(self, directory):
        self.directory = str(Path(directory).resolve())
        self.version = "1.17.9"
        self.requests = []
        self.sessions = {}
        self.messages = []
        self.status = {}
        self.frames = []
        self.prompt_mode = "accept"
        self.create_mode = "accept"
        self.redirect = None
        self.auth = "Basic " + base64.b64encode(b"opencode:synthetic-password").decode("ascii")

    def handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.dispatch()

            def do_POST(self):
                self.dispatch()

            def dispatch(self):
                parsed = urlsplit(self.path)
                body = None
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    body = json.loads(self.rfile.read(length))
                service.requests.append((self.command, parsed.path, parse_qs(parsed.query),
                                         body, self.headers.get("Authorization"),
                                         self.headers.get("Last-Event-ID")))
                if self.headers.get("Authorization") != service.auth:
                    self.respond(401, {"error": "auth"})
                    return
                if service.redirect:
                    self.send_response(302)
                    self.send_header("Location", service.redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parsed.path == "/global/health":
                    self.respond(200, {"healthy": True, "version": service.version})
                elif parsed.path == "/session" and self.command == "POST":
                    if service.create_mode == "drop":
                        self.drop()
                        return
                    if service.create_mode == "truncated":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        self.wfile.write(b'{"id":"ses_native1"')
                        self.wfile.flush()
                        self.drop()
                        return
                    info = {"id": "ses_native1", "projectID": "native_project",
                            "directory": service.directory, "metadata": body["metadata"],
                            "title": body["title"]}
                    service.sessions[info["id"]] = info
                    self.respond(200, info)
                elif parsed.path == "/session/status":
                    self.respond(200, service.status)
                elif parsed.path == "/session/ses_native1/message":
                    limit = int(parse_qs(parsed.query).get("limit", [500])[0])
                    self.respond(200, service.messages[-limit:])
                elif parsed.path == "/session/ses_native1/prompt_async":
                    if service.prompt_mode == "drop":
                        self.drop()
                    elif service.prompt_mode == "reject":
                        self.respond(400, {"error": "invalid request"})
                    elif service.prompt_mode == "server_error":
                        self.respond(500, {"error": "uncertain"})
                    elif service.prompt_mode == "wrong_status":
                        self.respond(200, {"accepted": True})
                    else:
                        self.respond(204)
                elif parsed.path == "/session/ses_native1/abort":
                    self.respond(200, True)
                elif parsed.path == "/session/ses_native1":
                    self.respond(200, service.sessions["ses_native1"])
                elif parsed.path == "/event":
                    raw = b"".join(service.frames)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.respond(404, {"error": "not found"})

            def respond(self, status, value=None):
                raw = b"" if status == 204 else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def drop(self):
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

        return Handler

    def user_message(self, message_id="msg_first", text="Read synthetic materials"):
        return {"info": {"id": message_id, "role": "user", "sessionID": "ses_native1"},
                "parts": [{"id": "prt_first", "type": "text", "text": text,
                           "sessionID": "ses_native1", "messageID": message_id}]}

    def event(self, event_id, kind, properties):
        data = {"id": event_id, "type": kind, "properties": properties}
        return b"event: message\ndata: " + json.dumps(data).encode("utf-8") + b"\n\n"


@pytest.fixture
def service(tmp_path):
    fake = FakeService(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    fake.endpoint = f"http://127.0.0.1:{server.server_port}"
    yield fake
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def runtime(service, **kwargs):
    return OpenCodeRuntime(service.endpoint, username="opencode",
                           password="synthetic-password", timeout=1, **kwargs)


def start(client, service, **kwargs):
    return client.start("project_a", "session_a", service.directory,
                        "Read synthetic materials", "msg_first", **kwargs)


def test_contract_start_and_explicit_scope(service):
    client = runtime(service)
    assert isinstance(client, AgentRuntime)
    receipt = start(client, service, system="Host rules",
                    model={"providerID": "popper", "modelID": "synthetic"},
                    agent="popper-research", tools={"popper_read": True})
    assert receipt.delivery == "accepted"
    assert receipt.binding.project_id == "project_a"
    assert receipt.binding.session_id == "session_a"
    assert receipt.binding.runtime_session_id == "ses_native1"
    assert receipt.binding.native_project_id == "native_project"
    assert len(receipt.binding.sha256) == 64
    assert client.descriptor.event_replay_supported is False
    for _method, path, query, _body, auth, _cursor in service.requests:
        assert auth == service.auth
        if path != "/global/health":
            assert query["directory"] == [service.directory]
    prompt = next(row[3] for row in service.requests if row[1].endswith("prompt_async"))
    assert prompt["messageID"] == "msg_first"
    assert prompt["model"] == {"providerID": "popper", "modelID": "synthetic"}
    assert prompt["tools"] == {"popper_read": True}
    assert not any(row[1].endswith(("shell", "init", "summarize")) for row in service.requests)


@pytest.mark.parametrize("endpoint", ["http://example.com:4096", "http://127.0.0.1.evil:4096",
                                     "http://192.168.0.1:4096", "http://user:pass@127.0.0.1:4096",
                                     "http://127.0.0.1:4096/api", "http://127.0.0.1:4096?key=secret",
                                     "http://127.0.0.1", "file:///tmp/runtime"])
def test_remote_or_ambiguous_endpoint_rejected(endpoint):
    with pytest.raises(RuntimeError):
        OpenCodeRuntime(endpoint, username="opencode", password="secret")


@pytest.mark.parametrize("username,password", [("", "secret"), ("opencode", ""),
                                                ("a:b", "secret"), ("opencode", "secret\n")])
def test_requires_explicit_auth(service, username, password):
    with pytest.raises(RuntimeError, match="显式服务认证"):
        OpenCodeRuntime(service.endpoint, username=username, password=password)
    assert service.requests == []


def test_authentication_rejection_does_not_leak_secret(service):
    client = OpenCodeRuntime(service.endpoint, username="opencode", password="wrong-secret")
    with pytest.raises(RuntimeError) as caught:
        client.health()
    assert caught.value.code == "http_rejected"
    assert "wrong-secret" not in str(caught.value)


def test_version_mismatch_prevents_session_creation(service):
    service.version = "1.18.0"
    with pytest.raises(RuntimeError) as caught:
        start(runtime(service), service)
    assert caught.value.code == "version_mismatch"
    assert len(service.requests) == 1
    assert not service.sessions


def test_redirect_not_followed(service):
    service.redirect = "https://example.com/steal"
    with pytest.raises(RuntimeError) as caught:
        runtime(service).health()
    assert caught.value.code == "redirect_refused"
    assert len(service.requests) == 1


@pytest.mark.parametrize("mode", ["drop", "server_error", "wrong_status"])
def test_unknown_prompt_delivery_has_binding_and_no_retry(service, mode):
    service.prompt_mode = mode
    client = runtime(service)
    with pytest.raises(RuntimeDeliveryUnknown) as caught:
        start(client, service)
    failure = caught.value
    assert failure.binding.runtime_session_id == "ses_native1"
    assert failure.message_id == "msg_first"
    assert failure.operation == "prompt"
    with pytest.raises(RuntimeDeliveryUnknown):
        client.resume(failure.binding, "Read synthetic materials", "msg_first")
    assert len([row for row in service.requests if row[1].endswith("prompt_async")]) == 1
    assert len([row for row in service.requests if row[0] == "POST" and row[1] == "/session"]) == 1


def test_known_prompt_rejection_is_not_unknown(service):
    service.prompt_mode = "reject"
    with pytest.raises(RuntimeError) as caught:
        start(runtime(service), service)
    assert type(caught.value) is RuntimeError
    assert caught.value.code == "http_rejected"


@pytest.mark.parametrize("mode", ["drop", "truncated"])
def test_unknown_session_creation_not_automatically_retried(service, mode):
    service.create_mode = mode
    client = runtime(service)
    with pytest.raises(RuntimeDeliveryUnknown) as caught:
        start(client, service)
    assert caught.value.binding is None
    assert caught.value.operation == "create"
    with pytest.raises(RuntimeDeliveryUnknown):
        start(client, service)
    assert len([row for row in service.requests if row[0] == "POST"]) == 1


def test_process_local_success_receipt_deduplicates_before_message_visible(service):
    client = runtime(service)
    binding = start(client, service).binding
    receipt = client.resume(binding, "Read synthetic materials", "msg_first")
    assert receipt.delivery == "already_accepted"
    assert len([row for row in service.requests if row[1].endswith("prompt_async")]) == 1
    assert start(client, service).delivery == "already_accepted"
    assert len([row for row in service.requests if row[0] == "POST" and row[1] == "/session"]) == 1
    with pytest.raises(RuntimeError) as caught:
        client.resume(binding, "Changed input", "msg_first")
    assert caught.value.code == "message_conflict"


def test_restart_reconciles_existing_message_without_resending(service):
    binding = start(runtime(service), service).binding
    service.messages = [service.user_message()]
    restarted = runtime(service)
    receipt = restarted.resume(binding, "Read synthetic materials", "msg_first")
    assert receipt.delivery == "already_observed"
    assert len([row for row in service.requests if row[1].endswith("prompt_async")]) == 1


def test_existing_binding_never_inferred_from_title(service):
    binding = start(runtime(service), service).binding
    service.sessions["ses_native1"]["title"] = "Another title does not change ownership"
    client = runtime(service)
    assert client.reconcile(binding).binding == binding
    service.sessions["ses_native1"]["metadata"]["popper"]["session_id"] = "other"
    with pytest.raises(RuntimeError) as caught:
        client.reconcile(binding)
    assert caught.value.code == "binding_mismatch"


@pytest.mark.parametrize("changed", [{"project_id": "project_b"}, {"session_id": "session_b"},
                                      {"native_project_id": "other_native"},
                                      {"directory": "C:/not-the-project"}])
def test_binding_scope_change_rejected(service, changed):
    binding = start(runtime(service), service).binding
    with pytest.raises(RuntimeError) as caught:
        runtime(service).reconcile(replace(binding, **changed))
    assert caught.value.code == "binding_mismatch"


def test_busy_or_retry_session_does_not_accept_new_prompt(service):
    binding = start(runtime(service), service).binding
    for status in ({"type": "busy"}, {"type": "retry", "attempt": 1}):
        service.status = {"ses_native1": status}
        with pytest.raises(RuntimeError) as caught:
            runtime(service).resume(binding, "Next prompt", "msg_next")
        assert caught.value.code == "session_busy"
    assert len([row for row in service.requests if row[1].endswith("prompt_async")]) == 1


def test_idle_reconcile_and_abort_are_not_scientific_completion(service):
    client = runtime(service)
    binding = start(client, service).binding
    snapshot = client.reconcile(binding)
    assert snapshot.status == {"type": "idle"}
    assert not hasattr(snapshot, "success")
    cancelled = client.cancel(binding)
    assert cancelled.abort_requested is True
    assert not hasattr(cancelled, "cancelled")
    assert len([row for row in service.requests if row[1].endswith("abort")]) == 1


def test_reconcile_checks_message_ownership_and_history_limit(service):
    client = runtime(service)
    binding = start(client, service).binding
    service.messages = [service.user_message()]
    assert client.reconcile(binding, limit=1).history_complete is False
    service.messages[0]["parts"][0]["sessionID"] = "other"
    with pytest.raises(RuntimeError) as caught:
        client.reconcile(binding)
    assert caught.value.code == "invalid_messages"


def test_events_scope_dedup_delta_and_eof_require_reconcile(service):
    client = runtime(service)
    binding = start(client, service).binding
    info = {"sessionID": "ses_native1", "info": {
        "id": "msg_assistant", "sessionID": "ses_native1", "role": "assistant"}}
    delta = {"sessionID": "ses_native1", "messageID": "msg_assistant", "partID": "prt_1",
             "field": "text", "delta": "Hello"}
    service.frames = [service.event("evt_connected", "server.connected", {}),
                      service.event("evt_other", "session.idle", {"sessionID": "other"}),
                      service.event("evt_message", "message.updated", info),
                      service.event("evt_message", "message.updated", info),
                      service.event("evt_message_copy", "message.updated", info),
                      service.event("evt_delta", "message.part.delta", delta),
                      service.event("evt_delta", "message.part.delta", delta),
                      service.event("evt_idle", "session.idle", {"sessionID": "ses_native1"})]
    rows = []
    with pytest.raises(RuntimeStreamError) as caught:
        rows.extend(client.events(binding))
    assert [row.type for row in rows] == ["message.updated", "message.part.delta", "session.idle"]
    assert caught.value.code == "stream_disconnected"
    assert caught.value.requires_reconcile is True
    assert caught.value.cursor.last_event_id == "evt_idle"
    assert not any(row[5] for row in service.requests)  # No invented Last-Event-ID support.
    resumed = []
    with pytest.raises(RuntimeStreamError):
        resumed.extend(client.events(binding, cursor=caught.value.cursor))
    assert resumed == []
    assert not any(row[1].endswith("prompt_async") for row in service.requests[-5:])


@pytest.mark.parametrize("frames", [[b"data: not-json\n\n"],
                                    [b'data: {"type":"session.idle","properties":{}}\n\n'],
                                    [b"data: " + b"x" * (64 * 1024) + b"\n\n"],
                                    [b"data: \xff\n\n"]])
def test_malformed_stream_fails_closed(service, frames):
    client = runtime(service)
    binding = start(client, service).binding
    service.frames = frames
    with pytest.raises(RuntimeStreamError) as caught:
        list(client.events(binding))
    assert caught.value.requires_reconcile is True
    assert caught.value.code == "stream_invalid_or_failed"


def test_conflicting_duplicate_event_id_fails_closed(service):
    client = runtime(service)
    binding = start(client, service).binding
    service.frames = [service.event("evt_1", "session.idle", {"sessionID": "ses_native1"}),
                      service.event("evt_1", "session.status", {"sessionID": "ses_native1",
                                                               "status": {"type": "busy"}})]
    rows = []
    with pytest.raises(RuntimeStreamError):
        rows.extend(client.events(binding))
    assert len(rows) == 1


def test_cancelled_event_reader_does_not_abort_or_make_requests(service):
    client = runtime(service)
    binding = start(client, service).binding
    flag = threading.Event()
    flag.set()
    count = len(service.requests)
    assert list(client.events(binding, cancel_event=flag)) == []
    assert len(service.requests) == count


def test_cursor_json_roundtrip_and_validation():
    cursor = RuntimeCursor("evt_1", (("event:evt_1", "a" * 64),))
    restored = RuntimeCursor(**json.loads(json.dumps({"last_event_id": cursor.last_event_id,
                                                   "recent_events": cursor.recent_events})))
    assert restored == cursor
    with pytest.raises(ProtocolError, match="cursor"):
        RuntimeCursor(recent_events=(("event:x", "bad"),))
