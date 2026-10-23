"""不联网的树搜索提案器：先交一次会失败的实现，再交线性拟合。

分数仍由独立评估器计算。没有模型端点时，`popper search` 用它把开发循环跑完。
这只是测试/离线探针，不能当成「agent 做了研究」的演示。
"""
from __future__ import annotations


_FAILING = '''"""Search probe that fails while predicting."""
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
    if not train:
        raise RuntimeError("empty train")
    raise RuntimeError("search probe stops before writing predictions")


if __name__ == "__main__":
    main()
'''

_LINEAR = '''"""One-feature least squares. The frozen config is loaded and not rewritten."""
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
    count = len(train)
    sum_x = sum(row["x"] for row in train)
    sum_y = sum(row["y"] for row in train)
    sum_xx = sum(row["x"] * row["x"] for row in train)
    sum_xy = sum(row["x"] * row["y"] for row in train)
    denom = count * sum_xx - sum_x * sum_x
    slope = 0.0 if abs(denom) < 1e-12 else (count * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / count
    predictions = [{"id": row["id"], "prediction": intercept + slope * row["x"]}
                   for row in load(args.input)]
    Path(args.output).write_text(json.dumps(predictions, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
'''


class LocalSearchHarness:
    name = "local_search"
    model = None

    def __init__(self):
        self.calls = 0

    @property
    def tokens_used(self):
        return 0

    def propose_revision(self, objective, hypothesis, config, code_files,
                         parent_revision=None, failure=None):
        from ..research.revisions import CodeEdit
        row = code_files[0]
        self.calls += 1
        replacement = _FAILING if self.calls == 1 else _LINEAR
        rationale = ("第一次提案在预测前失败，留给树搜索修。" if self.calls == 1
                     else "用单特征最小二乘替换均值基线。")
        return {"edits": (CodeEdit(row["path"], replacement, row["sha256"]),),
                "rationale": rationale}
