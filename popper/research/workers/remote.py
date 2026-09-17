"""远程 worker 客户端契约；服务端可部署在独立 Linux/GPU 权限域。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
from urllib.parse import urlparse, quote
import re
from pathlib import Path

from ...core import ProtocolError, canonical
from .base import JobSpec


class RemoteWorker:
    def __init__(self, base_url, token_env="POPPER_WORKER_TOKEN", revisions=None):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ProtocolError("远程 worker 必须使用 HTTPS；仅本机开发允许 HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProtocolError("worker URL 不能包含凭据、查询或 fragment")
        token = os.environ.get(token_env)
        if not token:
            raise ProtocolError(f"远程 worker 凭据必须来自环境变量 {token_env}")
        self.base_url, self.token = base_url.rstrip("/"), token
        # 只在真正需要时校验：远程端必须能读取本地 revision 与输入制品去上传。
        self.revisions = revisions

    def _check_job_id(self, job_id):
        if not re.fullmatch(r"JOB-[0-9a-f]{24}", job_id):
            raise ProtocolError("远程 worker job_id 不合法")

    def _call(self, method, path, payload=None):
        data = canonical(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ProtocolError("远程 worker 响应超过限制")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except ProtocolError:
            raise
        except Exception as error:
            raise ProtocolError(f"远程 worker 请求失败: {type(error).__name__}") from None

    def submit(self, spec: JobSpec):
        result = self._call("POST", "/v1/jobs", self._upload_payload(spec))
        if result.get("job_id") != spec.job_id:
            raise ProtocolError("远程 worker 返回了其他 job_id")
        return result

    def _upload_payload(self, spec: JobSpec):
        """把 JobSpec 所需的不可变 revision 与输入制品打包为一次传输。

        二者都按内容寻址（revision manifest identity 摘要 + 输入 SHA-256）校验，
        服务端据此幂等落盘；控制器本机路径绝不跨越到远程文件系统。
        """
        if self.revisions is None:
            raise ProtocolError("RemoteWorker 需要 revisions 以同步不可变代码到远端")
        revision = self.revisions.verify(spec.revision_id)
        version_dir = self.revisions.path(spec.revision_id)
        files = {}
        for name in revision["files"]:
            path = version_dir / "code" / name
            files[name] = base64.b64encode(path.read_bytes()).decode("ascii")
        inputs = {}
        for item in spec.inputs:
            source = Path(item.source).resolve()
            if not source.is_file():
                raise ProtocolError("JobSpec 输入制品缺失")
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != item.sha256:
                raise ProtocolError("JobSpec 输入制品 SHA-256 变化")
            inputs[item.sha256] = base64.b64encode(data).decode("ascii")
        return {"spec": spec.payload(),
                "revision": {"manifest": revision, "files": files},
                "inputs": inputs}

    def read_manifest(self, job_id):
        self._check_job_id(job_id)
        return self._call("GET", f"/v1/jobs/{job_id}/manifest")

    def read_workspace_file(self, job_id, name):
        self._check_job_id(job_id)
        return self._download(f"/v1/jobs/{job_id}/workspace/{quote(name, safe='')}")

    def poll(self, job_id):
        if not re.fullmatch(r"JOB-[0-9a-f]{24}", job_id):
            raise ProtocolError("远程 worker job_id 不合法")
        return self._call("GET", f"/v1/jobs/{job_id}")

    def collect(self, job_id):
        if not re.fullmatch(r"JOB-[0-9a-f]{24}", job_id):
            raise ProtocolError("远程 worker job_id 不合法")
        return self._call("GET", f"/v1/jobs/{job_id}/receipt")

    def cancel(self, job_id):
        if not re.fullmatch(r"JOB-[0-9a-f]{24}", job_id):
            raise ProtocolError("远程 worker job_id 不合法")
        return self._call("POST", f"/v1/jobs/{job_id}/cancel")

    def failure_context(self, job_id, max_bytes=8192):
        """返回有界、可验证的失败反馈（与 LocalWorker 同构，日志由服务端摘录）。"""
        if type(max_bytes) is not int or not 1 <= max_bytes <= 32768:
            raise ProtocolError("日志摘录上限必须为 1..32768 字节")
        receipt = self.collect(job_id)
        if receipt["status"] == "succeeded":
            raise ProtocolError("成功 Job 不能作为失败反馈")
        logs = self._call("GET", f"/v1/jobs/{job_id}/logs").get("logs", {})
        return {"job_id": job_id, "revision_id": receipt.get("revision_id"),
                "status": receipt["status"], "error_type": receipt.get("error_type"),
                "logs": logs}

    def launch(self, spec: JobSpec):
        """远程端提交即 launch：服务端 /v1/jobs 复用 LocalWorker.launch 触发 detach。"""
        return self.submit(spec)

    def reap_stale(self, job_ids=None, stale_seconds=20.0):
        payload = {"job_ids": list(job_ids) if job_ids is not None else None,
                   "stale_seconds": stale_seconds}
        return self._call("POST", "/v1/jobs/reap", payload)

    def list_jobs(self):
        return self._call("GET", "/v1/jobs").get("jobs", [])

    def list_receipts(self):
        return self._call("GET", "/v1/jobs/receipts").get("receipts", [])

    def fetch_artifact(self, job_id, name, destination):
        """下载已由服务端验证（SHA-256 对回执）的制品并落到本地 destination。"""
        if not re.fullmatch(r"JOB-[0-9a-f]{24}", job_id):
            raise ProtocolError("远程 worker job_id 不合法")
        data = self._download(f"/v1/jobs/{job_id}/artifacts/{quote(name, safe='')}")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return str(destination)

    def _download(self, path, limit=64_000_000):
        request = urllib.request.Request(
            self.base_url + path, method="GET",
            headers={"Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read(limit + 1)
        except Exception as error:
            raise ProtocolError(f"远程 worker 下载失败: {type(error).__name__}") from None
        if len(data) > limit:
            raise ProtocolError("远程 worker 制品超过大小上限")
        return data
