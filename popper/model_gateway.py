"""Host-owned, fail-closed model dispatch for an external Agent runtime.

This is not an HTTP forward proxy or a second scientific budget. The host pins
one endpoint/model and injects its existing idempotent reserve/settle services.
Every primary or auxiliary request uses the same sanitized immutable snapshot.
No transport retry is performed. Output events belong to the runtime's durable
conversation; this receipt deliberately retains no raw, potentially sensitive,
stream fragments. A repeated request returns its receipt, not a replayed call.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

from .chat import MODEL_PAYLOAD_LIMIT, SSE_LINE_LIMIT, SSE_WIRE_LIMIT
from .core import ProtocolError, canonical
from .task_context import redact, redact_input


class GatewayConflict(ProtocolError):
    """A request identity cannot be reused for a different input."""


class GatewayBlocked(ProtocolError):
    """Another dispatch or uncertain usage prevents further project calls."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _usage(value):
    """Match SessionStore settlement rules; unknown never means zero."""
    if not isinstance(value, dict) or type(value.get("total_tokens")) is not int:
        raise ProtocolError("provider total_tokens is unavailable")
    result = {name: value.get(name, 0) for name in
              ("prompt_tokens", "completion_tokens", "total_tokens")}
    if any(type(item) is not int or item < 0 for item in result.values()):
        raise ProtocolError("provider token usage is invalid")
    if result["prompt_tokens"] + result["completion_tokens"] > result["total_tokens"]:
        raise ProtocolError("provider usage components exceed total_tokens")
    return result


def _replace_host_secret(value, key):
    """Exact replacement after JSON decoding, including escaped strings."""
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]")
    if isinstance(value, list):
        return [_replace_host_secret(item, key) for item in value]
    if isinstance(value, dict):
        return {name.replace(key, "[REDACTED]"): _replace_host_secret(item, key)
                for name, item in value.items()}
    return value


class _RelayGuard:
    """Hold only a possible credential prefix across native text/arg deltas.

    This does not interpret or repair generated tool arguments. With no actual
    credential occurrence, fragments concatenate to their original text. An
    unfinished substantial credential prefix is concealed at a terminal frame.
    """
    def __init__(self, key):
        self.key, self.pending = key, {}

    def _feed(self, route, text):
        text = (self.pending.pop(route, "") + text).replace(self.key, "[REDACTED]")
        for count in range(min(len(text), len(self.key) - 1), 0, -1):
            if self.key.startswith(text[-count:]):
                self.pending[route] = text[-count:]
                return text[:-count]
        return text

    def apply(self, envelope):
        value = json.loads(json.dumps(envelope, ensure_ascii=False))
        for choice in value.get("choices") or []:
            delta = choice.get("delta") or {}
            choice["delta"] = delta
            for field in ("content", "reasoning_content", "reasoning"):
                if isinstance(delta.get(field), str):
                    delta[field] = self._feed((field, None), delta[field])
            for item in delta.get("tool_calls") or []:
                function, index = item.get("function") or {}, item.get("index")
                if isinstance(function.get("arguments"), str) and type(index) is int:
                    function["arguments"] = self._feed(("arguments", index), function["arguments"])
            if choice.get("finish_reason") is not None:
                for (field, index), held in list(self.pending.items()):
                    suffix = "[REDACTED]" if len(held) >= min(8, len(self.key)) else held
                    if field == "arguments":
                        tool = next((item for item in delta.get("tool_calls", []) if item.get("index") == index), None)
                        if tool is None:
                            tool = {"index": index, "function": {"arguments": ""}}
                            delta.setdefault("tool_calls", []).append(tool)
                        function = tool.setdefault("function", {})
                        function["arguments"] = function.get("arguments", "") + suffix
                    else:
                        delta[field] = delta.get(field, "") + suffix
                    del self.pending[(field, index)]
        return _replace_host_secret(value, self.key)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProtocolError("model endpoint redirects are forbidden")


DDL = """
CREATE TABLE IF NOT EXISTS gateway_requests(
 request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('not_sent','sending','settled','unknown')),
 outcome TEXT NOT NULL, reservation_json TEXT, usage_json TEXT, error_json TEXT,
 result_json TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
 UNIQUE(project_id,run_id,idempotency_key));
CREATE TABLE IF NOT EXISTS gateway_blocks(
 project_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS gateway_snapshot_immutable
 BEFORE UPDATE ON gateway_requests
 WHEN NEW.snapshot_json != OLD.snapshot_json OR NEW.snapshot_sha256 != OLD.snapshot_sha256
 OR NEW.project_id != OLD.project_id OR NEW.run_id != OLD.run_id
 OR NEW.idempotency_key != OLD.idempotency_key OR NEW.request_id != OLD.request_id
 BEGIN SELECT RAISE(ABORT,'gateway input snapshot is immutable'); END;
"""


class ModelGateway:
    """A single host-managed endpoint with durable at-most-once dispatch.

    ``reserve(request_id, snapshot)`` must atomically reserve or raise
    ProtocolError before reserving. It may return a safe reservation reference.
    ``settle(request_id, usage_or_none)`` must be idempotent. None means unknown
    usage, never a refund. Existing session and campaign ledgers can be composed
    behind these callbacks. They are mandatory; no unlimited default exists.

    The application calls ``recover_interrupted`` once on exclusive startup,
    after its runtime worker has stopped. Opening another reader must not mark
    live requests interrupted. Recovery never forwards or resumes a request.
    """

    def __init__(self, db_path, base_url, model, *, reserve, settle,
                 api_key_env="POPPER_API_KEY", max_input_bytes=65536, timeout=120):
        parsed = urlparse(base_url)
        if (parsed.scheme != "https" and not
                (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})):
            raise ProtocolError("model endpoint must be HTTPS or loopback HTTP")
        if (not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ProtocolError("model endpoint must not contain credentials/query/fragment")
        if (not isinstance(model, str) or not model.strip() or len(model) > 256
                or not callable(reserve) or not callable(settle)):
            raise ProtocolError("a fixed model and budget callbacks are required")
        if (not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", api_key_env)
                or type(max_input_bytes) is not int or max_input_bytes <= 0
                or not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 300):
            raise ProtocolError("invalid gateway host configuration")
        self.path = os.fspath(db_path)
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key_env = model.strip(), api_key_env
        self.reserve, self.settle = reserve, settle
        self.max_input_bytes, self.timeout = max_input_bytes, timeout
        self._active = set()
        self._lock = threading.Lock()
        # Disable system proxy inheritance: the only network target is pinned.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        with self._connection() as conn:
            conn.executescript(DDL)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _safe(self, value, key=None):
        cleaned = redact(value)
        secret = key if key is not None else os.environ.get(self.api_key_env)
        if not secret:
            return cleaned
        return _replace_host_secret(cleaned, secret)

    @staticmethod
    def _identity(value, label):
        if (not isinstance(value, str) or not 1 <= len(value) <= 256
                or any(ord(character) < 32 for character in value)):
            raise ProtocolError(f"invalid {label}")
        return value

    def _snapshot(self, project_id, run_id, request, purpose, key):
        if not isinstance(request, dict):
            raise ProtocolError("model request must be a JSON object")
        try:
            wire = json.loads(json.dumps(request, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            raise ProtocolError("model request is not finite JSON") from None
        if wire.get("model", self.model) != self.model:
            raise ProtocolError("model request does not match the pinned model")
        messages = wire.get("messages")
        if (not isinstance(messages, list) or not messages
                or any(not isinstance(item, dict) or item.get("role") not in
                       {"system", "developer", "user", "assistant", "tool"} for item in messages)):
            raise ProtocolError("model request requires explicit message roles")
        if "tools" in wire and not isinstance(wire["tools"], list):
            raise ProtocolError("tools must be an array")
        if wire.get("stream", True) is not True or wire.get("n", 1) != 1:
            raise ProtocolError("gateway requires streaming and a single completion")
        caps = [wire.get(name) for name in ("max_tokens", "max_completion_tokens") if name in wire]
        if not caps or any(type(item) is not int or item <= 0 for item in caps):
            raise ProtocolError("model request requires an explicit positive output limit")
        options = wire.get("stream_options", {})
        if not isinstance(options, dict) or options.get("include_usage", True) is not True:
            raise ProtocolError("stream usage must be requested")
        # Tool definitions are contractual. A credential in one is a rejection,
        # not a schema rewrite. Tool arguments in messages remain source data.
        if key and _replace_host_secret(wire.get("tools", []), key) != wire.get("tools", []):
            raise ProtocolError("tool definitions contain a host credential")
        safe = redact_input(wire)
        safe = {name: item if name == "tools" else self._safe(item, key)
                for name, item in safe.items()}
        safe.update(model=self.model, stream=True,
                    stream_options={**safe.get("stream_options", {}), "include_usage": True})
        selected = {name: safe[name] for name in ("system", "messages", "tools") if name in safe}
        if len(canonical(selected).encode("utf-8")) > self.max_input_bytes:
            raise ProtocolError("system/messages/tools exceed the input byte limit; nothing was sent")
        if len(canonical(safe).encode("utf-8")) > self.max_input_bytes + 16384:
            raise ProtocolError("model request metadata exceeds the byte limit")
        return {"project_id": project_id, "run_id": run_id, "purpose": purpose,
                "endpoint": self.endpoint, "request": safe}

    @staticmethod
    def _receipt(row):
        snapshot = json.loads(row["snapshot_json"])
        if _sha(row["snapshot_json"]) != row["snapshot_sha256"]:
            raise GatewayBlocked("gateway snapshot integrity failure")
        return {"request_id": row["request_id"], "project_id": row["project_id"],
                "run_id": row["run_id"], "state": row["state"], "outcome": row["outcome"],
                "model": snapshot["request"]["model"], "purpose": snapshot["purpose"],
                "snapshot": snapshot, "snapshot_sha256": row["snapshot_sha256"],
                "reservation": json.loads(row["reservation_json"]) if row["reservation_json"] else None,
                "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "created": row["created"], "updated": row["updated"]}

    def get(self, request_id):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM gateway_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise ProtocolError("unknown gateway request")
        return self._receipt(row)

    def project_status(self, project_id):
        with self._connection() as conn:
            block = conn.execute("SELECT * FROM gateway_blocks WHERE project_id=?", (project_id,)).fetchone()
            active = conn.execute("SELECT request_id FROM gateway_requests WHERE project_id=? AND state='sending'",
                                  (project_id,)).fetchall()
        return {"blocked": bool(block or active), "uncertain": bool(block),
                "reason": block["reason"] if block else ("model_request_sending" if active else None),
                "request_id": block["request_id"] if block else (active[0][0] if active else None),
                "sending": [row[0] for row in active]}

    def _finish(self, request_id, state, outcome, *, usage=None, error=None, result=None):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM gateway_requests WHERE request_id=?", (request_id,)).fetchone()
            conn.execute("UPDATE gateway_requests SET state=?,outcome=?,usage_json=?,error_json=?,result_json=?,updated=? WHERE request_id=?",
                         (state, outcome, canonical(usage) if usage is not None else None,
                          canonical(self._safe(error)) if error else None,
                          canonical(self._safe(result)) if result else None, _now(), request_id))
            if state == "unknown":
                conn.execute("INSERT OR REPLACE INTO gateway_blocks VALUES(?,?,?)",
                             (row["project_id"], request_id, (error or {}).get("kind", "unknown_usage")))
        return self.get(request_id)

    def recover_interrupted(self):
        """Exclusive host startup only. Never invoke while dispatch workers run."""
        with self._lock:
            if self._active:
                raise GatewayBlocked("cannot recover while local dispatches are active")
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute("SELECT * FROM gateway_requests WHERE state='sending'").fetchall()
                for row in rows:
                    error = {"kind": "interrupted_dispatch", "message": "dispatch/settlement status is uncertain; host reconciliation required"}
                    conn.execute("UPDATE gateway_requests SET state='unknown',outcome='interrupted',error_json=?,updated=? WHERE request_id=?",
                                 (canonical(error), _now(), row["request_id"]))
                    conn.execute("INSERT OR REPLACE INTO gateway_blocks VALUES(?,?,?)",
                                 (row["project_id"], row["request_id"], error["kind"]))
        return [self.get(row["request_id"]) for row in rows]

    def forward(self, project_id, run_id, idempotency_key, request, *, purpose="primary",
                on_event=None, cancel_event=None):
        """Forward once; callback gets JSON SSE data envelopes and '[DONE]'.

        On client disconnect/cancel, suppress further relay and drain the bounded
        upstream stream for accounting. Missing [DONE] or usage remains unknown.
        The receipt's cancelled outcome is independent of known settlement.
        """
        project_id = self._identity(project_id, "project ID")
        run_id = self._identity(run_id, "run ID")
        idempotency_key = self._identity(idempotency_key, "idempotency key")
        purpose = self._identity(purpose, "request purpose")
        cancel_event = cancel_event or threading.Event()
        key = os.environ.get(self.api_key_env)
        if key and (len(key) > 8192 or any(not 33 <= ord(character) <= 126 for character in key)):
            raise ProtocolError("host API key is not a valid authorization credential")
        if key and any(key in value for value in (project_id, run_id, idempotency_key, purpose, self.endpoint, self.model)):
            raise ProtocolError("a host credential cannot be used as gateway routing metadata")
        snapshot = self._snapshot(project_id, run_id, request, purpose, key)
        body, now = canonical(snapshot), _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT * FROM gateway_requests WHERE project_id=? AND run_id=? AND idempotency_key=?",
                                 (project_id, run_id, idempotency_key)).fetchone()
            if prior:
                if prior["snapshot_sha256"] != _sha(body):
                    raise GatewayConflict("request identity already belongs to another snapshot")
                return self._receipt(prior)
            if conn.execute("SELECT 1 FROM gateway_blocks WHERE project_id=?", (project_id,)).fetchone():
                raise GatewayBlocked("project blocked by unknown model usage or uncertain dispatch")
            if conn.execute("SELECT 1 FROM gateway_requests WHERE project_id=? AND state='sending'", (project_id,)).fetchone():
                raise GatewayBlocked("another model request is still sending for this project")
            request_id = "gateway-" + uuid.uuid4().hex
            pre_cancel = cancel_event.is_set()
            conn.execute("INSERT INTO gateway_requests VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?)",
                         (request_id, project_id, run_id, idempotency_key, body, _sha(body),
                          "not_sent" if pre_cancel or not key else "sending",
                          "cancelled" if pre_cancel else "prepared", now, now))
        if pre_cancel:
            return self.get(request_id)
        if not key:
            return self._finish(request_id, "not_sent", "rejected", error={"kind": "configuration", "message": "host API key is unavailable"})
        with self._lock:
            self._active.add(request_id)
        try:
            try:
                reservation = self.reserve(request_id, snapshot)
            except ProtocolError:
                return self._finish(request_id, "not_sent", "rejected", error={"kind": "budget_rejected", "message": "host budget reservation rejected; nothing was sent"})
            except Exception:
                return self._finish(request_id, "unknown", "blocked", error={"kind": "reservation_uncertain", "message": "budget reservation status is uncertain; nothing was redispatched"})
            with self._connection() as conn:
                conn.execute("UPDATE gateway_requests SET reservation_json=?,updated=? WHERE request_id=?",
                             (canonical(self._safe(reservation)) if reservation is not None else None, _now(), request_id))
            if cancel_event.is_set():
                # This zero is justified by the host's pre-dispatch boundary,
                # never inferred from an upstream error or missing usage.
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                try:
                    self.settle(request_id, usage)
                except Exception:
                    return self._finish(request_id, "unknown", "cancelled", usage=usage,
                                        error={"kind": "settlement_uncertain", "message": "unsent reservation awaits host reconciliation"})
                return self._finish(request_id, "not_sent", "cancelled", usage=usage)
            # Read exact persisted bytes again. Do not dispatch a mutable caller
            # object or a separately reassembled request after reservation.
            receipt = self.get(request_id)
            data = canonical(receipt["snapshot"]["request"]).encode("utf-8")
            upstream = urllib.request.Request(self.endpoint, data=data, method="POST",
                        headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                 "Authorization": "Bearer " + key})
            try:
                with self._opener.open(upstream, timeout=self.timeout) as response:
                    usage, result, outcome, error = self._read(response, cancel_event, on_event, key)
            except Exception as exc:
                # Neither connection failures nor non-SSE HTTP errors prove zero
                # provider consumption. Never expose credential-bearing bodies.
                usage, result = None, None
                outcome = "cancelled" if cancel_event.is_set() else "failed"
                error = {"kind": "transport_uncertain", "message": "model stream did not complete",
                         "exception": type(exc).__name__}
                if isinstance(exc, urllib.error.HTTPError):
                    error["http_status"] = exc.code
            if usage is None:
                try:
                    self.settle(request_id, None)
                except Exception:
                    pass
                return self._finish(request_id, "unknown", outcome, error=error, result=result)
            # Save known provider usage before the external, idempotent ledger
            # settlement. A crash here leaves sending, hence fails closed.
            with self._connection() as conn:
                conn.execute("UPDATE gateway_requests SET usage_json=?,result_json=?,outcome=?,updated=? WHERE request_id=?",
                             (canonical(usage), canonical(self._safe(result)), outcome, _now(), request_id))
            try:
                self.settle(request_id, usage)
            except Exception:
                return self._finish(request_id, "unknown", outcome, usage=usage, result=result,
                                    error={"kind": "settlement_uncertain", "message": "known provider usage awaits host ledger reconciliation"})
            settled = self._finish(request_id, "settled", outcome, usage=usage, error=error, result=result)
            # Withhold the end-of-stream marker until the host has persisted
            # settlement. The runtime must not race ahead into another call.
            if on_event is not None and outcome != "cancelled" and not cancel_event.is_set():
                try:
                    on_event("[DONE]")
                except Exception:
                    pass  # A disconnect after completion cannot undo accounting.
            return settled
        finally:
            with self._lock:
                self._active.discard(request_id)

    def _read(self, response, cancel_event, on_event, key):
        if not response.headers.get("Content-Type", "").lower().startswith("text/event-stream"):
            raise ProtocolError("model response is not SSE")
        wire_bytes = payload_bytes = events = 0
        usage = response_id = finish_reason = None
        completed = disconnected = has_payload = False
        provider_error = None
        checksum = hashlib.sha256()
        relay_guard = _RelayGuard(key)
        data_lines = []
        deadline = time.monotonic() + self.timeout
        def consume(data):
            nonlocal usage, response_id, finish_reason, completed, disconnected
            nonlocal events, payload_bytes, has_payload, provider_error
            if data == "[DONE]":
                completed = True
                return  # The forwarder relays this only after durable settlement.
            else:
                envelope = json.loads(data)
                if not isinstance(envelope, dict):
                    raise ProtocolError("SSE data must be an object")
                events += 1
                checksum.update(data.encode("utf-8"))
                candidate = envelope.get("id")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", candidate):
                    response_id = candidate
                if envelope.get("usage") is not None:
                    usage = _usage(envelope["usage"])
                if "error" in envelope:
                    provider_error = canonical(self._safe(envelope["error"], key))[:2000]
                choices = envelope.get("choices", [])
                if not isinstance(choices, list) or len(choices) > 1:
                    raise ProtocolError("unexpected completion choices")
                if choices:
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        raise ProtocolError("invalid completion choice")
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if not isinstance(reason, str):
                            raise ProtocolError("invalid finish reason")
                        finish_reason = reason
                    delta = choice.get("delta", {})
                    if not isinstance(delta, dict):
                        raise ProtocolError("invalid completion delta")
                    payload_bytes += len(canonical(delta).encode("utf-8"))
                    if payload_bytes > MODEL_PAYLOAD_LIMIT:
                        raise ProtocolError("model payload exceeds the existing byte limit")
                    has_payload = has_payload or bool(delta.get("content") or delta.get("tool_calls"))
            if on_event is not None and not cancel_event.is_set() and not disconnected:
                try:
                    # Never relay provider error bodies, which can quote the
                    # Authorization header. Ordinary deltas/tool arguments are
                    # unchanged except for an exact host-credential occurrence.
                    if "error" in envelope:
                        relay = {name: value for name, value in envelope.items() if name != "error"}
                        relay["error"] = {"message": "upstream model error; see host receipt"}
                    else:
                        relay = envelope
                    safe_relay = relay_guard.apply(relay)
                    on_event(safe_relay)
                except Exception:
                    # A dead local client must not lose known accounting.
                    disconnected = True
        while not completed:
            if time.monotonic() >= deadline:
                raise ProtocolError("model stream exceeded its wall-clock limit")
            line = response.readline(SSE_LINE_LIMIT + 1)
            if time.monotonic() >= deadline:
                raise ProtocolError("model stream exceeded its wall-clock limit")
            if not line:
                if data_lines:
                    consume("\n".join(data_lines))
                break
            if len(line) > SSE_LINE_LIMIT:
                raise ProtocolError("SSE line exceeds the existing byte limit")
            wire_bytes += len(line)
            if wire_bytes > SSE_WIRE_LIMIT:
                raise ProtocolError("SSE stream exceeds the existing byte limit")
            text = line.decode("utf-8").rstrip("\r\n")
            if not text:
                if data_lines:
                    consume("\n".join(data_lines))
                    data_lines = []
            elif text.startswith("data:"):
                data_lines.append(text[5:].lstrip(" "))
            elif text.startswith((":", "event:", "id:", "retry:")):
                continue
            else:
                raise ProtocolError("invalid SSE field")
        result = {"response_id": response_id, "finish_reason": finish_reason,
                  "event_count": events, "stream_sha256": checksum.hexdigest(),
                  "stream_completed": completed, "client_disconnected": disconnected,
                  "provider_error": provider_error}
        cancelled = cancel_event.is_set() or disconnected
        if not completed or usage is None:
            return None, result, "cancelled" if cancelled else "failed", {
                "kind": "incomplete_stream" if not completed else "unknown_usage",
                "message": "complete stream and verified usage required; automatic retry forbidden"}
        if cancelled:
            return usage, result, "cancelled", None
        if provider_error or not has_payload or finish_reason not in {"stop", "tool_calls"}:
            return usage, result, "failed", {
                "kind": "provider_error" if provider_error else "unusable_reply",
                "message": "provider completed with an error, empty or limited reply",
                **({"detail": provider_error} if provider_error else {})}
        return usage, result, "succeeded", None
