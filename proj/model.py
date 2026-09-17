"""Tiny dependency-free least-squares adapter, not a scientific discovery benchmark."""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def solve(matrix, rhs):
    a = [list(row) + [value] for row, value in zip(matrix, rhs)]
    n = len(a)
    for i in range(n):
        pivot = max(range(i, n), key=lambda j: abs(a[j][i]))
        a[i], a[pivot] = a[pivot], a[i]
        scale = a[i][i]
        if abs(scale) < 1e-12:
            raise ValueError("singular training system")
        a[i] = [x / scale for x in a[i]]
        for j in range(n):
            if j != i:
                scale = a[j][i]
                a[j] = [x - scale * y for x, y in zip(a[j], a[i])]
    return [row[-1] for row in a]


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    train = load(args.train)
    degree = load(args.config)["degree"]
    if type(degree) is not int or not 0 <= degree <= 3:
        raise ValueError("degree must be an integer in [0, 3]")
    # This algorithm is deterministic; seeds intentionally do not imply independent samples.
    basis = [[r["x"] ** i for i in range(degree + 1)] for r in train]
    matrix = [[sum(row[i] * row[j] for row in basis) for j in range(degree + 1)] for i in range(degree + 1)]
    rhs = [sum(row[i] * r["y"] for row, r in zip(basis, train)) for i in range(degree + 1)]
    weights = solve(matrix, rhs)
    result = [{"id": r["id"], "prediction": sum(w * r["x"] ** i for i, w in enumerate(weights))}
              for r in load(args.input)]
    Path(args.output).write_text(json.dumps(result, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
