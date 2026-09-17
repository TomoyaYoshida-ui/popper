"""Independent, hash-bound scoring service for development evaluations."""
from __future__ import annotations

import argparse
import math
import re
import statistics
import subprocess
import sys
from pathlib import Path

from ..core import (EVALUATORS, ProtocolError, canonical, dataset, digest, evaluator_metric,
                    evaluator_pack, file_hash, read_json, score, write_json)


SERVICE_VERSION = "research-evaluator-v3"


def scoring_code_hash():
    """Bind the scoring implementation and this service source."""
    return digest({"core.py": file_hash(Path(__file__).resolve().parents[1] / "core.py"),
                   "evaluation_service.py": file_hash(Path(__file__))})


def _request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ProtocolError("request_id must be a safe single-component identifier")


def _prediction_manifest(predictions, expected_seeds):
    if (not isinstance(expected_seeds, list) or not expected_seeds
            or any(type(seed) is not int for seed in expected_seeds)
            or len(set(expected_seeds)) != len(expected_seeds)):
        raise ProtocolError("expected_seeds must be a unique nonempty integer list")
    if not isinstance(predictions, list):
        raise ProtocolError("predictions must be a list")
    by_seed = {}
    for item in predictions:
        if (not isinstance(item, dict) or set(item) != {"seed", "path", "sha256"}
                or type(item["seed"]) is not int or item["seed"] in by_seed
                or not isinstance(item["path"], str) or not item["path"]
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ProtocolError("prediction manifest is malformed or has duplicate seeds")
        by_seed[item["seed"]] = item
    if set(by_seed) != set(expected_seeds):
        raise ProtocolError("prediction seeds do not match expected_seeds")
    return [by_seed[seed] for seed in expected_seeds]


def _analysis_slices(value, evaluator_id):
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ProtocolError("analysis_slices must contain 1-16 registered slices")
    seen = set()
    normalized = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"slice_id", "rule"}
                or not isinstance(item["slice_id"], str)
                or not re.fullmatch(r"s[0-9]{1,2}", item["slice_id"])
                or item["slice_id"] in seen or not isinstance(item["rule"], dict)):
            raise ProtocolError("analysis slice identity or schema is invalid")
        seen.add(item["slice_id"])
        rule = item["rule"]
        kind, value = rule.get("kind"), rule.get("value")
        if kind in {"id_prefix", "not_id_prefix"}:
            valid = (set(rule) == {"kind", "value"} and isinstance(value, str)
                     and 0 < len(value) <= 32)
        elif kind in {"abs_x_le", "abs_x_gt"}:
            valid = (set(rule) == {"kind", "value"} and evaluator_id == "mse-v1"
                     and type(value) in (int, float) and math.isfinite(value) and value >= 0)
        else:
            valid = False
        if not valid:
            raise ProtocolError("analysis slice rule is invalid for this evaluator")
        normalized.append(item)
    return normalized


def _slice_rows(rows, rule):
    kind, value = rule["kind"], rule["value"]
    if kind == "id_prefix":
        selected = [row for row in rows if row["id"].startswith(value)]
    elif kind == "not_id_prefix":
        selected = [row for row in rows if not row["id"].startswith(value)]
    elif kind == "abs_x_le":
        selected = [row for row in rows if abs(row["x"]) <= value]
    elif kind == "abs_x_gt":
        selected = [row for row in rows if abs(row["x"]) > value]
    else:
        raise ProtocolError("unknown analysis slice rule")
    if not selected:
        raise ProtocolError("registered analysis slice selected no rows")
    return selected


def _slice_score(rows, predictions, evaluator_id):
    ids = {row["id"] for row in rows}
    return score(rows, [item for item in predictions if item["id"] in ids], evaluator_id)


def _validate_points(points, seeds, spec, label):
    if (not isinstance(points, list) or len(points) != len(seeds)
            or any(not isinstance(point, dict) or set(point) != {"seed", "value"}
                   or type(point["seed"]) is not int for point in points)
            or [point["seed"] for point in points] != seeds):
        raise ProtocolError(f"{label} does not contain every preregistered seed")
    values = [point["value"] for point in points]
    for value in values:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ProtocolError(f"{label} contains a non-finite score")
        # 值域来自指标契约本身，新指标不必再改这段校验代码。
        if not spec.accepts(value):
            raise ProtocolError(
                f"{label} score is outside the declared metric domain ({spec.range_text()})")
    return values


def _validate_response(response, request):
    expected = {"schema_version": request["schema_version"],
                "request_id": request["request_id"], "request_sha256": digest(request),
                "evaluator_id": request["evaluator_id"],
                "evaluator_hash": request["evaluator_hash"],
                "scoring_code_sha256": request["scoring_code_sha256"],
                "service_version": SERVICE_VERSION,
                "service_sha256": file_hash(Path(__file__)),
                "trust": "separate_process_same_account"}
    fields = set(expected) | {"per_seed", "mean", "std"}
    if request["schema_version"] == "3.0":
        fields.add("slices")
    if not isinstance(response, dict) or set(response) != fields:
        raise ProtocolError("evaluation response has missing or unknown fields")
    for name, value in expected.items():
        if response[name] != value:
            raise ProtocolError(f"evaluation response identity mismatch: {name}")
    spec = evaluator_metric(request["evaluator_id"])
    values = _validate_points(response["per_seed"], request["expected_seeds"],
                              spec, "global scores")
    if (type(response["mean"]) not in (int, float)
            or type(response["std"]) not in (int, float)
            or not math.isfinite(response["mean"]) or not math.isfinite(response["std"])
            or response["std"] < 0):
        raise ProtocolError("evaluation summary must be finite with nonnegative std")
    expected_stats = (statistics.mean(values),
                      statistics.stdev(values) if len(values) > 1 else 0.0)
    if (not math.isclose(response["mean"], expected_stats[0], rel_tol=1e-12, abs_tol=1e-12)
            or not math.isclose(response["std"], expected_stats[1], rel_tol=1e-12,
                                abs_tol=1e-12)):
        raise ProtocolError("evaluation global summary is inconsistent with per-seed scores")
    if request["schema_version"] == "3.0":
        registered = _analysis_slices(request["analysis_slices"], request["evaluator_id"])
        slices = response["slices"]
        if (not isinstance(slices, list) or len(slices) != len(registered)
                or [item.get("slice_id") for item in slices]
                != [item["slice_id"] for item in registered]):
            raise ProtocolError("evaluation response slices do not match registration")
        for item in slices:
            if (not isinstance(item, dict)
                    or set(item) != {"slice_id", "n", "per_seed", "mean", "std"}
                    or type(item["n"]) is not int or item["n"] < 1):
                raise ProtocolError("evaluation response slice schema is invalid")
            slice_values = _validate_points(item["per_seed"], request["expected_seeds"],
                                            spec, "slice scores")
            expected_slice = (statistics.mean(slice_values),
                              statistics.stdev(slice_values) if len(slice_values) > 1 else 0.0)
            if (type(item["mean"]) not in (int, float)
                    or type(item["std"]) not in (int, float)
                    or not math.isclose(item["mean"], expected_slice[0], rel_tol=1e-12,
                                        abs_tol=1e-12)
                    or not math.isclose(item["std"], expected_slice[1], rel_tol=1e-12,
                                        abs_tol=1e-12)):
                raise ProtocolError("evaluation slice summary is inconsistent")


def evaluate_request(request):
    required = {"schema_version", "request_id", "dataset", "dataset_sha256",
                "evaluator_id", "evaluator_hash", "predictions", "expected_seeds",
                "scoring_code_sha256"}
    if not isinstance(request, dict) or request.get("schema_version") not in {"2.0", "3.0"}:
        raise ProtocolError("evaluation request version is unsupported")
    if request["schema_version"] == "3.0":
        required.add("analysis_slices")
    if set(request) != required:
        raise ProtocolError("evaluation request has missing or unknown fields")
    _request_id(request["request_id"])
    predictions = _prediction_manifest(request["predictions"], request["expected_seeds"])
    if request["scoring_code_sha256"] != scoring_code_hash():
        raise ProtocolError("scoring implementation hash mismatch")
    evaluator_id = request["evaluator_id"]
    if evaluator_id not in EVALUATORS or digest(EVALUATORS[evaluator_id]) != request["evaluator_hash"]:
        raise ProtocolError("evaluator identity mismatch")
    data_path = Path(request["dataset"])
    if not data_path.is_file() or file_hash(data_path) != request["dataset_sha256"]:
        raise ProtocolError("evaluation dataset is missing or changed")
    rows = dataset(data_path, evaluator_id)
    slices = (_analysis_slices(request["analysis_slices"], evaluator_id)
              if request["schema_version"] == "3.0" else [])
    selected_rows = {item["slice_id"]: _slice_rows(rows, item["rule"]) for item in slices}
    points = []
    slice_points = {item["slice_id"]: [] for item in slices}
    for item in predictions:
        path = Path(item["path"])
        if not path.is_file() or file_hash(path) != item["sha256"]:
            raise ProtocolError("预测制品缺失或摘要变化")
        prediction_rows = read_json(path)
        points.append({"seed": item["seed"], "value": score(rows, prediction_rows, evaluator_id)})
        for registered in slices:
            slice_id = registered["slice_id"]
            slice_points[slice_id].append({
                "seed": item["seed"],
                "value": _slice_score(selected_rows[slice_id], prediction_rows, evaluator_id),
            })
    values = [point["value"] for point in points]
    response = {"schema_version": request["schema_version"],
                "request_id": request["request_id"], "request_sha256": digest(request),
                "evaluator_id": evaluator_id, "evaluator_hash": request["evaluator_hash"],
                "per_seed": points, "scoring_code_sha256": request["scoring_code_sha256"],
                "mean": statistics.mean(values),
                "std": statistics.stdev(values) if len(values) > 1 else 0.0,
                "service_version": SERVICE_VERSION,
                "service_sha256": file_hash(Path(__file__)),
                "trust": "separate_process_same_account"}
    if slices:
        response["slices"] = []
        for registered in slices:
            slice_id = registered["slice_id"]
            per_seed = slice_points[slice_id]
            raw = [point["value"] for point in per_seed]
            response["slices"].append({
                "slice_id": slice_id, "n": len(selected_rows[slice_id]), "per_seed": per_seed,
                "mean": statistics.mean(raw),
                "std": statistics.stdev(raw) if len(raw) > 1 else 0.0,
            })
    return response


class IndependentEvaluator:
    def __init__(self, project, output_dir):
        self.project = Path(project).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def score_core_result(self, state, result):
        _request_id(result["run_id"])
        run_dir = self.project / ".popper" / "runs" / result["run_id"]
        invocation = evaluator_pack(state["evaluator_id"]).invocation()
        predictions = [{"seed": point["seed"],
                        "path": str(run_dir / invocation.prediction_name(point["seed"])),
                        "sha256": file_hash(run_dir / invocation.prediction_name(point["seed"]))}
                       for point in result["per_seed"]]
        return self.score_artifacts(state, result["split"], result["run_id"], predictions)

    def score_artifacts(self, state, split, request_id, predictions):
        _request_id(request_id)
        if split not in {"dev", "test"}:
            raise ProtocolError("independent evaluator only accepts dev/test")
        predictions = _prediction_manifest(predictions, state["spec"]["seeds"])
        data_path = (self.project / state["spec"][split]).resolve()
        expected_hash = state["input_hashes"][state["spec"][split]]
        if not data_path.is_relative_to(self.project) or file_hash(data_path) != expected_hash:
            raise ProtocolError("evaluation dataset differs from preregistration")
        for item in predictions:
            if not Path(item["path"]).is_file() or file_hash(item["path"]) != item["sha256"]:
                raise ProtocolError("预测制品缺失或摘要变化")
        slices = state["spec"].get("analysis_slices", [])
        request = {"schema_version": "3.0" if slices else "2.0", "request_id": request_id,
                   "dataset": str(data_path), "dataset_sha256": expected_hash,
                   "evaluator_id": state["evaluator_id"],
                   "evaluator_hash": state["evaluator_hash"],
                   "predictions": predictions, "expected_seeds": state["spec"]["seeds"],
                   "scoring_code_sha256": scoring_code_hash()}
        if slices:
            request["analysis_slices"] = slices
        target = self.output_dir / request_id
        target.mkdir(parents=True, exist_ok=True)
        request_path, response_path = target / "request.json", target / "response.json"
        if request_path.exists() and read_json(request_path) != request:
            raise ProtocolError("request_id cannot overwrite a different evaluation")
        if response_path.exists():
            if not request_path.is_file():
                raise ProtocolError("evaluation response lacks its source request")
            response = read_json(response_path)
            _validate_response(response, request)
            response["artifact_id"] = str(response_path)
            response["artifact_sha256"] = file_hash(response_path)
            return response
        try:
            with request_path.open("x", encoding="utf-8") as stream:
                stream.write(canonical(request))
        except FileExistsError:
            if read_json(request_path) != request:
                raise ProtocolError("request_id cannot overwrite a different evaluation")
        completed = subprocess.run(
            [sys.executable, "-c",
             "from popper.research.evaluation_service import main; raise SystemExit(main())",
             "--request", str(request_path), "--response", str(response_path)],
            cwd=str(Path(__file__).resolve().parents[2]), capture_output=True,
            text=True, timeout=120, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode != 0:
            raise ProtocolError(f"independent evaluator failed: {completed.stderr[-500:]}")
        response = read_json(response_path)
        _validate_response(response, request)
        response["artifact_id"] = str(response_path)
        response["artifact_sha256"] = file_hash(response_path)
        return response


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        write_json(args.response, evaluate_request(read_json(args.request)))
        return 0
    except (ProtocolError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"evaluation_service: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
