"""Windows metered domain Worker, selected only by an explicit frozen binding.

LocalWorker supplies the existing manifest/receipt/artifact contracts; this
adapter owns its real sandbox execution and detached supervisor. It is not an
injected test executor and does not change Research Kernel state transitions.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from ..core import ProtocolError, file_hash, read_json, write_json
from ..domains.oscillator_trajectory import PACK
from .execution import assess_execution
from .oscillator_execution import execute_metered, require_windows_boundary
from .workers.base import JobSpec, TERMINAL_JOB_STATUSES, WorkerReceipt
from .workers.local import LocalWorker
from .workers.supervisor import _heartbeat, _watch_cancel

BINDING = "oscillator-worker-binding.json"


def freeze_binding(run_dir, protected_files):
    """Explicit host setup, before dispatch; existing binding is never replaced."""
    require_windows_boundary()
    run = Path(run_dir).resolve()
    manifest = read_json(run / "research.json")
    if manifest["scorer_id"] != PACK.evaluator_id:
        raise ProtocolError("metered Worker requires oscillator evaluator")
    files = {Path(p).resolve() for p in protected_files}
    # Sealing .py alone would still allow reading its cached bytecode. Freeze
    # and seal every existing cache variant for the protected scoring sources.
    for source in tuple(files):
        if source.suffix == ".py":
            files.update(source.parent.joinpath("__pycache__").glob(source.stem + ".*.pyc"))
    files = sorted(files, key=str)
    if not files or any(not p.is_file() for p in files):
        raise ProtocolError("actual protected reference/scoring files required")
    value = {"schema_version": "1.0", "kind": "windows_metered_rhs_v1",
             "research_sha256": file_hash(run / "research.json"),
             "protected_files": {str(p): file_hash(p) for p in files},
             "adapter_sha256": file_hash(Path(__file__)),
             "execution_sha256": file_hash(Path(__file__).with_name("oscillator_execution.py")),
             "network_isolation": "not_kernel_blocked",
             "budget_scope": "RHS queries; not total arithmetic or wall time"}
    value["implementation_files"] = {str(Path(__file__).with_name(name)): file_hash(Path(__file__).with_name(name))
        for name in ("oscillator_broker.py", "oscillator_client.py", "oscillator_driver.py", "revision_archive.py")}
    path = run / BINDING
    if path.exists():
        if read_json(path) != value:
            raise ProtocolError("frozen Worker binding changed")
    else:
        write_json(path, value)
    return {"path": str(path), "sha256": file_hash(path)}


@contextmanager
def _sealing_lock(timeout):
    # Different runs share scoring sources. Serialize their seal/unseal windows
    # across supervisor processes to avoid one process restoring another's seal.
    import msvcrt
    path = Path(tempfile.gettempdir()) / "popper-oscillator-sealing-v1.lock"
    with path.open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ProtocolError("read-sealing execution lease timed out")
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class MeteredOscillatorWorker(LocalWorker):
    def __init__(self, root, revision_store, binding_path):
        super().__init__(root, revision_store)
        self.binding_path = Path(binding_path).resolve()
        self.binding_sha256 = file_hash(self.binding_path)
        self._verify_binding()

    def _verify_binding(self):
        require_windows_boundary()
        if file_hash(self.binding_path) != self.binding_sha256:
            raise ProtocolError("Worker binding changed")
        value = read_json(self.binding_path)
        if value.get("kind") != "windows_metered_rhs_v1" or value.get("schema_version") != "1.0":
            raise ProtocolError("invalid metered Worker binding")
        checks = {self.binding_path.parent / "research.json": value["research_sha256"],
                  Path(__file__): value["adapter_sha256"],
                  Path(__file__).with_name("oscillator_execution.py"): value["execution_sha256"],
                  **{Path(p): sha for p, sha in value["implementation_files"].items()},
                  **{Path(p): sha for p, sha in value["protected_files"].items()}}
        if any(not p.is_file() or file_hash(p) != sha for p, sha in checks.items()):
            raise ProtocolError("bound research/reference/implementation changed")
        return value

    def _spawn_supervisor(self, job_id):
        command = [sys.executable, "-m", "popper.research.oscillator_worker",
                   str(self.root), job_id, str(self.revisions.root),
                   str(self.binding_path), self.binding_sha256]
        with (self.root / job_id / "supervisor.log").open("ab") as log:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, cwd=str(self.root), close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        write_json(self.root / job_id / "supervisor.json",
                   {"pid": proc.pid, "heartbeat": time.time()})
        return proc

    def _execute_job(self, spec, manifest):
        job_dir = self.root / spec.job_id
        workspace = job_dir / "workspace"
        started = time.monotonic()
        status, error_type, gate = "succeeded", None, None
        artifacts = {}
        manifest["status"] = "running"
        write_json(job_dir / "job.json", manifest)
        try:
            binding = self._verify_binding()
            if not spec.sandboxed or spec.gpu_count or len(spec.outputs) != 1:
                raise ProtocolError("metered oscillator requires one sandboxed CPU trajectory output")
            revision = self.revisions.verify(spec.revision_id)
            code = self.revisions.path(spec.revision_id) / "code"
            if file_hash(code.parent / "revision.json") != spec.revision_manifest_sha256:
                raise ProtocolError("Revision manifest changed")
            if Path(spec.entrypoint).name != spec.entrypoint:
                raise ProtocolError("metered entrypoint must be at code root")
            # No old workspace/output from an interrupted call is reused.
            if workspace.exists() or (job_dir / "metered").exists():
                raise ProtocolError("interrupted execution requires audit, never automatic replay")
            shutil.copytree(code, workspace / "code")
            for item in spec.inputs:
                source = self._resolve_input(item)
                target = workspace / "inputs" / item.target
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            self._verify_code_copy(workspace, revision)
            inputs = [item for item in spec.inputs if item.role == "development_features_without_labels"]
            configs = [item for item in spec.inputs if item.role == "frozen_intervention"]
            if len(inputs) != 1 or len(configs) != 1 or len(spec.inputs) != 2:
                raise ProtocolError("unexpected oscillator input contract")
            rows = read_json(self._resolve_input(inputs[0]))
            config = read_json(self._resolve_input(configs[0]))
            PACK.validate_rows(rows, "inputs")
            with _sealing_lock(spec.timeout_seconds):
                metered = execute_metered(code / spec.entrypoint, rows, config,
                    job_dir / "metered", job_id=spec.job_id,
                    protected_files=list(binding["protected_files"]),
                    timeout=spec.timeout_seconds,
                    code_files={name: code / name for name in revision["files"]},
                    trace_execution=spec.require_edit_coverage,
                    on_spawn=lambda proc: write_json(job_dir / "sandbox.pid", {"pid": proc.pid}))
            # Host receipt, trajectories and trace are immutable artifacts, not
            # candidate-written score/count files. Failed cases remain as null.
            output = workspace / spec.outputs[0]
            output.parent.mkdir(parents=True, exist_ok=True)
            write_json(output, metered["payload"])
            for name in ("stdout.log", "stderr.log"):
                shutil.copyfile(job_dir / "metered" / name, job_dir / name)
            for source, target in (("receipt.json", "outputs/rhs-receipt.json"),
                                   ("execution-trace.json", "outputs/_popper_execution.json")):
                if (job_dir / "metered" / source).is_file():
                    shutil.copyfile(job_dir / "metered" / source, workspace / target)
            if spec.require_edit_coverage:
                trace = workspace / "outputs/_popper_execution.json"
                if not trace.is_file():
                    raise ProtocolError("execution trace missing")
                gate = assess_execution(revision["identity"]["execution_changes"], read_json(trace))
                if not gate["passed"]:
                    status, error_type = "implementation_failed", "UnexecutedRevision"
            self._verify_code_copy(workspace, revision)
            self._verify_binding()
            # Case failures are scored by the independent evaluator rather than
            # dropping the whole run or deleting hard cases. Process cancellation
            # remains a cancelled Job, not a successful science result.
            if (job_dir / "cancel_requested").is_file():
                status, error_type = "cancelled", "Cancelled"
        except OSError as error:
            status, error_type = "infrastructure_failed", type(error).__name__
        except Exception as error:
            status, error_type = "implementation_failed", type(error).__name__
            with (job_dir / "stderr.log").open("ab") as log:
                log.write((str(error) + "\n").encode("utf-8"))
        for name in (*spec.outputs, "outputs/rhs-receipt.json", "outputs/_popper_execution.json"):
            if (workspace / name).is_file():
                artifacts[name] = file_hash(workspace / name)
        for name in ("stdout.log", "stderr.log"):
            if (job_dir / name).is_file():
                artifacts[name] = file_hash(job_dir / name)
        receipt = WorkerReceipt(spec.job_id, status, spec.revision_id,
            spec.revision_manifest_sha256, artifacts, time.monotonic() - started,
            "windows_low_integrity_metered_rhs", error_type).payload()
        receipt.update({"worker_binding_sha256": self.binding_sha256,
                        "execution_gate": gate, "network_block": False})
        write_json(job_dir / "receipt.json", receipt)
        manifest.update({"status": status, "receipt_sha256": file_hash(job_dir / "receipt.json")})
        write_json(job_dir / "job.json", manifest)
        return receipt


def controller_for_run(run_dir):
    """Application adapter factory; default research behavior is unchanged."""
    from .controller import ResearchController
    from .revision_archive import RevisionArchiveWorker
    run = Path(run_dir).resolve()
    binding = run / BINDING
    worker = (MeteredOscillatorWorker(run / "jobs", run / "revisions", binding)
              if binding.is_file() else LocalWorker(run / "jobs", run / "revisions"))
    return ResearchController(run, worker=RevisionArchiveWorker(worker, run))


def main():
    parser = argparse.ArgumentParser()
    for name in ("root", "job_id", "revisions", "binding", "binding_sha256"):
        parser.add_argument(name)
    args = parser.parse_args()
    worker = MeteredOscillatorWorker(args.root, args.revisions, args.binding)
    if worker.binding_sha256 != args.binding_sha256:
        raise ProtocolError("supervisor binding digest mismatch")
    job_dir = worker.root / args.job_id
    manifest = worker.read_manifest(args.job_id)
    if manifest["status"] in TERMINAL_JOB_STATUSES:
        return
    if (job_dir / "cancel_requested").exists():
        worker._finalize_manual(job_dir, manifest, "cancelled", "Cancelled")
        return
    done = threading.Event()
    threads = [threading.Thread(target=target, args=(job_dir, done), daemon=True)
               for target in (_heartbeat, _watch_cancel)]
    for thread in threads:
        thread.start()
    try:
        worker._execute_job(JobSpec.from_payload(manifest["spec"]), manifest)
    finally:
        done.set()
        for thread in threads:
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
