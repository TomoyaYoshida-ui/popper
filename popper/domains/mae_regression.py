"""表格回归域包（MAE）：与 MSE 同形状，只换判定指标。

新增指标的全部改动 = 本文件 + ``popper/domains/__init__.py`` 一行注册。
"""
from __future__ import annotations

import statistics

from .protocol import MetricSpec, register
from .tabular_regression import TabularRegressionV1


class MaeRegressionV1(TabularRegressionV1):
    pack_id = "tabular-regression-mae"
    evaluator_id = "mae-v1"

    _metrics = (MetricSpec(name="mae", direction="min", unit="absolute_error",
                           description="id 对齐平均绝对误差",
                           value_domain=(0, None)),)
    _entry = {
        "id": "mae-v1", "metric": {"name": "mae", "direction": "min"},
        "definition": "id-aligned arithmetic mean of absolute finite prediction errors",
        "dataset": "rows[id,x,y] where x and y are finite numbers",
    }

    def score_values(self, rows, values, metric_id):
        return statistics.mean(abs(values[row["id"]] - row["y"]) for row in rows)


PACK = register(MaeRegressionV1())
