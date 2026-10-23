"""开发集基线：用训练标签的均值预测。不读取确认集。"""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    train = load(args.train)
    load(args.config)
    mean = sum(row["y"] for row in train) / len(train)
    predictions = [{"id": row["id"], "prediction": mean} for row in load(args.input)]
    Path(args.output).write_text(json.dumps(predictions, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
