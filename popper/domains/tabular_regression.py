"""表格回归域包：逐样本预测连续数值，主指标为 MSE。

评估器 ID 沿用 ``mse-v1``，冻结身份记录与既有实验逐字节一致，
因此已初始化实验的 ``evaluator_hash`` 不变（域包化不使旧实验失效）。
"""
from __future__ import annotations

import statistics

from ..core import ProtocolError, number
from .aligned import AlignedPredictionPack
from .protocol import MetricSpec, register


class TabularRegressionV1(AlignedPredictionPack):
    pack_id = "tabular-regression"
    schema_version = "1.0"
    evaluator_id = "mse-v1"

    input_fields = ("x",)
    target_field = "y"

    _metrics = (MetricSpec(name="mse", direction="min", unit="squared_error",
                           description="id 对齐均方误差",
                           value_domain=(0, None)),)
    _entry = {
        "id": "mse-v1", "metric": {"name": "mse", "direction": "min"},
        "definition": "id-aligned arithmetic mean of squared finite prediction errors",
        "dataset": "rows[id,x,y] where x and y are finite numbers",
    }

    def validate_input_fields(self, row):
        if not number(row["x"]):
            raise ProtocolError("回归数据 x 必须是有限数值")

    def validate_target(self, value):
        if not number(value):
            raise ProtocolError("回归数据 y 必须是有限数值")

    def validate_prediction(self, value):
        if not number(value):
            raise ProtocolError("回归预测必须是有限数值")

    def score_values(self, rows, values, metric_id):
        return statistics.mean((values[row["id"]] - row["y"]) ** 2 for row in rows)


PACK = register(TabularRegressionV1())
