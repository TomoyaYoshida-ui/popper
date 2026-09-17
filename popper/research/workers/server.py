"""stdlib worker 服务端：把 LocalWorker 暴露为 /v1/jobs REST 契约。

用于把「分离式 supervisor」搬到任一台远程盒子（AutoDL/恒源云 GPU 实例等）：
本地控制器用 `RemoteWorker(base_url)` 连接，服务端复用 LocalWorker 的
submit/launch/poll/collect/...，从而本地↔云端无感互换（同一 Worker Protocol）。

认证：每个请求校验 `Authorization: Bearer <POPPER_WORKER_TOKEN>`（常量时间比较）；
仅 HTTPS 部署时配合 `--host 0.0.0.0` 暴露，或由用户侧反代终结 TLS。

零第三方运行依赖：`http.server.ThreadingHTTPServer` + `urllib`，与沙箱/渲染
同一条 stdlib-only 约束。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ...core import ProtocolError, canonical, file_hash
from .base import JobSpec
from .local import LocalWorker

_JOB_ID = re.compile(r"JOB-[0-9a-f]{24}")
_MAX_BODY = 1_000_000
_UPLOAD_LIMIT = 64_000_000
_ARTIFACT_LIMIT = 64_000_000


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")


class WorkerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PopperWorker/1.0"
    # 由 serve() 在启动前注入；请求处理器按实例读取这些类属性。
    worker: LocalWorker = None
    token: bytes = None

    # ---- infra ----
    def log_message(self, *args):  # 关闭默认 stderr 访问日志，避免污染 supervisor 日志
        pass

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        provided = header.removeprefix("Bearer ").encode() if header.startswith("Bearer ") else b""
        return hmac.compare_digest(provided, self.token or b"\x00")

    def _reject_auth(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_json_bytes({"error": "unauthorized"}))))
        self.end_headers()
        self.wfile.write(_json_bytes({"error": "unauthorized"}))

    def _read_body(self, limit=_MAX_BODY):
        length = int(self.headers.get("Content-Length", "0"))
        if length > limit:
            raise ProtocolError("请求体超过大小上限")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            raise ProtocolError("缺少请求体")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _stage_revision(self, spec, revision):
        if revision is None:
            return
        manifest = revision["manifest"]
        files = {name: base64.b64decode(content) for name, content in revision["files"].items()}
        self.worker.revisions.materialize(spec.revision_id, manifest, files)

    def _stage_inputs(self, inputs):
        for sha256, content_b64 in inputs.items():
            blob = self.worker.blob_root / sha256
            if blob.is_file() and file_hash(blob) == sha256:
                continue
            data = base64.b64decode(content_b64)
            if hashlib.sha256(data).hexdigest() != sha256:
                raise ProtocolError("上传输入 SHA-256 不匹配")
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(data)

    def _send(self, status, obj=None, payload=None, content_type="application/json"):
        body = payload if payload is not None else (_json_bytes(obj) if obj is not None else b"")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, error):
        status = 400 if isinstance(error, (ProtocolError, ValueError, KeyError, TypeError)) else 500
        self._send(status, {"error": type(error).__name__, "detail": str(error)})

    # ---- routing ----
    def do_GET(self):
        if not self._authorized():
            return self._reject_auth()
        try:
            self._route_get(urlsplit(self.path).path)
        except Exception as error:
            self._error(error)

    def do_POST(self):
        if not self._authorized():
            return self._reject_auth()
        try:
            self._route_post(urlsplit(self.path).path)
        except Exception as error:
            self._error(error)

    def _route_get(self, path):
        if path == "/v1/jobs":
            return self._send(200, {"jobs": self.worker.list_jobs()})
        if path == "/v1/jobs/receipts":
            return self._send(200, {"receipts": self.worker.list_receipts()})
        match = re.fullmatch(r"/v1/jobs/(JOB-[0-9a-f]{24})", path)
        if match:
            return self._send(200, self.worker.poll(match.group(1)))
        match = re.fullmatch(r"/v1/jobs/(JOB-[0-9a-f]{24})/receipt", path)
        if match:
            return self._send(200, self.worker.collect(match.group(1)))
        match = re.fullmatch(r"/v1/jobs/(JOB-[0-9a-f]{24})/logs", path)
        if match:
            return self._send(200, self.worker.failure_context(match.group(1)))
        match = re.fullmatch(r"/v1/jobs/(JOB-[0-9a-f]{24})/artifacts/(.+)", path)
        if match:
            data = self.worker.read_artifact(match.group(1), unquote(match.group(2)))
            if len(data) > _ARTIFACT_LIMIT:
                raise ProtocolError("制品超过大小上限")
            return self._send(200, payload=data, content_type="application/octet-stream")
        match = re.fullmatch(r"/v1/jobs/(JOB-[0-9a-f]{24})/manifest", path)
        if match:
            return self._send(200, self.worker.read_manifest(match.group(1)))
        match = re.fullmatch(r"/v1/jobs/(JOB-[0-9a-f]{24})/workspace/(.+)", path)
        if match:
            data = self.worker.read_workspace_file(match.group(1), unquote(match.group(2)))
            if len(data) > _ARTIFACT_LIMIT:
                raise ProtocolError("workspace 文件超过大小上限")
            return self._send(200, payload=data, content_type="application/octet-stream")
        self._send(404, {"error": "not_found", "detail": path})

    def _route_post(self, path):
        if path == "/v1/jobs":
            body = self._read_body(_UPLOAD_LIMIT)
            spec = JobSpec.from_payload(body["spec"])
            self._stage_revision(spec, body.get("revision"))
            self._stage_inputs(body.get("inputs") or {})
            return self._send(200, self.worker.launch(spec))
        if path == "/v1/jobs/reap":
            body = self._read_body()
            self.worker.reap_stale(body.get("job_ids"), body.get("stale_seconds", 20.0))
            return self._send(200, {"reaped": True, "jobs": self.worker.list_jobs()})
        match = re.fullmatch(r"/v1/jobs/(JOB-[0-9a-f]{24})/cancel", path)
        if match:
            return self._send(200, self.worker.cancel(match.group(1)))
        self._send(404, {"error": "not_found", "detail": path})


def serve(jobs_root, revisions_root, host="0.0.0.0", port=8670,
          token_env="POPPER_WORKER_TOKEN"):
    token = os.environ.get(token_env)
    if not token:
        raise ProtocolError(f"worker 服务端凭据必须来自环境变量 {token_env}")
    worker = LocalWorker(Path(jobs_root), Path(revisions_root))
    handler = type("_BoundHandler", (WorkerHandler,), {"worker": worker, "token": token.encode()})
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Popper worker serving {worker.root} on {host}:{port} "
          f"({len(worker.list_jobs())} known jobs)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()