"""显式单步法的收敛阶测量入口（Popper staged_artifacts 试点工程）。

候选按声明的细化表求解同一个初值问题 ``y' = -rate · y``、``y(0) = y0``，只报告
自己测得的**误差序列**（每个网格一个最大绝对误差）；「收敛阶」由域包用
log(误差) 对 log(步数) 的最小二乘拟合得到，候选若在制品里写 ``convergence_order``
会被拒绝。本工程只演示「序列测量 + 非逐样本形状」的接入，不声称数值方法上的创新。

参数由域包声明的调用契约给出：``--instance``（问题实例与细化表）/ ``--output``（测量制品）
/ ``--config``（格式选择）/ ``--seed``（本次独立重复的初始幅值；精确解随之缩放，
收敛阶不受影响，这样各次重复不是同一个常数的重放）。
"""
import argparse
import json
import math
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def exact_solution(time, rate, y0):
    return y0 * math.exp(-rate * time)


def step(scheme, y, h, rate):
    """显式单步：返回下一个网格点的数值解。"""
    if scheme == "euler":
        return y + h * (-rate * y)
    if scheme == "midpoint":
        k1 = -rate * y
        k2 = -rate * (y + h * k1)
        return y + h * (k1 + k2) / 2
    if scheme == "rk4":
        k1 = -rate * y
        k2 = -rate * (y + h * k1 / 2)
        k3 = -rate * (y + h * k2 / 2)
        k4 = -rate * (y + h * k3)
        return y + h * (k1 + 2 * k2 + 2 * k3 + k4) / 6
    raise ValueError(f"unknown scheme: {scheme!r}")


SCHEMES = ("euler", "midpoint", "rk4")


def max_error(steps, rate, t_end, y0, scheme):
    """在 steps 个等距网格上求解，返回与精确解的最大绝对误差。"""
    h = t_end / steps
    time, y = 0.0, y0
    worst = 0.0
    for _ in range(steps):
        y = step(scheme, y, h, rate)
        time += h
        worst = max(worst, abs(y - exact_solution(time, rate, y0)))
    return worst


def main():
    parser = argparse.ArgumentParser()
    for name in ("instance", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    scheme = load(args.config)["scheme"]
    if scheme not in SCHEMES:
        raise ValueError(f"unknown scheme: {scheme!r}")
    y0 = 1.0 + 0.25 * (int(args.seed) % 5 - 2) / 2.0
    rows = load(args.instance)
    errors = [max_error(row["steps"], row["rate"], row["t_end"], y0, scheme) for row in rows]
    Path(args.output).write_text(
        json.dumps({"steps": [row["steps"] for row in rows], "errors": errors}),
        encoding="utf-8")


if __name__ == "__main__":
    main()
