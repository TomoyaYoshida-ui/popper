"""处理效应估计试点入口（Popper staged_artifacts + 分析单元试点工程）。

候选收到当前划分的全队列（``cohort.json``，已剥离真实效应 tau）与区块 id
（``--seed`` 槽承载分析单元取值），只在该区块内估计平均处理效应（ATE），写出
``{"ate_estimate": ...}``；区块真值由域包用控制器持有的 tau 计算，候选自报
``ate_error`` 会被拒绝。

两种估计器由配置 ``{"estimator": ...}`` 选择，均只用标准库：

- ``dif``：处理组/对照组观测均值差。处理分配与协变量 x 相关时，它把混杂误当效应；
- ``adj``：``y ~ 1 + x + treated`` 的 OLS 协变量调整（3x3 正规方程，高斯消元），
  ``treated`` 系数即调整后 ATE。

本工程只演示「分析单元参与统计 + 非随机整块切分」的接入，不声称计量方法上的创新。
"""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def difference_in_means(rows):
    treated = [row["outcome"] for row in rows if row["treated"] == 1]
    control = [row["outcome"] for row in rows if row["treated"] == 0]
    return sum(treated) / len(treated) - sum(control) / len(control)


def solve_linear(matrix, vector):
    """高斯-若尔当消元解 n 元线性方程组（判定代码规模小，不需要 numpy）。"""
    n = len(vector)
    work = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(work[r][col]))
        if abs(work[pivot][col]) < 1e-12:
            raise ValueError("design matrix is singular")
        work[col], work[pivot] = work[pivot], work[col]
        pivot_value = work[col][col]
        work[col] = [value / pivot_value for value in work[col]]
        for row in range(n):
            if row == col:
                continue
            factor = work[row][col]
            work[row] = [value - factor * work[col][j] for j, value in enumerate(work[row])]
    return [work[i][n] for i in range(n)]


def ols_adjusted(rows):
    """y = b0 + b1*x + b2*treated 的 OLS；b2 为协变量调整后的 ATE 估计。"""
    columns = [(1.0, row["x"], float(row["treated"])) for row in rows]
    outcomes = [row["outcome"] for row in rows]
    xtx = [[sum(a[j] * a[k] for a in columns) for k in range(3)] for j in range(3)]
    xty = [sum(a[j] * y for a, y in zip(columns, outcomes)) for j in range(3)]
    return solve_linear(xtx, xty)[2]


ESTIMATORS = {"dif": difference_in_means, "adj": ols_adjusted}


def main():
    parser = argparse.ArgumentParser()
    for name in ("cohort", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    estimator = load(args.config)["estimator"]
    if estimator not in ESTIMATORS:
        raise ValueError(f"unknown estimator: {estimator!r}")
    block = int(args.seed)
    rows = [row for row in load(args.cohort) if row["block"] == block]
    if not rows:
        raise ValueError(f"block {block} has no rows in this split")
    estimate = ESTIMATORS[estimator](rows)
    Path(args.output).write_text(
        json.dumps({"ate_estimate": estimate}), encoding="utf-8")


if __name__ == "__main__":
    main()
