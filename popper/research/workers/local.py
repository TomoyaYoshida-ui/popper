"""只执行不可变 CodeRevision 的本地受限 worker。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ... import sandbox
from ...core import ProtocolError, file_hash, read_json, write_json
from ..revisions import RevisionStore
from ..execution import PROBE_SOURCE, assess_execution
from .base import TERMINAL_JOB_STATUSES, JobSpec, WorkerReceipt


# supervisor 心跳判活阈值：超过该秒数未收到心跳即视为 supervisor 丢失。
_SUPERVISOR_STALE_SECONDS = 20.0


class LocalWorker:
    def __init__(self, root, revision_store, executor=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.revisions = revision_store if isinstance(revision_store, RevisionStore) \
            else RevisionStore(revision_store)
        self.executor = executor
        # 内容寻址输入缓存：远程服务端把上传的输入按 SHA-256 落盘到这里，
        # 执行时据此解析输入，从而跨机（本地/云端）共享同一套不可变契约。
        self.blob_root = self.root / "_blobs"

    @staticmethod
    def _verify_code_copy(workspace, revision):
        code_dir = workspace / "code"
        actual = {str(path.relative_to(code_dir)).replace("\\", "/"): file_hash(path)
                  for path in sorted(code_dir.rglob("*.py"))}
        if actual != revision["files"]:
            raise ProtocolError("worker 中的 CodeRevision 执行副本已变化")

    def _resolve_input(self, item):
        """按内容寻址解析输入：优先命中 blob 缓存（远程上传），否则回退本机 source 路径。"""
        blob = self.blob_root / item.sha256
        if blob.is_file() and file_hash(blob) == item.sha256:
            return blob
        source = Path(item.source).resolve()
        if not source.is_file() or file_hash(source) != item.sha256:
            raise ProtocolError("JobSpec 输入制品缺失或 SHA-256 变化")
        return source

    def submit(self, spec: JobSpec):
        revision = self.revisions.verify(spec.revision_id)
        revision_path = self.revisions.path(spec.revision_id)
        if file_hash(revision_path / "revision.json") != spec.revision_manifest_sha256:
            raise ProtocolError("JobSpec 绑定的 revision manifest SHA-256 不匹配")
        if spec.entrypoint not in revision["files"]:
            raise ProtocolError("JobSpec entrypoint 不在 CodeRevision 中")
        job_dir = (self.root / spec.job_id).resolve()
        if not job_dir.is_relative_to(self.root):
            raise ProtocolError("worker job 目录越界")
        manifest_path = job_dir / "job.json"
        if manifest_path.is_file():
            current = read_json(manifest_path)
            if current.get("spec") != spec.payload():
                raise ProtocolError("同一 job_id 对应不同 JobSpec")
            return current
        job_dir.mkdir(parents=True)
        manifest = {"schema_version": "1.0", "job_id": spec.job_id,
                    "status": "queued", "spec": spec.payload()}
        write_json(manifest_path, manifest)
        return manifest

    def run(self, spec: JobSpec):
        """同步执行整条生命周期（注入 executor 的测试路径与既有调用方）。"""
        manifest = self.submit(spec)
        if manifest["status"] in TERMINAL_JOB_STATUSES:
            if not (self.root / spec.job_id / "receipt.json").is_file():
                raise ProtocolError("终态 Job 缺少 WorkerReceipt")
            return self.collect(spec.job_id)
        return self._execute_job(spec, manifest)

    def launch(self, spec: JobSpec):
        """异步提交：入队后 detach 一个 supervisor 拥有执行（生产路径）。

        注入 executor（测试）时退化为进程内同步执行，保证既有测试契约不变。
        """
        manifest = self.submit(spec)
        if manifest["status"] in TERMINAL_JOB_STATUSES:
            return manifest
        if manifest["status"] == "running":
            # 已被先前控制器 dispatch：重连，绝不重复 spawn。
            return manifest
        if self.executor is not None:
            self._execute_job(spec, manifest)
        else:
            self._spawn_supervisor(spec.job_id)
        return manifest

    def poll(self, job_id):
        job_dir = (self.root / job_id).resolve()
        if not job_dir.is_relative_to(self.root):
            raise ProtocolError("job_id 越界")
        manifest = read_json(job_dir / "job.json")
        return {"job_id": job_id, "status": manifest["status"],
                "receipt_sha256": manifest.get("receipt_sha256")}

    def cancel(self, job_id):
        job_dir = (self.root / job_id).resolve()
        if not job_dir.is_relative_to(self.root):
            raise ProtocolError("job_id 越界")
        manifest = read_json(job_dir / "job.json")
        if manifest["status"] in TERMINAL_JOB_STATUSES:
            return manifest
        write_json(job_dir / "cancel_requested", {"requested_at": time.time()})
        if manifest["status"] == "queued":
            # 尚未执行：直接落 cancelled 终态（detached supervisor 见标记亦不再执行）。
            return self._finalize_manual(job_dir, manifest, "cancelled", "Cancelled")
        # running：标记已落盘，执行侧在下一检查点采纳（尽力而为）。
        return self.poll(job_id)

    def _spawn_supervisor(self, job_id):
        command = [sys.executable, "-m", "popper.research.workers.supervisor",
                   str(self.root), job_id, str(self.revisions.root)]
        kwargs = {}
        if os.name == "nt":
            # CREATE_NO_WINDOW（而非 DETACHED_PROCESS）：supervisor 需要一个
            # 隐藏控制台供后续 job 子进程继承；DETACHED 会让每个 job 各自
            # 新开一个可见控制台窗口。
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            kwargs["start_new_session"] = True
        log_path = self.root / job_id / "supervisor.log"
        with log_path.open("ab") as log:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, cwd=str(self.root),
                                    close_fds=True, **kwargs)
        write_json(self.root / job_id / "supervisor.json",
                   {"pid": proc.pid, "heartbeat": time.time()})
        return proc

    def _finalize_manual(self, job_dir, manifest, status, error_type):
        """写一个无产物的终态回执（取消 / supervisor 丢失等非执行路径）。"""
        if self.executor:
            backend = "injected_test_executor"
        elif manifest["spec"].get("sandboxed", True):
            # 回执里的后端名必须描述真实跑过的内核机制，不能把 Linux bubblewrap
            # 签成 Windows 低完整性；无可用后端时如实写 no_sandbox_backend。
            backend = sandbox.execution_backend_name() or "no_sandbox_backend"
        else:
            backend = "trusted_local_process_group"
        receipt = WorkerReceipt(
            manifest["job_id"], status, manifest["spec"]["revision_id"],
            manifest["spec"]["revision_manifest_sha256"], {}, 0.0,
            backend, error_type).payload()
        receipt_path = job_dir / "receipt.json"
        write_json(receipt_path, receipt)
        manifest["status"] = status
        manifest["receipt_sha256"] = file_hash(receipt_path)
        write_json(job_dir / "job.json", manifest)
        return manifest

    def reap_stale(self, job_ids=None, stale_seconds=_SUPERVISOR_STALE_SECONDS):
        """把 running 但 supervisor 已死的 job 判 infrastructure_failed（不重跑）。

        重连语义：仅「manifest 停在 running 且心跳过期」才落终态；有活
        supervisor 的在途 job 保持 running，等待 poll 重连。心跳（单调时间戳）
        而非 pid 作为判活依据——pid 复用不会造成误判漏判。
        """
        targets = list(job_ids) if job_ids is not None else [
            path.parent.name for path in self.root.glob("JOB-*/job.json")]
        for job_id in targets:
            job_dir = (self.root / job_id).resolve()
            manifest_path = job_dir / "job.json"
            if not manifest_path.is_file():
                continue
            manifest = read_json(manifest_path)
            if manifest["status"] != "running":
                continue
            marker = job_dir / "supervisor.json"
            if marker.is_file():
                heartbeat = read_json(marker).get("heartbeat", 0)
                if time.time() - float(heartbeat) < stale_seconds:
                    continue
            self._finalize_manual(job_dir, manifest, "infrastructure_failed",
                                  "SupervisorLost")

    def list_jobs(self):
        """按 job_id 排序的 [{job_id, status}]，供控制器重连扫描（不再依赖 .root）。"""
        jobs = []
        for path in self.root.glob("JOB-*/job.json"):
            manifest = read_json(path)
            jobs.append({"job_id": manifest["job_id"], "status": manifest["status"]})
        return sorted(jobs, key=lambda item: item["job_id"])

    def list_receipts(self):
        """按 job_id 排序的回执列表（含 execution_gate），供状态快照（不再依赖 .root）。"""
        receipts = [read_json(path) for path in self.root.glob("JOB-*/receipt.json")]
        return sorted(receipts, key=lambda item: item["job_id"])

    def read_artifact(self, job_id, name):
        """返回已验证（SHA-256 对回执）的制品原始字节；服务端下载与本地 fetch 共用。"""
        job_dir = (self.root / job_id).resolve()
        if not job_dir.is_relative_to(self.root):
            raise ProtocolError("job_id 越界")
        path = (job_dir / name if name in {"stdout.log", "stderr.log"}
                else job_dir / "workspace" / name)
        receipt = read_json(job_dir / "receipt.json")
        expected = receipt.get("artifacts", {}).get(name)
        if expected is None:
            raise ProtocolError(f"Job 未声明制品: {name}")
        if not path.is_file() or file_hash(path) != expected:
            raise ProtocolError(f"worker 输出制品已变化: {name}")
        return path.read_bytes()

    def fetch_artifact(self, job_id, name, destination):
        """把已验证制品落到本地 destination（传输无关的取产物入口）。"""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.read_artifact(job_id, name))
        return str(destination)

    def read_manifest(self, job_id):
        """返回 job.json 全量 manifest（含 spec），供冻结证据重连验证。"""
        job_dir = (self.root / job_id).resolve()
        if not job_dir.is_relative_to(self.root):
            raise ProtocolError("job_id 越界")
        return read_json(job_dir / "job.json")

    def read_workspace_file(self, job_id, name):
        """返回 workspace 内任意相对路径的原始字节（路径受限，不做制品摘要校验）。"""
        relative = Path(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            raise ProtocolError("workspace 相对路径不合法")
        job_dir = (self.root / job_id).resolve()
        if not job_dir.is_relative_to(self.root):
            raise ProtocolError("job_id 越界")
        workspace = (job_dir / "workspace").resolve()
        path = (workspace / relative).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            raise ProtocolError("workspace 文件缺失或路径越界")
        return path.read_bytes()

    def _execute_job(self, spec: JobSpec, manifest):
        job_dir = self.root / spec.job_id
        receipt_path = job_dir / "receipt.json"
        revision = self.revisions.verify(spec.revision_id)
        revision_path = self.revisions.path(spec.revision_id)
        if file_hash(revision_path / "revision.json") != spec.revision_manifest_sha256:
            raise ProtocolError("执行前 revision manifest 摘要变化")
        workspace = job_dir / "workspace"
        if not workspace.exists():
            shutil.copytree(revision_path / "code", workspace / "code")
            for item in spec.inputs:
                source = self._resolve_input(item)
                destination = workspace / "inputs" / item.target
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        self._verify_code_copy(workspace, revision)
        manifest["status"] = "running"
        write_json(job_dir / "job.json", manifest)
        started = time.monotonic()
        if self.executor:
            backend = "injected_test_executor"
        elif spec.sandboxed:
            backend = sandbox.execution_backend_name() or "no_sandbox_backend"
        else:
            backend = "trusted_local_process_group"
        error_type = None
        status = "succeeded"
        execution_gate = None
        trace_name = "outputs/_popper_execution.json"
        stdout_path, stderr_path = job_dir / "stdout.log", job_dir / "stderr.log"
        try:
            if self.executor is None and spec.sandboxed and not sandbox.available():
                raise OSError(
                    "本地受限 worker 在本平台不可用：没有可用的沙箱后端"
                    "（已登记 Windows 低完整性与 Linux bubblewrap，后者需 bwrap 可建非特权命名空间）。"
                    "生成代码不能在受限模式下执行，也不允许降级到 trusted-local。"
                    "请在控制器侧改用 --trusted-local 人工路径，或把 worker 部署到有沙箱后端的平台")
            output_dir = workspace / "outputs"
            temp_dir = workspace / "tmp"
            output_dir.mkdir(exist_ok=True)
            temp_dir.mkdir(exist_ok=True)
            if spec.require_edit_coverage:
                if "execution_changes" not in revision["identity"]:
                    raise ProtocolError("旧 revision 缺少执行改动契约，需创建新版本")
                probe_path = workspace / "_popper_execution_probe.py"
                probe_path.write_text(PROBE_SOURCE, encoding="utf-8")
                # Never reuse stale coverage from an interrupted invocation.
                trace_path = workspace / trace_name
                if trace_path.exists():
                    trace_path.unlink()
            if self.executor is None and spec.sandboxed:
                # Existing code/inputs stay Medium (read-only to the Low token). Mark
                # only the workspace container and declared writable roots as Low.
                sandbox.label_write_scope(workspace)
                sandbox.label_write_scope(output_dir)
                sandbox.label_write_scope(temp_dir)
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            env.update({"TMP": str(temp_dir), "TEMP": str(temp_dir),
                        "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1",
                        # PyTorch also calls default_cache_dir() directly, which
                        # needs getpass.getuser() on Windows. Use a fixed worker
                        # identity, never the parent's username or home directory.
                        "USERNAME": "popper-worker"})
            # Some torch imports initialize disk caches even for eager training.
            # Let the Low process create these children of its writable tmp root;
            # creating them here would give them Medium integrity on Windows.
            env.update({name: str(temp_dir / directory) for name, directory in {
                "TORCHINDUCTOR_CACHE_DIR": "torchinductor",
                "TORCH_HOME": "torch",
                "TORCH_EXTENSIONS_DIR": "torch-extensions",
                "TRITON_CACHE_DIR": "triton",
                "CUDA_CACHE_PATH": "cuda",
                "XDG_CACHE_HOME": "cache",
                "TORCH_COMPILE_DEBUG_DIR": "torch-debug",
            }.items()})
            if spec.gpu_count:
                assigned = [value.strip() for value in
                            os.environ.get("POPPER_GPU_DEVICES", "").split(",")
                            if value.strip()]
                if len(assigned) < spec.gpu_count:
                    raise OSError(
                        "GPU job 需要 POPPER_GPU_DEVICES 显式分配足够的设备")
                env["CUDA_VISIBLE_DEVICES"] = ",".join(assigned[:spec.gpu_count])
            command = [sys.executable, str(workspace / "code" / spec.entrypoint), *spec.args]
            if spec.require_edit_coverage:
                command = [sys.executable, str(probe_path), str(workspace / "code"),
                           spec.entrypoint, str(trace_path), *spec.args]
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                if self.executor:
                    self.executor(command, workspace, env, stdout, stderr, spec)
                else:
                    sandbox.launch(
                        command, cwd=str(workspace), env=env, stdout=stdout, stderr=stderr,
                        timeout_seconds=spec.timeout_seconds, mem_limit_mb=spec.mem_limit_mb,
                        cpu_time_seconds=spec.cpu_time_seconds,
                        max_processes=spec.max_processes, sandboxed=spec.sandboxed,
                        on_spawn=lambda proc: write_json(job_dir / "sandbox.pid",
                                                         {"pid": proc.pid}))
            missing = [name for name in spec.outputs if not (workspace / name).is_file()]
            if missing:
                raise ProtocolError(f"Job 缺少声明输出: {missing}")
            self._verify_code_copy(workspace, revision)
            if spec.require_edit_coverage:
                if not trace_path.is_file() or trace_path.stat().st_size > 2_000_000:
                    raise ProtocolError("执行轨迹缺失或超过大小上限")
                execution_gate = assess_execution(
                    revision["identity"]["execution_changes"], read_json(trace_path))
                # Coverage is evidence: only a trace that proves detected changed
                # statements never ran, or that never completed, fails the job.
                # Ambiguous and partial coverage are recorded in the receipt.
                if not execution_gate["passed"]:
                    status, error_type = "implementation_failed", "UnexecutedRevision"
        except subprocess.TimeoutExpired:
            status, error_type = "infrastructure_failed", "TimeoutExpired"
        except OSError as error:
            status, error_type = "infrastructure_failed", type(error).__name__
        except Exception as error:
            status, error_type = "implementation_failed", type(error).__name__
        finally:
            if self.executor is None and spec.sandboxed:
                for writable in (workspace / "outputs", workspace / "tmp", workspace):
                    try:
                        sandbox.unlabel_write_scope(writable)
                    except Exception:
                        pass
        # 作业被强杀后（taskkill/超时导致非零退出），一旦取消标记已落盘，
        # 终态按 cancelled 而非 failed 上报——镜像 OpenResearch 的
        # should_report_cancelled 语义：已成功的作业不被取消覆盖。
        if (job_dir / "cancel_requested").is_file() and status != "succeeded":
            status, error_type = "cancelled", "Cancelled"
        artifacts = {}
        for name in spec.outputs:
            path = workspace / name
            if path.is_file():
                artifacts[name] = file_hash(path)
        for name, path in (("stdout.log", stdout_path), ("stderr.log", stderr_path)):
            if path.is_file():
                artifacts[name] = file_hash(path)
        if spec.require_edit_coverage and (workspace / trace_name).is_file():
            artifacts[trace_name] = file_hash(workspace / trace_name)
        receipt = WorkerReceipt(
            spec.job_id, status, spec.revision_id, spec.revision_manifest_sha256,
            artifacts, time.monotonic() - started, backend, error_type).payload()
        if spec.require_edit_coverage:
            receipt["execution_gate"] = execution_gate
        write_json(receipt_path, receipt)
        manifest["status"] = status
        manifest["receipt_sha256"] = file_hash(receipt_path)
        write_json(job_dir / "job.json", manifest)
        return receipt

    def collect(self, job_id):
        job_dir = (self.root / job_id).resolve()
        if not job_dir.is_relative_to(self.root):
            raise ProtocolError("job_id 越界")
        manifest = read_json(job_dir / "job.json")
        receipt = read_json(job_dir / "receipt.json")
        if file_hash(job_dir / "receipt.json") != manifest.get("receipt_sha256"):
            raise ProtocolError("WorkerReceipt 已变化")
        for name, expected in receipt["artifacts"].items():
            path = (job_dir / name if name in {"stdout.log", "stderr.log"}
                    else job_dir / "workspace" / name)
            if not path.is_file() or file_hash(path) != expected:
                raise ProtocolError(f"worker 输出制品已变化: {name}")
        return receipt

    def failure_context(self, job_id, max_bytes=8192):
        """Return bounded, verified diagnostics as untrusted model input."""
        if type(max_bytes) is not int or not 1 <= max_bytes <= 32768:
            raise ProtocolError("日志摘录上限必须为 1..32768 字节")
        receipt = self.collect(job_id)
        if receipt["status"] == "succeeded":
            raise ProtocolError("成功 Job 不能作为失败反馈")
        logs = {}
        for name in ("stderr.log", "stdout.log"):
            expected = receipt["artifacts"].get(name)
            if expected is None:
                continue
            path = self.root / job_id / name
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - max_bytes))
                excerpt = stream.read(max_bytes)
            if file_hash(path) != expected:
                raise ProtocolError("读取失败反馈期间日志已变化")
            logs[name] = {"text": excerpt.decode("utf-8", errors="replace"),
                          "sha256": expected, "truncated": size > max_bytes,
                          "total_bytes": size}
        return {"job_id": job_id, "revision_id": receipt["revision_id"],
                "status": receipt["status"], "error_type": receipt.get("error_type"),
                "trust": "untrusted_execution_output", "logs": logs,
                "execution_gate": receipt.get("execution_gate")}
