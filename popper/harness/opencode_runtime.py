"""Loopback-only OpenCode 1.17.9 JSON HTTP/SSE adapter.

Interface verified against the official v1.17.9 generated SDK and handlers:
https://github.com/anomalyco/opencode/blob/v1.17.9/packages/sdk/js/src/v2/gen/types.gen.ts
https://github.com/anomalyco/opencode/blob/v1.17.9/packages/opencode/src/server/routes/instance/httpapi/handlers/event.ts

The event endpoint has no Last-Event-ID/history replay. JSON event IDs are used
only for deduplication; EOF, malformed frames and network failures require a
read-only reconcile. This module neither starts a process nor configures tools,
credentials, models or global OpenCode settings. The Popper host must establish
those boundaries before using it and persist every outgoing intent first.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
from http.client import HTTPException
import ipaddress
import json
import os
from pathlib import Path
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..core import ProtocolError
from .runtime import (AGENT_RUNTIME_PROTOCOL, RuntimeBinding, RuntimeCancellation,
                      RuntimeCursor, RuntimeDeliveryUnknown, RuntimeDescriptor,
                      RuntimeError, RuntimeEvent, RuntimeReceipt, RuntimeSnapshot,
                      RuntimeStreamError, _identifier)

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_SSE_LINE_BYTES = 64 * 1024
MAX_SSE_FRAME_BYTES = 1024 * 1024
_REJECTED_WRITE_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 413, 415, 422})


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _directory(value):
    try:
        result = Path(value).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("运行目录不存在或无效", code="invalid_directory") from exc
    if not result.is_dir():
        raise RuntimeError("运行目录必须是目录", code="invalid_directory")
    return str(result)


def _same_directory(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenCodeRuntime:
    descriptor = RuntimeDescriptor("opencode", AGENT_RUNTIME_PROTOCOL, "1.17.9")

    def __init__(self, endpoint, *, username, password, timeout=10, stream_timeout=15):
        try:
            url = urlsplit(endpoint)
            port = url.port
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OpenCode 端点无效", code="invalid_endpoint") from exc
        host = url.hostname
        if (url.scheme not in {"http", "https"} or not host or port is None
                or not 1 <= port <= 65535 or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise RuntimeError("OpenCode 必须使用明确的本机端点", code="invalid_endpoint")
        if host.lower() == "localhost":
            try:
                addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                loopback = bool(addresses) and all(
                    ipaddress.ip_address(row[4][0]).is_loopback for row in addresses)
            except (OSError, ValueError):
                loopback = False
        else:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise RuntimeError("OpenCode 端点必须是 loopback", code="non_loopback_endpoint")
        if (not isinstance(username, str) or not username or ":" in username
                or not isinstance(password, str) or not password
                or any(ord(c) < 32 for c in username + password)
                or len(username) > 256 or len(password) > 4096):
            raise RuntimeError("OpenCode 必须配置显式服务认证", code="missing_auth")
        for value in (timeout, stream_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 60:
                raise RuntimeError("OpenCode 超时参数无效", code="invalid_timeout")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.stream_timeout = stream_timeout
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._authorization = f"Basic {token}"
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        # Process-local barriers are additional protection; the host must persist
        # them because this adapter intentionally has no database handle.
        self._unknown = {}
        self._submitted = {}
        self._created = {}
        self._unknown_creates = {}

    def _request(self, method, path, *, directory=None, body=None, query=None,
                 binding=None, message_id=None, operation=None, stream=False,
                 expected_status=200):
        params = dict(query or {})
        if directory is not None:
            params["directory"] = directory
        url = self.endpoint + path + ("?" + urlencode(params) if params else "")
        try:
            data = _canonical(body).encode("utf-8") if body is not None else None
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OpenCode 请求不能序列化", code="invalid_request") from exc
        if data is not None and len(data) > MAX_JSON_BYTES:
            raise RuntimeError("OpenCode 请求过大", code="request_too_large")
        headers = {"Authorization": self._authorization,
                   "Accept": "text/event-stream" if stream else "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, method=method, headers=headers)
        write = method != "GET"
        try:
            response = self._opener.open(req, timeout=self.stream_timeout if stream else self.timeout)
            if stream:
                if response.headers.get_content_type() != "text/event-stream":
                    response.close()
                    raise RuntimeError("OpenCode 返回的不是事件流", code="invalid_stream")
                return response
            with response:
                raw = response.read(MAX_JSON_BYTES + 1)
                if len(raw) > MAX_JSON_BYTES:
                    raise ValueError("oversized response")
                if response.status != expected_status:
                    raise ValueError("unexpected response status")
                if response.status == 204:
                    if raw:
                        raise ValueError("unexpected 204 body")
                    return None
                if response.headers.get_content_type() != "application/json":
                    raise ValueError("invalid response media type")
                return json.loads(raw.decode("utf-8"), parse_constant=lambda _: _invalid_constant())
        except HTTPError as exc:
            if write and exc.code not in _REJECTED_WRITE_STATUSES:
                raise self._unknown_error(operation, binding, message_id) from exc
            code = "redirect_refused" if 300 <= exc.code < 400 else "http_rejected"
            raise RuntimeError(f"OpenCode 请求被拒绝（HTTP {exc.code}）", code=code,
                               binding=binding, operation=operation, message_id=message_id) from exc
        except (OSError, URLError, HTTPException, UnicodeError, ValueError) as exc:
            if write:
                raise self._unknown_error(operation, binding, message_id) from exc
            raise RuntimeError("OpenCode 读取失败或响应无效", code="read_failed",
                               binding=binding, operation=operation) from exc

    def _unknown_error(self, operation, binding, message_id):
        if binding is not None:
            self._unknown[(binding.sha256, message_id, operation)] = True
        return RuntimeDeliveryUnknown(
            "OpenCode 写请求回执不确定；必须核对，不能自动重发", code="delivery_unknown",
            binding=binding, operation=operation, message_id=message_id)

    def health(self):
        data = self._request("GET", "/global/health", operation="health")
        if not isinstance(data, dict) or data.get("healthy") is not True:
            raise RuntimeError("OpenCode 服务未就绪", code="unhealthy")
        if data.get("version") != self.descriptor.expected_version:
            raise RuntimeError("OpenCode 版本未通过适配验收", code="version_mismatch")
        return data

    def _binding(self, data, project_id, session_id, directory, *, existing=None):
        if not isinstance(data, dict):
            raise RuntimeError("OpenCode 会话回执无效", code="invalid_session")
        try:
            binding = RuntimeBinding(project_id, session_id, directory,
                                     data["id"], data["projectID"])
        except (KeyError, TypeError, ValueError, ProtocolError) as exc:
            raise RuntimeError("OpenCode 会话回执缺少标识", code="invalid_session") from exc
        if (not isinstance(data.get("directory"), str)
                or not _same_directory(directory, data["directory"])
                or (existing is not None and existing.sha256 != binding.sha256)):
            raise RuntimeError("OpenCode 会话归属或目录不一致", code="binding_mismatch",
                               binding=binding)
        metadata = data.get("metadata", {})
        if (not isinstance(metadata, dict)
                or metadata.get("popper") != {"project_id": project_id, "session_id": session_id}):
            raise RuntimeError("OpenCode 会话缺少一致的项目绑定", code="binding_mismatch",
                               binding=binding)
        return binding

    def _check_binding(self, binding):
        if not isinstance(binding, RuntimeBinding):
            raise RuntimeError("必须使用已保存的 runtime binding", code="invalid_binding")
        self.health()
        data = self._request("GET", "/session/" + quote(binding.runtime_session_id, safe=""),
                             directory=binding.directory, binding=binding, operation="session")
        return self._binding(data, binding.project_id, binding.session_id,
                             binding.directory, existing=binding)

    def start(self, project_id, session_id, directory, prompt, message_id,
              runtime_session_id=None, *, system=None, model=None, agent=None, tools=None):
        _identifier(project_id, "project_id")
        _identifier(session_id, "session_id")
        _identifier(message_id, "message_id")
        directory = _directory(directory)
        body = self._prompt_body(prompt, message_id, system=system, model=model,
                                 agent=agent, tools=tools)
        scope = (project_id, session_id, directory)
        if runtime_session_id is None and scope in self._unknown_creates:
            raise self._unknown_error("create", None, self._unknown_creates[scope])
        if runtime_session_id is None and scope in self._created:
            runtime_session_id = self._created[scope].runtime_session_id
        self.health()
        if runtime_session_id is None:
            try:
                data = self._request("POST", "/session", directory=directory,
                                     body={"title": "Popper session", "metadata": {
                                         "popper": {"project_id": project_id, "session_id": session_id}}},
                                     operation="create", message_id=message_id)
                binding = self._binding(data, project_id, session_id, directory)
            except RuntimeDeliveryUnknown:
                self._unknown_creates[scope] = message_id
                raise
            except RuntimeError as exc:
                if exc.code == "http_rejected":
                    raise
                # Creation happened but the returned association is untrustworthy.
                self._unknown_creates[scope] = message_id
                raise self._unknown_error("create", exc.binding, message_id) from exc
            self._created[scope] = binding
        else:
            _identifier(runtime_session_id, "runtime_session_id")
            data = self._request("GET", "/session/" + quote(runtime_session_id, safe=""),
                                 directory=directory, operation="session")
            binding = self._binding(data, project_id, session_id, directory)
            if binding.runtime_session_id != runtime_session_id:
                raise RuntimeError("OpenCode 返回其他会话", code="binding_mismatch", binding=binding)
        return self._submit(binding, body)

    def _prompt_body(self, prompt, message_id, *, system=None, model=None, agent=None, tools=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise RuntimeError("Agent 请求不能为空", code="invalid_prompt")
        _identifier(message_id, "message_id")
        body = {"messageID": message_id, "parts": [{"type": "text", "text": prompt}]}
        if system is not None:
            if not isinstance(system, str):
                raise RuntimeError("system 必须为字符串", code="invalid_prompt")
            body["system"] = system
        if model is not None:
            if (not isinstance(model, dict) or set(model) != {"providerID", "modelID"}
                    or any(not isinstance(v, str) or not v for v in model.values())):
                raise RuntimeError("模型标识必须明确", code="invalid_model")
            body["model"] = dict(model)
        if agent is not None:
            body["agent"] = _identifier(agent, "agent")
        if tools is not None:
            if (not isinstance(tools, dict) or any(not isinstance(k, str) or not k
                                                  or not isinstance(v, bool) for k, v in tools.items())):
                raise RuntimeError("工具目录开关无效", code="invalid_tools")
            body["tools"] = dict(tools)
        # Fail locally before creating any session when input cannot be represented.
        try:
            size = len(_canonical(body).encode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("Agent 输入无法序列化", code="invalid_prompt") from exc
        if size > MAX_JSON_BYTES:
            raise RuntimeError("Agent 输入超过运行时传输限制", code="request_too_large")
        return body

    def _submit(self, binding, body):
        message_id = body["messageID"]
        key = (binding.sha256, message_id)
        digest = _digest(body)
        if key in self._submitted:
            if self._submitted[key] != digest:
                raise RuntimeError("消息标识已用于其他输入", code="message_conflict", binding=binding)
            return RuntimeReceipt(binding, message_id, "already_accepted")
        snapshot = self.reconcile(binding)
        for row in snapshot.messages:
            if row["info"]["id"] == message_id:
                texts = [part for part in row["parts"] if part.get("type") == "text"]
                if (row["info"].get("role") != "user" or len(texts) != 1
                        or texts[0].get("text") != body["parts"][0]["text"]
                        or any(row["info"].get(name) != body[name]
                               for name in ("system", "model", "agent", "tools") if name in body)):
                    raise RuntimeError("消息标识已用于其他输入", code="message_conflict", binding=binding)
                return RuntimeReceipt(binding, message_id, "already_observed")
        if any(key[0] == binding.sha256 for key in self._unknown):
            raise self._unknown_error("prompt", binding, message_id)
        if snapshot.status.get("type") != "idle":
            raise RuntimeError("OpenCode 会话仍在运行或重试中", code="session_busy", binding=binding)
        self._request("POST", "/session/" + quote(binding.runtime_session_id, safe="") + "/prompt_async",
                      directory=binding.directory, body=body, binding=binding,
                      message_id=message_id, operation="prompt", expected_status=204)
        self._submitted[key] = digest
        return RuntimeReceipt(binding, message_id)

    def resume(self, binding, prompt, message_id, **kwargs):
        body = self._prompt_body(prompt, message_id, **kwargs)
        self._check_binding(binding)
        return self._submit(binding, body)

    def cancel(self, binding):
        self._check_binding(binding)
        result = self._request("POST", "/session/" + quote(binding.runtime_session_id, safe="") + "/abort",
                               directory=binding.directory, binding=binding, operation="abort")
        if result is not True:
            raise self._unknown_error("abort", binding, None)
        return RuntimeCancellation(binding, abort_requested=True)

    def reconcile(self, binding, *, limit=500):
        self._check_binding(binding)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise RuntimeError("消息读取上限无效", code="invalid_limit")
        statuses = self._request("GET", "/session/status", directory=binding.directory,
                                 binding=binding, operation="status")
        if not isinstance(statuses, dict):
            raise RuntimeError("OpenCode 状态无效", code="invalid_status", binding=binding)
        # The official status service omits idle entries. Session existence and
        # identity were verified separately; idle is only a runtime observation.
        status = statuses.get(binding.runtime_session_id, {"type": "idle"})
        if not isinstance(status, dict) or status.get("type") not in {"idle", "busy", "retry"}:
            raise RuntimeError("OpenCode 状态无效", code="invalid_status", binding=binding)
        messages = self._request("GET", "/session/" + quote(binding.runtime_session_id, safe="") + "/message",
                                 directory=binding.directory, query={"limit": limit},
                                 binding=binding, operation="messages")
        if not isinstance(messages, list) or len(messages) > limit:
            raise RuntimeError("OpenCode 消息列表无效", code="invalid_messages", binding=binding)
        for row in messages:
            if (not isinstance(row, dict) or not isinstance(row.get("info"), dict)
                    or row["info"].get("sessionID") != binding.runtime_session_id
                    or row["info"].get("role") not in {"user", "assistant"}
                    or not isinstance(row["info"].get("id"), str)
                    or not isinstance(row.get("parts"), list)
                    or any(not isinstance(part, dict) or part.get("sessionID") != binding.runtime_session_id
                           or part.get("messageID") != row["info"]["id"] for part in row["parts"])):
                raise RuntimeError("OpenCode 消息归属无效", code="invalid_messages", binding=binding)
        return RuntimeSnapshot(binding, dict(status), tuple(messages), len(messages) < limit)

    def events(self, binding, *, cursor=None, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return
        self._check_binding(binding)
        cursor = cursor or RuntimeCursor()
        if not isinstance(cursor, RuntimeCursor):
            raise RuntimeError("事件 cursor 无效", code="invalid_cursor", binding=binding)
        seen = OrderedDict(cursor.recent_events)
        current = cursor
        try:
            response = self._request("GET", "/event", directory=binding.directory,
                                     binding=binding, operation="events", stream=True)
            with response:
                data_lines = []
                frame_bytes = 0
                while cancel_event is None or not cancel_event.is_set():
                    raw = response.readline(MAX_SSE_LINE_BYTES + 1)
                    if not raw:
                        raise RuntimeStreamError("OpenCode 事件连接已断开，需核对当前状态",
                                                 code="stream_disconnected", binding=binding, cursor=current)
                    if len(raw) > MAX_SSE_LINE_BYTES:
                        raise ValueError("oversized SSE line")
                    frame_bytes += len(raw)
                    if frame_bytes > MAX_SSE_FRAME_BYTES:
                        raise ValueError("oversized SSE frame")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line:
                        if line.startswith("data:"):
                            data_lines.append(line[5:].removeprefix(" "))
                        continue
                    frame_bytes = 0
                    if not data_lines:
                        continue
                    payload = json.loads("\n".join(data_lines), parse_constant=lambda _: _invalid_constant())
                    data_lines = []
                    if (not isinstance(payload, dict) or not isinstance(payload.get("id"), str)
                            or not payload["id"] or len(payload["id"]) > 256
                            or any(ord(c) < 32 for c in payload["id"])
                            or not isinstance(payload.get("type"), str) or not payload["type"]
                            or len(payload["type"]) > 256
                            or any(ord(c) < 32 for c in payload["type"])
                            or not isinstance(payload.get("properties"), dict)):
                        raise ValueError("malformed event")
                    event_id = payload["id"]
                    digest = _digest(payload)
                    event_key = "event:" + event_id
                    if event_key in seen:
                        if seen[event_key] != digest:
                            raise ValueError("conflicting event id")
                        continue
                    seen[event_key] = digest
                    properties = payload["properties"]
                    event_type = payload["type"]
                    duplicate_snapshot = False
                    if event_type in {"message.updated", "message.part.updated"}:
                        item = properties.get("info" if event_type == "message.updated" else "part")
                        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                            raise ValueError("malformed message event")
                        if item.get("sessionID") != properties.get("sessionID"):
                            raise ValueError("conflicting event session")
                        snapshot_key = "snapshot:" + event_type + ":" + item["id"]
                        snapshot_digest = _digest(properties)
                        duplicate_snapshot = seen.get(snapshot_key) == snapshot_digest
                        seen[snapshot_key] = snapshot_digest
                        seen.move_to_end(snapshot_key)
                    while len(seen) > 256:
                        seen.popitem(last=False)
                    current = RuntimeCursor(event_id, tuple(seen.items()))
                    if duplicate_snapshot or properties.get("sessionID") != binding.runtime_session_id:
                        # Global heartbeat/connection/file events are not owned
                        # by this run and never acquire project attribution.
                        continue
                    yield RuntimeEvent(binding, event_id, event_type, properties, current)
        except RuntimeStreamError:
            raise
        except (RuntimeError, OSError, URLError, HTTPException, UnicodeError, ValueError) as exc:
            raise RuntimeStreamError("OpenCode 事件读取失败，需核对消息和状态",
                                     code="stream_invalid_or_failed", binding=binding, cursor=current) from exc


def _invalid_constant():
    raise ValueError("invalid JSON numeric constant")
