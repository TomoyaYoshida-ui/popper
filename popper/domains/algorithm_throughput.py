"""算法吞吐域包：staged_artifacts 形状的第一个真实领域。

候选在给定工作负载清单上执行一次算法，只报告自己数出的**原始测量**（基本操作数、
墙上时间）；「每秒基本操作数」由本域包计算，候选自报同名指标会被立即拒绝。
重复单位是**独立重复测量**：每个种子用不同的伪随机排列重复测量同一算法，而不是训练种子。

接入本领域的全部改动 = 本文件 + ``popper/domains/__init__.py`` 一行注册；调用契约
（参数名、输入制品名、制品牌名）也由这里声明，判定层与执行主流程无需改动。
"""
from __future__ import annotations

from .protocol import Invocation, MetricSpec, register
from .staged import StagedArtifactsPack

WARMUP_BASENAME = "warmup.json"
WORKLOAD_BASENAME = "workload.json"
CONFIG_BASENAME = "algorithm.json"
MEASUREMENT_TEMPLATE = "measurements-{seed}.json"


class AlgorithmThroughputV1(StagedArtifactsPack):
    pack_id = "algorithm-throughput"
    schema_version = "1.0"
    evaluator_id = "algorithm-throughput-v1"

    # 工作负载清单：id + 正整数规模 n。
    workload_fields = (("n", "int"),)
    # 候选制品：自己数出的基本操作数与墙上时间。
    measurement_fields = (("operations", "int"), ("elapsed_seconds", "number"))

    _metrics = (MetricSpec(name="throughput", direction="max", unit="operations_per_second",
                           description="每秒完成的基本操作数（比较与元素移动之和）",
                           value_domain=(0, None)),)
    _entry = {
        "id": "algorithm-throughput-v1",
        "metric": {"name": "throughput", "direction": "max"},
        "definition": ("self-counted elementary operations divided by elapsed seconds "
                       "over the declared workload list"),
        "dataset": "rows[id,n] where n is a positive integer workload size",
    }

    def invocation(self):
        """调用契约：预热清单 + 工作负载 + 算法配置 → 一份测量制品。

        参数名与制品名都与逐样本形状不同，这正是「契约由域包声明」要覆盖的情况：
        核心流程只按角色取路径，不关心这些字面量。
        """
        return Invocation(
            args=(("--warmup", "train"), ("--workload", "inputs"), ("--output", "prediction"),
                  ("--config", "config"), ("--seed", "seed")),
            inputs=(("train", WARMUP_BASENAME), ("inputs", WORKLOAD_BASENAME),
                    ("config", CONFIG_BASENAME)),
            prediction=MEASUREMENT_TEMPLATE,
        )

    def score_measurements(self, measurements, metric_id):
        # 取值合法性（正数、有限）已在 validate_predictions 里保证，这里不会再除以 0。
        return measurements["operations"] / measurements["elapsed_seconds"]


PACK = register(AlgorithmThroughputV1())
