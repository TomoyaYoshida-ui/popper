"""Offline preparation for an Agent study, NOT a model capability benchmark.

Run: python -m popper.research.oscillator_study prepare <new-directory>
Reference construction requires SciPy. Scoring remains standard-library only.
No model calls, Study execution, or confirmation are performed by this entry.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
from pathlib import Path

from ..core import ProtocolError, digest, file_hash, read_json, write_json
from ..domains.oscillator_trajectory import PACK


class EvaluationBudgetExceeded(ProtocolError):
    pass


class MeteredRHS:
    """Trusted-host counter for offline baseline/reference work only.

    Do not pass this Python object into untrusted code and claim tamper-proof
    metering. The candidate broker must own counters in a different process.
    """
    def __init__(self, row):
        self.row = row
        self.calls = 0

    def __call__(self, t, state):
        if self.calls >= self.row["budget"]:
            raise EvaluationBudgetExceeded("function evaluation budget exhausted")
        self.calls += 1
        q, v = state
        force = q if self.row["system"] == "harmonic" else math.sin(q)
        return (v, -self.row["omega"] ** 2 * force)


def baseline_rk4(row):
    """Fixed, trusted baseline; every RHS request counted including final steps."""
    intervals = len(row["times"]) - 1
    steps_per_interval = row["budget"] // (4 * intervals)
    if steps_per_interval < 1:
        raise ProtocolError("budget too small for baseline sample schedule")
    rhs = MeteredRHS(row)
    state = [row["q0"], row["v0"]]
    states = [state.copy()]
    for start, stop in zip(row["times"], row["times"][1:]):
        h = (stop - start) / steps_per_interval
        for step in range(steps_per_interval):
            t = start + step * h
            a = rhs(t, state)
            b = rhs(t + h / 2, [x + h * k / 2 for x, k in zip(state, a)])
            c = rhs(t + h / 2, [x + h * k / 2 for x, k in zip(state, b)])
            d = rhs(t + h, [x + h * k for x, k in zip(state, c)])
            state = [x + h * (ka + 2 * kb + 2 * kc + kd) / 6
                     for x, ka, kb, kc, kd in zip(state, a, b, c, d)]
            if any(not math.isfinite(x) or abs(x) > 1e100 for x in state):
                return None, rhs.calls
        states.append(state.copy())
    return states, rhs.calls


def reference(row):
    if row["system"] == "harmonic":
        w, q, v = row["omega"], row["q0"], row["v0"]
        states = [[q * math.cos(w * t) + v / w * math.sin(w * t),
                   -q * w * math.sin(w * t) + v * math.cos(w * t)]
                  for t in row["times"]]
        return states, {"methods": ["analytic"], "normalized_disagreement": 0.0}
    from scipy.integrate import solve_ivp

    def rhs(t, y):
        return [y[1], -row["omega"] ** 2 * math.sin(y[0])]

    outputs = []
    counts = []
    # Distinct numerical schemes plus a tightened DOP853 convergence check.
    for method, tolerance in (("DOP853", 8e-14), ("RK45", 3e-14), ("DOP853", 2.3e-14)):
        result = solve_ivp(rhs, (0, row["t_end"]), [row["q0"], row["v0"]],
                           method=method, rtol=tolerance, atol=tolerance / 10,
                           t_eval=row["times"])
        if not result.success or result.y.shape[1] != len(row["times"]):
            raise ProtocolError("reference integrator failed")
        outputs.append(result.y.T.tolist())
        counts.append(int(result.nfev))
    scale = math.sqrt(row["q0"] ** 2 + (row["v0"] / row["omega"]) ** 2)
    disagreement = max(math.hypot(a[0] - b[0], (a[1] - b[1]) / row["omega"]) / scale
                       for earlier in outputs[:2] for a, b in zip(earlier, outputs[2]))
    if not math.isfinite(disagreement) or disagreement > 1e-8:
        raise ProtocolError(f"reference agreement failed for {row['id']}: {disagreement:.6g} > 1e-8")
    return outputs[2], {"methods": ["DOP853", "RK45", "DOP853_tightened"],
                        "normalized_disagreement": disagreement, "nfev": counts}


def cases(split):
    parameters = {"train": ((1.0, 3.0), (80.0, 200.0)),
                  "dev": ((1.4, 3.6), (100.0, 300.0)),
                  "test": ((1.8, 4.2), (140.0, 420.0))}
    frequencies, horizons = parameters[split]
    rows = []
    for system in ("harmonic", "pendulum"):
        for frequency in frequencies:
            for amplitude in (0.3, 1.2):
                for horizon in horizons:
                    for budget in (4096, 16384):
                        index = len(rows)
                        rows.append({"id": f"{split}-{system}-{index}", "system": system,
                                     "omega": frequency, "q0": amplitude,
                                     "v0": 0.0, "t_end": horizon, "budget": budget,
                                     "times": [horizon * i / 64 for i in range(65)]})
    return rows


def preflight():
    from .. import sandbox
    capabilities = {"sandbox": sandbox.available(), "sealed_reads": sandbox.seal_read_available(),
                    "network_block": sandbox.netblock_available()}
    # Capability flags alone are not end-to-end isolation tests.
    return {"capabilities": capabilities, "formal_run_ready": False,
            "blockers": ["candidate RHS broker and retained trusted cost receipts not implemented",
                         "conversation Revision proposal/execution adapter not validated",
                         "end-to-end reference/holdout read-denial probes not completed"],
            "model_requests": 0, "confirmation_executions": 0}


def prepare(destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ProtocolError("preparation requires a new directory; existing outputs are never overwritten")
    destination.mkdir(parents=True)
    private = destination / "evaluator-private"
    public = destination / "agent-visible"
    private.mkdir()
    public.mkdir()
    outputs, checks, baseline_tracks, counts, reference_cache = {}, {}, [], [], {}
    for split in ("train", "dev", "test"):
        rows = cases(split)
        for row in rows:
            key = digest({k: v for k, v in row.items() if k not in {"id", "budget"}})
            if key not in reference_cache:
                reference_cache[key] = reference(row)
            row["reference"], checks[row["id"]] = reference_cache[key]
            if split == "dev":
                track, count = baseline_rk4(row)
                baseline_tracks.append({"id": row["id"], "states": track})
                counts.append({"id": row["id"], "function_evaluations": count,
                               "budget": row["budget"], "trust": "fixed_host_baseline_only"})
        PACK.validate_rows(rows, split)
        write_json(private / f"{split}.json", rows)
        outputs[split] = rows
        if split != "test":
            write_json(public / f"{split}.json", PACK.project_inputs(rows))
    PACK.validate_splits(outputs)
    payload = {"trajectories": baseline_tracks}
    write_json(private / "reference-checks.json", checks)
    write_json(destination / "baseline-development.json", {
        "kind": "offline_baseline_not_agent_evidence", "split": "dev",
        "trajectory_error": PACK.score(outputs["dev"], payload, "trajectory_error"),
        "cases": PACK.diagnostics(outputs["dev"], payload), "costs": counts})
    write_json(private / "baseline-development-trajectories.json", payload)
    protocol = {"kind": "agent_oscillator_study", "version": 1,
                "status": "offline_prepared_not_formal_execution",
                "evaluator": PACK.evaluator_entry(), "arms": ["fixed_rk4", "single_shot", "popper_loop"],
                "model_runs_per_arm": 3, "max_development_executions_per_loop": 6,
                "max_model_requests_total": 60, "max_model_tokens_total": 400000,
                "relative_improvement_threshold": 0.20,
                "required_improved_runs": 2, "failure_rate_must_not_exceed_control": True,
                "candidate_output": "trajectories only; no scores or self-reported costs",
                "normalization": "initial phase-space amplitude; equal weight per case",
                "failed_case_error": 1000000, "confirmation_requires_separate_user_approval": True,
                "test_rows": len(outputs["test"]), "environment": {"python": platform.python_version()},
                "preflight": preflight()}
    import scipy
    protocol["environment"]["scipy"] = scipy.__version__
    paths = [p for p in destination.rglob("*.json") if p.is_file()]
    protocol["artifacts"] = {str(p.relative_to(destination)).replace("\\", "/"): file_hash(p)
                             for p in paths}
    write_json(destination / "protocol.json", protocol)
    write_json(destination / "protocol-receipt.json", {"protocol_sha256": file_hash(destination / "protocol.json")})
    return {"directory": str(destination), "protocol_sha256": file_hash(destination / "protocol.json"),
            "status": protocol["status"], "model_requests": 0, "confirmation_executions": 0}


def verify(destination):
    root = Path(destination).resolve()
    protocol = read_json(root / "protocol.json")
    receipt = read_json(root / "protocol-receipt.json")
    if receipt["protocol_sha256"] != file_hash(root / "protocol.json"):
        raise ProtocolError("frozen protocol changed")
    for name, expected in protocol["artifacts"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or file_hash(path) != expected:
            raise ProtocolError("frozen artifact changed")
    return {"integrity": "ok", "formal_run_ready": False, "model_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify", "preflight"))
    parser.add_argument("directory", nargs="?")
    args = parser.parse_args()
    if args.action != "preflight" and args.directory is None:
        parser.error("directory required")
    result = preflight() if args.action == "preflight" else (
        prepare(args.directory) if args.action == "prepare" else verify(args.directory))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
