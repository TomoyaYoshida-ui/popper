"""Offline verification of frozen protocol, result summary and saved fold predictions."""
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def same(left, right):
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-15)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    result = json.loads((ROOT / "results.json").read_text(encoding="utf-8"))
    paper = json.loads((ROOT / "paper.json").read_text(encoding="utf-8"))
    protocol = json.loads((ROOT / "protocol.json").read_text(encoding="utf-8"))
    if result["protocol_sha256"] != digest(ROOT / "protocol.json"):
        raise ValueError("protocol changed after execution")
    if result["paper_sha256"] != digest(ROOT / "paper.json"):
        raise ValueError("paper claim record changed after execution")
    if result["dataset"]["sha256"] != digest(ROOT / result["dataset"]["path"]):
        raise ValueError("dataset changed after execution")
    grouped = defaultdict(list)
    by_fold = defaultdict(list)
    appearances = defaultdict(lambda: defaultdict(int))
    with (ROOT / "predictions.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            expected = int(int(row["truth"]) == int(row["prediction"]))
            if expected != int(row["correct"]):
                raise ValueError("prediction correctness column is inconsistent")
            grouped[(row["evaluation"], row["method"], int(row["repeat"]))].append(expected)
            by_fold[(row["evaluation"], row["method"], int(row["repeat"]), int(row["fold"]))].append(expected)
            appearances[(row["evaluation"], row["method"])][row["id"]] += 1
    primary_name = protocol["primary"]["name"]
    primary_accuracy = sum(grouped[("primary", primary_name, 0)]) / len(grouped[("primary", primary_name, 0)])
    if not same(primary_accuracy, result["primary_reproduced_accuracy"]):
        raise ValueError("primary accuracy does not recompute")
    for summary in result["primary_cv"]:
        method = summary["method"]
        values = grouped[("primary", method, 0)]
        folds = [statistics.mean(by_fold[("primary", method, 0, fold)]) for fold in range(10)]
        if (not same(statistics.mean(values), summary["accuracy"])
                or not same(statistics.mean(folds), summary["fold_accuracy_mean"])
                or not same(statistics.stdev(folds), summary["fold_accuracy_std"])
                or set(appearances[("primary", method)].values()) != {1}):
            raise ValueError(f"primary summary does not recompute: {method}")
    difference = primary_accuracy - paper["claim"]["reported_value"]
    expected_status = ("close_reproduction" if abs(difference) <= protocol["target_claim"]["absolute_tolerance"]
                       else "not_reproduced_with_surrogate")
    if not same(difference, result["difference"]) or expected_status != result["status"]:
        raise ValueError("claim status does not recompute")
    repeats = [sum(grouped[("robustness", primary_name, repeat)]) / len(grouped[("robustness", primary_name, repeat)])
               for repeat in range(30)]
    if len(set(len(grouped[("robustness", primary_name, repeat)]) for repeat in range(30))) != 1:
        raise ValueError("robustness repeats do not cover equal sample counts")
    if not all(same(actual, saved) for actual, saved in zip(
            repeats, result["robustness_30x10_cv"]["repeat_accuracy"])):
        raise ValueError("robustness repeat accuracies do not recompute")
    robust_values = [value for repeat in range(30)
                     for value in grouped[("robustness", primary_name, repeat)]]
    robust = result["robustness_30x10_cv"]
    if (not same(statistics.mean(robust_values), robust["accuracy"])
            or not same(statistics.mean(repeats), robust["repeat_accuracy_mean"])
            or not same(statistics.stdev(repeats), robust["repeat_accuracy_std"])
            or set(appearances[("robustness", primary_name)].values()) != {30}):
        raise ValueError("robustness summary does not recompute")
    print(json.dumps({"status": "verified", "primary_accuracy": primary_accuracy,
                      "reported_accuracy": paper["claim"]["reported_value"],
                      "difference": difference, "repeats": len(repeats),
                      "method_exactness": result["method_exactness"]}, indent=2))


if __name__ == "__main__":
    main()
