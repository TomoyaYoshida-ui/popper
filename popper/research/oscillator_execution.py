"""Host-side Windows execution adapter. No Research Kernel mutations.

Results come from the scoped broker, never from files written by the candidate.
Receipts live outside the Low-integrity workspace. Not a public execution API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from .. import sandbox
from ..core import ProtocolError, digest, file_hash, portable_path, read_json, write_json
from ..domains.oscillator_trajectory import PACK
from .oscillator_broker import WindowsRHSBroker


def minimal_environment():
    return {k: v for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}


def require_windows_boundary():
    if os.name != "nt" or not sandbox.available() or not sandbox.seal_read_available():
        raise ProtocolError("Windows OS sandbox and actual read sealing required")


def probe_read_isolation(protected_files):
    require_windows_boundary()
    files = [Path(p).resolve() for p in protected_files]
    if not files or any(not p.is_file() for p in files):
        raise ProtocolError("read-denial probe requires existing protected files")
    with tempfile.TemporaryDirectory(prefix="popper-read-probe-") as directory:
        root = Path(directory)
        probe, result = root / "probe.py", root / "result.json"
        probe.write_text(
            "import json,sys\nfrom pathlib import Path\nresults=[]\n"
            "for p in json.loads(sys.argv[1]):\n"
            " try:\n  Path(p).read_bytes(); results.append(False)\n"
            " except PermissionError:\n  results.append(True)\n"
            "Path(sys.argv[2]).write_text(json.dumps({'denied':results,'executed':True}))\n",
            encoding="utf-8")
        sandbox.label_write_scope(root)
        try:
            with sandbox.sealed_reads(files):
                # A denial for the child must not prevent the trusted host reading.
                host_hashes = [file_hash(p) for p in files]
                with (root / "stdout.log").open("wb") as out, (root / "stderr.log").open("wb") as err:
                    sandbox.launch([sys.executable, str(probe), json.dumps([str(p) for p in files]), str(result)],
                                   cwd=str(root), env=minimal_environment(), stdout=out, stderr=err,
                                   timeout_seconds=10, max_processes=2, sandboxed=True)
                received = read_json(result)
                if received != {"denied": [True] * len(files), "executed": True}:
                    raise ProtocolError("candidate could read a protected research artifact")
                if host_hashes != [file_hash(p) for p in files]:
                    raise ProtocolError("protected research artifact changed during probe")
            return {"passed": True, "protected_count": len(files),
                    "host_read_verified": True, "network_block": sandbox.netblock_available(),
                    "protected_sha256": host_hashes}
        finally:
            sandbox.unlabel_write_scope(root)


def execute_metered(candidate, rows, config, destination, *, job_id,
                    protected_files, timeout=120, code_files=None,
                    trace_execution=False, on_spawn=None):
    require_windows_boundary()
    PACK.validate_rows(rows, "inputs")
    candidate = Path(candidate).resolve()
    destination = Path(destination).resolve()
    if not candidate.is_file() or destination.exists():
        raise ProtocolError("candidate required and execution destination must be new")
    if not protected_files:
        raise ProtocolError("protected reference/scoring files must be explicit")
    probe = probe_read_isolation(protected_files)
    destination.mkdir(parents=True)
    source_root = Path(__file__).resolve().parent
    paths = {candidate.name: candidate} if code_files is None else {
        name: Path(path).resolve() for name, path in code_files.items()}
    if candidate.name not in paths or any(not path.is_file() for path in paths.values()):
        raise ProtocolError("candidate code binding incomplete")
    reserved_names = {"oscillator_client.py", "oscillator_driver.py"}
    if set(paths) & reserved_names:
        raise ProtocolError("candidate cannot replace the broker client or wrapper")
    source_hashes = {name: file_hash(path) for name, path in paths.items()}
    started = time.monotonic()
    execution_error = None
    with tempfile.TemporaryDirectory(prefix="popper-oscillator-worker-") as directory:
        workspace = Path(directory)
        if destination.is_relative_to(workspace):
            raise ProtocolError("host receipts must be outside candidate writable scope")
        code = workspace / "code"
        code.mkdir()
        for name, path in paths.items():
            if portable_path(name) != name:
                raise ProtocolError("candidate code path must be a canonical portable relative path")
            relative = Path(name)
            target = code / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        for name in reserved_names:
            shutil.copyfile(source_root / name, code / name)
        write_json(workspace / "instances.json", rows)
        write_json(workspace / "config.json", config)
        command = [sys.executable, str(code / "oscillator_driver.py"),
                   str(code / candidate.name), str(workspace / "instances.json"),
                   str(workspace / "config.json")]
        if trace_execution:
            from .execution import PROBE_SOURCE
            (workspace / "probe.py").write_text(PROBE_SOURCE, encoding="utf-8")
            command = [sys.executable, str(workspace / "probe.py"), str(code),
                       "oscillator_driver.py", str(workspace / "trace.json"), *command[2:]]
        broker = WindowsRHSBroker(job_id, rows, timeout=timeout + 10)
        sandbox.label_write_scope(workspace)
        environment = minimal_environment()
        environment.update({"POPPER_RHS_PIPE": broker.address,
                            "POPPER_RHS_CAPABILITY": broker.session.capability,
                            "POPPER_RHS_JOB": job_id, "PYTHONDONTWRITEBYTECODE": "1",
                            "TMP": str(workspace), "TEMP": str(workspace)})
        try:
            with sandbox.sealed_reads(protected_files):
                with (destination / "stdout.log").open("wb") as out, (destination / "stderr.log").open("wb") as err:
                    try:
                        sandbox.launch(command, cwd=str(code), env=environment,
                                       stdout=out, stderr=err, timeout_seconds=timeout,
                                       cpu_time_seconds=timeout, mem_limit_mb=256,
                                       max_processes=2, sandboxed=True, on_spawn=on_spawn)
                    except (subprocess.SubprocessError, OSError) as error:
                        execution_error = f"{type(error).__name__}: candidate process failed"
                        broker.session.fail(execution_error)
            broker.close()
        finally:
            broker.close()
            sandbox.unlabel_write_scope(workspace)
        receipt = broker.session.receipt()
        if trace_execution:
            trace = workspace / "trace.json"
            if trace.is_file() and trace.stat().st_size <= 2_000_000:
                shutil.copyfile(trace, destination / "execution-trace.json")
    if source_hashes != {name: file_hash(path) for name, path in paths.items()}:
        raise ProtocolError("candidate source changed during execution")
    receipt.update({"kind": "host_metered_rhs_execution", "source_sha256": source_hashes,
                    "config_sha256": digest(config), "elapsed_seconds": time.monotonic() - started,
                    "execution_error": execution_error, "read_isolation": probe,
                    "boundary": "windows_low_integrity_named_pipe",
                    "network_block": sandbox.netblock_available(),
                    "wrapper_sha256": file_hash(source_root / "oscillator_driver.py"),
                    "client_sha256": file_hash(source_root / "oscillator_client.py"),
                    "broker_sha256": file_hash(source_root / "oscillator_broker.py")})
    write_json(destination / "receipt.json", receipt)
    write_json(destination / "trajectories.json", receipt["payload"])
    write_json(destination / "receipt-digest.json", {"sha256": file_hash(destination / "receipt.json")})
    return receipt
