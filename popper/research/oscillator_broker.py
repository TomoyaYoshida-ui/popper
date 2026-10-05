"""Windows-only, host-owned RHS query service (JSON, never pickle).

Only the candidate-specific named pipe is labelled Low. References and host
receipts are not passed to this service's client. No filesystem API is exposed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hmac
import json
import math
from multiprocessing.connection import Listener
import os
import secrets
import threading
import time

from ..core import ProtocolError, canonical, digest, number
from ..domains.oscillator_trajectory import PACK

MAX_MESSAGE = 16384


def _label_pipe_low(handle):
    """Default pipe DACL restricts clients; lower mandatory label for Low IL."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    get_sacl = advapi.GetSecurityDescriptorSacl
    get_sacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    get_sacl.restype = wintypes.BOOL
    set_info = advapi.SetSecurityInfo
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    set_info.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    sd, sacl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    if not convert("S:(ML;;NW;;;LW)", 1, ctypes.byref(sd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not get_sacl(sd, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Named pipes are kernel objects. LABEL_SECURITY_INFORMATION only.
        error = set_info(wintypes.HANDLE(handle), 6, 0x10, None, None, None, sacl)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(sd)


class RHSQuerySession:
    """Pure protocol state: one Job, strict sequence, one active case."""
    def __init__(self, job_id, rows, capability=None):
        PACK.validate_rows(rows, "inputs")
        self.job_id = job_id
        self.rows = {row["id"]: row for row in rows}
        self.capability = capability or secrets.token_hex(24)
        self.sequence = 0
        self.active = None
        self.records = {}
        self.trajectories = {}
        self.chain = "0" * 64
        self.error = None

    def fail(self, reason):
        self.error = reason
        if self.active is not None:
            self.records[self.active]["status"] = "failed"
            self.trajectories[self.active] = None

    def request(self, message):
        if self.error:
            raise ProtocolError("RHS session already failed")
        try:
            result = self._request(message)
            self.chain = digest({"previous": self.chain, "request": {
                k: v for k, v in message.items() if k != "capability"}, "response": result})
            self.sequence += 1
            return result
        except Exception as error:
            self.fail(f"{type(error).__name__}: {error}")
            raise

    def _request(self, message):
        if (not isinstance(message, dict) or
                not isinstance(message.get("capability"), str) or
                not hmac.compare_digest(message["capability"], self.capability) or
                message.get("job_id") != self.job_id or
                type(message.get("seq")) is not int or message["seq"] != self.sequence):
            raise ProtocolError("invalid RHS capability, Job or sequence")
        base = {"capability", "job_id", "seq", "kind", "case_id"}
        kind, identity = message.get("kind"), message.get("case_id")
        if not isinstance(identity, str) or identity not in self.rows:
            raise ProtocolError("case does not belong to this Job")
        row = self.rows[identity]
        if kind == "begin":
            if set(message) != base or self.active is not None or identity in self.records:
                raise ProtocolError("case already started or another case is active")
            self.active = identity
            self.records[identity] = {"case_id": identity, "query_count": 0,
                                      "budget": row["budget"], "status": "running"}
            return {"ok": True}
        if self.active != identity:
            raise ProtocolError("case is not active")
        if kind == "rhs":
            if set(message) != base | {"t", "state"}:
                raise ProtocolError("RHS accepts only time and phase-space state")
            t, state = message["t"], message["state"]
            if (not number(t) or not 0 <= t <= row["t_end"] + 1e-9 or
                    not isinstance(state, list) or len(state) != 2 or
                    any(not number(x) or abs(x) > 1e100 for x in state)):
                raise ProtocolError("invalid RHS input")
            record = self.records[identity]
            if record["query_count"] >= row["budget"]:
                raise ProtocolError("RHS query budget exhausted")
            record["query_count"] += 1
            q, v = state
            return {"value": [v, -row["omega"] ** 2 * (q if row["system"] == "harmonic" else math.sin(q))]}
        if kind == "finish":
            if set(message) != base | {"states"}:
                raise ProtocolError("finish accepts trajectories only, no candidate costs")
            output = {"trajectories": [{"id": identity, "states": message["states"]}]}
            PACK.validate_predictions([row], output, "trajectory_error")
            self.trajectories[identity] = message["states"]
            self.records[identity]["status"] = "succeeded" if message["states"] is not None else "failed"
            self.active = None
            return {"ok": True}
        raise ProtocolError("unknown RHS operation")

    def receipt(self):
        tracks, records = [], []
        for identity, row in self.rows.items():
            record = self.records.get(identity, {"case_id": identity, "query_count": 0,
                                                "budget": row["budget"], "status": "not_completed"})
            complete = record["status"] == "succeeded"
            tracks.append({"id": identity, "states": self.trajectories.get(identity) if complete else None})
            records.append(dict(record))
        return {"job_id": self.job_id, "input_sha256": digest(list(self.rows.values())),
                "query_contract": "host RHS queries, not total arithmetic operations",
                "cases": records, "query_chain_sha256": self.chain, "error": self.error,
                "payload": {"trajectories": tracks}}


class WindowsRHSBroker:
    def __init__(self, job_id, rows, timeout=30):
        if os.name != "nt":
            raise ProtocolError("Windows named-pipe broker required; no TCP fallback")
        if not 0 < timeout <= 3600:
            raise ProtocolError("invalid RHS deadline")
        self.session = RHSQuerySession(job_id, rows)
        self.address = r"\\.\pipe\popper-rhs-" + secrets.token_hex(16)
        self.listener = Listener(self.address, family="AF_PIPE", authkey=None)
        implementation = self.listener._listener
        if not hasattr(implementation, "_handle_queue") or len(implementation._handle_queue) != 1:
            self.listener.close()
            raise ProtocolError("unsupported Python named-pipe implementation")
        self.accept_handle = implementation._handle_queue[0]
        try:
            _label_pipe_low(self.accept_handle)
        except Exception:
            self.listener.close()
            raise
        self.deadline = time.monotonic() + timeout
        self.connection = None
        self.done = threading.Event()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _accept(self):
        # PipeListener.accept waits indefinitely. CancelIoEx from close can run
        # before ConnectNamedPipe has registered any I/O, losing cancellation.
        # Own the overlapped operation and observe shutdown/deadline in its
        # thread, including before starting it. Only one client is served.
        import _winapi
        from multiprocessing.connection import PipeConnection
        handle = self.listener._listener._handle_queue.pop(0)
        transferred = False
        operation = None
        try:
            if self.stopping.is_set():
                raise ProtocolError("RHS broker cancelled before connection")
            operation = _winapi.ConnectNamedPipe(handle, overlapped=True)
            while True:
                if self.stopping.is_set() or time.monotonic() >= self.deadline:
                    raise TimeoutError("RHS connection cancelled or deadline exhausted")
                if _winapi.WaitForMultipleObjects([operation.event], False, 50) == 0:
                    _, error = operation.GetOverlappedResult(True)
                    if error:
                        raise ctypes.WinError(error)
                    transferred = True
                    return PipeConnection(handle)
        finally:
            if not transferred:
                if operation is not None:
                    operation.cancel()
                    operation.GetOverlappedResult(True)
                _winapi.CloseHandle(handle)

    def _serve(self):
        try:
            self.connection = self._accept()
            while time.monotonic() < self.deadline:
                remaining = min(2, self.deadline - time.monotonic())
                if not self.connection.poll(remaining):
                    continue
                message = json.loads(self.connection.recv_bytes(MAX_MESSAGE).decode("utf-8"))
                try:
                    result = self.session.request(message)
                except Exception as error:
                    self.connection.send_bytes(canonical({"error": str(error)}).encode("utf-8"))
                    raise
                self.connection.send_bytes(canonical(result).encode("utf-8"))
                if len(self.session.trajectories) == len(self.session.rows):
                    return
            raise TimeoutError("RHS Job deadline exhausted")
        except Exception as error:
            self.session.fail(f"{type(error).__name__}: {error}")
        finally:
            if self.connection is not None:
                self.connection.close()
            self.listener.close()
            self.done.set()

    def close(self):
        if not self.done.is_set():
            self.stopping.set()
            self.deadline = time.monotonic()
            self.thread.join(timeout=3)
        if not self.done.is_set():
            self.session.fail("broker termination not acknowledged")
            raise ProtocolError("RHS broker did not terminate")
