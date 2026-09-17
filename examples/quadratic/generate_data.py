"""Deterministic toy data preparation. Run once, before popper experiment init."""
import json
import random
from pathlib import Path


def generate(root):
    root = Path(root)
    if (root / ".popper").exists():
        raise RuntimeError("Already initialized; do not replace registered data")
    for split, seed, count in [("train", 1729, 60), ("dev", 2718, 30), ("test", 3141, 30)]:
        rng = random.Random(seed)
        rows = []
        for i in range(count):
            x = rng.uniform(-2, 2)
            rows.append({"id": f"{split}-{i}", "x": x, "y": 0.5 + 0.7 * x + 1.8 * x * x + rng.gauss(0, 0.1)})
        (root / f"{split}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    generate(Path(__file__).parent)
