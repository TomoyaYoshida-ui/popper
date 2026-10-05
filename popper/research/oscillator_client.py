"""Copyable candidate client: no host dynamics, references, counters or scoring.

The host supplies one ephemeral capability for one Job. JSON only, no pickle.
"""
import json
from multiprocessing.connection import Client


class RHSClient:
    def __init__(self, address, capability, job_id, timeout=30):
        self.connection = Client(address, family="AF_PIPE", authkey=None)
        self.capability = capability
        self.job_id = job_id
        self.timeout = timeout
        self.sequence = 0
        self.case_id = None

    def _request(self, kind, **values):
        message = {"capability": self.capability, "job_id": self.job_id,
                   "seq": self.sequence, "kind": kind, "case_id": self.case_id, **values}
        body = json.dumps(message, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 16384:
            raise ValueError("RHS request too large")
        self.connection.send_bytes(body)
        if not self.connection.poll(self.timeout):
            raise TimeoutError("RHS host did not respond")
        result = json.loads(self.connection.recv_bytes(16384))
        if "error" in result:
            raise RuntimeError(result["error"])
        self.sequence += 1
        return result

    def begin(self, case_id):
        self.case_id = case_id
        self._request("begin")

    def rhs(self, t, state):
        return self._request("rhs", t=t, state=list(state))["value"]

    def finish(self, states):
        self._request("finish", states=states)
        self.case_id = None

    def close(self):
        self.connection.close()
