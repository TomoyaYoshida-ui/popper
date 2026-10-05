"""Evaluator-owned trajectory scoring. No candidate-reported errors or costs.

This pack scores retained trajectories only. It does NOT certify execution or
function-evaluation budgets; those require a trusted execution receipt.
Reference generation lives outside this standard-library-only decision code.
"""
from __future__ import annotations

import math

from ..core import ProtocolError, digest, number
from .protocol import Invocation, MetricSpec, register
from .staged import StagedArtifactsPack


FAILURE_ERROR = 1_000_000.0


def energy(system, omega, q, v):
    potential = q * q / 2 if system == "harmonic" else 1 - math.cos(q)
    return v * v / 2 + omega * omega * potential


class OscillatorTrajectoryV1(StagedArtifactsPack):
    pack_id = "oscillator-trajectory"
    schema_version = "1.0"
    evaluator_id = "oscillator-trajectory-v1"
    _metrics = (
        MetricSpec("trajectory_error", "min", "normalized RMS",
                   "Mean case-wise normalized trajectory RMS error", (0, FAILURE_ERROR)),
    )
    _entry = {
        "id": evaluator_id,
        "metric": {"name": "trajectory_error", "direction": "min"},
        "definition": "mean case-wise sqrt(mean((dq^2+(dv/omega)^2)/(q0^2+(v0/omega)^2))); failed case=1e6; no resource attestation",
        "dataset": "oscillator cases with frozen sample times and evaluator-only reference trajectories",
    }

    def invocation(self):
        return Invocation(
            args=(("--instance", "inputs"), ("--output", "prediction"),
                  ("--config", "config"), ("--seed", "seed")),
            inputs=(("inputs", "instances.json"), ("config", "solver.json")),
            prediction="trajectories-{seed}.json")

    def validate_rows(self, rows, split):
        if not isinstance(rows, list) or not rows:
            raise ProtocolError("oscillator cases must be nonempty")
        seen = set()
        visible = split == "inputs"
        required = {"id", "system", "omega", "q0", "v0", "t_end", "budget", "times"}
        for row in rows:
            if not isinstance(row, dict) or set(row) != required | (set() if visible else {"reference"}):
                raise ProtocolError("invalid oscillator case fields")
            if not isinstance(row["id"], str) or not row["id"] or row["id"] in seen:
                raise ProtocolError("duplicate or invalid case ID")
            seen.add(row["id"])
            if row["system"] not in {"harmonic", "pendulum"}:
                raise ProtocolError("unknown oscillator")
            if any(not number(row[k]) for k in ("omega", "q0", "v0", "t_end")):
                raise ProtocolError("nonfinite oscillator parameter")
            if row["omega"] <= 0 or row["t_end"] <= 0 or type(row["budget"]) is not int or row["budget"] < 256:
                raise ProtocolError("invalid positive parameter or budget")
            if row["q0"] ** 2 + (row["v0"] / row["omega"]) ** 2 < 1e-12:
                raise ProtocolError("zero-amplitude case has undefined normalization")
            if energy(row["system"], row["omega"], row["q0"], row["v0"]) < 1e-12:
                raise ProtocolError("zero-energy case has undefined drift")
            times = row["times"]
            if (not isinstance(times, list) or len(times) < 3 or
                    any(not number(t) for t in times) or times[0] != 0 or
                    times[-1] != row["t_end"] or
                    any(b <= a for a, b in zip(times, times[1:]))):
                raise ProtocolError("invalid frozen sample times")
            if not visible:
                self._states(row["reference"], len(times))
                if row["reference"][0] != [row["q0"], row["v0"]]:
                    raise ProtocolError("reference initial state differs")

    @staticmethod
    def _states(states, count):
        if (not isinstance(states, list) or len(states) != count or
                any(not isinstance(s, list) or len(s) != 2 or
                    any(not number(x) or abs(x) > 1e100 for x in s) for s in states)):
            raise ProtocolError("trajectory must contain a finite [q,v] for every sample")

    def project_inputs(self, rows):
        return [{key: value for key, value in row.items() if key != "reference"} for row in rows]

    def row_identity(self, row):
        return digest({k: v for k, v in row.items() if k not in {"id", "reference"}})

    def validate_predictions(self, rows, payload, metric_id):
        self.metric(metric_id)
        if not isinstance(payload, dict) or set(payload) != {"trajectories"}:
            raise ProtocolError("candidate may submit only trajectories, not scores or costs")
        tracks = payload["trajectories"]
        if not isinstance(tracks, list) or len(tracks) != len(rows):
            raise ProtocolError("every case must be represented, including failures")
        by_id = {row["id"]: row for row in rows}
        seen = set()
        for track in tracks:
            if not isinstance(track, dict) or set(track) != {"id", "states"}:
                raise ProtocolError("invalid trajectory fields")
            identity = track["id"]
            if not isinstance(identity, str) or identity not in by_id or identity in seen:
                raise ProtocolError("unknown or repeated trajectory ID")
            seen.add(identity)
            if track["states"] is not None:
                self._states(track["states"], len(by_id[identity]["times"]))
                if track["states"][0] != [by_id[identity]["q0"], by_id[identity]["v0"]]:
                    raise ProtocolError("candidate initial state differs")

    def diagnostics(self, rows, payload):
        self.validate_predictions(rows, payload, "trajectory_error")
        tracks = {track["id"]: track["states"] for track in payload["trajectories"]}
        results = []
        for row in rows:
            states = tracks[row["id"]]
            if states is None:
                results.append({"id": row["id"], "system": row["system"],
                                "failed": True, "trajectory_error": FAILURE_ERROR,
                                "energy_drift": None})
                continue
            scale = row["q0"] ** 2 + (row["v0"] / row["omega"]) ** 2
            error = math.sqrt(math.fsum(
                ((q - qr) ** 2 + ((v - vr) / row["omega"]) ** 2) / scale
                for (q, v), (qr, vr) in zip(states, row["reference"])) / len(states))
            initial = energy(row["system"], row["omega"], row["q0"], row["v0"])
            drift = max(abs(energy(row["system"], row["omega"], q, v) - initial) / initial
                        for q, v in states)
            results.append({"id": row["id"], "system": row["system"], "failed": False,
                            "trajectory_error": min(error, FAILURE_ERROR), "energy_drift": drift})
        return results

    def score(self, rows, payload, metric_id, unit=None):
        self.metric(metric_id)
        return math.fsum(r["trajectory_error"] for r in self.diagnostics(rows, payload)) / len(rows)


PACK = register(OscillatorTrajectoryV1())
