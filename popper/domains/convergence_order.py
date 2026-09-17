"""数值收敛阶域包：staged_artifacts 形状的第二个真实领域（数值计算）。

候选对**同一个**初值问题在声明的一串细化网格上求解，只报告自己测得的**误差序列**
（每个网格一个误差范数）；「收敛阶」由本域包用 log(误差) 对 log(步数) 的最小二乘
斜率取负得到，候选自报同名指标会被立即拒绝。

与算法吞吐域包的差别：这里的测量不是单个数，而是一条**序列**——整条误差衰减曲线
都要交给域包，阶数才有意义；因此工作负载行里的步数是一张声明式的细化表，候选必须
原样复述，不得自行改写它实际使用的网格。

接入本领域的全部改动 = 本文件 + ``popper/domains/__init__.py`` 一行注册；调用契约
（参数名、输入制品名、制品牌名）也由这里声明，判定层与执行主流程无需改动。
"""
from __future__ import annotations

import math

from ..core import ProtocolError
from .protocol import Invocation, MetricSpec, register
from .staged import StagedArtifactsPack

INSTANCE_BASENAME = "convergence.json"
CONFIG_BASENAME = "scheme.json"
MEASUREMENT_TEMPLATE = "measurements-{seed}.json"


def _least_squares_order(steps, errors):
    """log(误差) 对 log(步数) 的最小二乘斜率取负：误差 ≈ C · 步数^(-阶数)。"""
    x = [math.log(step) for step in steps]
    y = [math.log(error) for error in errors]
    count = len(x)
    mean_x = sum(x) / count
    mean_y = sum(y) / count
    # 步数序列已被要求严格递增，分母不会被 0 除。
    denominator = sum((value - mean_x) ** 2 for value in x)
    slope = sum((value - mean_x) * (residual - mean_y) for value, residual in zip(x, y))
    return -slope / denominator


class ConvergenceOrderV1(StagedArtifactsPack):
    pack_id = "convergence-order"
    schema_version = "1.0"
    evaluator_id = "convergence-order-v1"

    # 细化表：id + 步数 steps + 本次研究共享的问题实例（衰减率 rate、积分终点 t_end）。
    workload_fields = (("steps", "int"), ("rate", "number"), ("t_end", "number"))
    # 候选制品：复述的步数序列 + 自己测得的误差序列（逐网格一个误差范数）。
    measurement_fields = (("steps", "series"), ("errors", "series"))

    _metrics = (MetricSpec(name="convergence_order", direction="max", unit="order",
                           description="误差随网格加密的观测衰减阶数（log-log 最小二乘斜率）",
                           value_domain=(0, None)),)
    _entry = {
        "id": "convergence-order-v1",
        "metric": {"name": "convergence_order", "direction": "max"},
        "definition": ("negated least-squares slope of log(error) against log(steps) "
                       "over the declared refinement schedule"),
        "dataset": ("rows[id,steps,rate,t_end] where steps is a positive integer and the "
                    "problem instance (rate, t_end) is shared by all rows of a split"),
    }

    def invocation(self):
        """调用契约：问题实例（含细化表）+ 格式配置 → 一份测量制品。

        本领域没有训练阶段，``train`` 输入角色因此不被声明；声明集是角色的子集，
        核心流程与执行端都只按声明的角色落位（落位表来自这里，不是约定的三件套）。
        """
        return Invocation(
            args=(("--instance", "inputs"), ("--output", "prediction"),
                  ("--config", "config"), ("--seed", "seed")),
            inputs=(("inputs", INSTANCE_BASENAME), ("config", CONFIG_BASENAME)),
            prediction=MEASUREMENT_TEMPLATE,
        )

    def validate_rows(self, rows, split):
        """细化表必须能构成一次收敛研究：至少两个网格、严格加密、只针对一个问题。"""
        super().validate_rows(rows, split)
        steps = [row["steps"] for row in rows]
        if len(steps) < 2:
            raise ProtocolError("收敛研究的细化表至少需要两个网格，否则拟合不出阶数")
        if any(later <= earlier for earlier, later in zip(steps, steps[1:])):
            raise ProtocolError(f"细化表的步数必须按行序严格递增（实际 {steps}）")
        instances = {(row["rate"], row["t_end"]) for row in rows}
        if len(instances) != 1:
            raise ProtocolError("同一次收敛研究必须只针对一个问题实例（rate/t_end 全表一致）")
        return None

    def validate_predictions(self, rows, payload, metric_id):
        """候选必须原样复述细化表，并逐网格给出一个误差；不得自报指标。"""
        super().validate_predictions(rows, payload, metric_id)
        steps = [row["steps"] for row in rows]
        if payload["steps"] != steps:
            raise ProtocolError(
                f"制品复述的步数序列与声明的细化表不一致: {payload['steps']!r} ≠ {steps!r}")
        if len(payload["errors"]) != len(steps):
            raise ProtocolError(
                f"误差序列必须逐网格一一对应（{len(payload['errors'])} 个误差 vs {len(steps)} 个网格）")
        return None

    def score_measurements(self, measurements, metric_id):
        errors = measurements["errors"]
        # 误差随网格加密严格下降是「收敛」一词的实质内容；不满足时拟合出的斜率
        # 只是噪声的斜率，必须显式失败而不是当成一个偏低的阶数写进证据。
        if any(later >= earlier for earlier, later in zip(errors, errors[1:])):
            raise ProtocolError(f"误差序列必须随网格加密严格下降（实际 {errors}）")
        return _least_squares_order(measurements["steps"], errors)


PACK = register(ConvergenceOrderV1())
