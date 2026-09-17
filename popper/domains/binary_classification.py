"""二分类域包：逐样本预测 0/1，主指标为准确率。

评估器 ID 沿用 ``binary-accuracy-v1``，冻结身份记录与既有实验逐字节一致。
"""
from __future__ import annotations

import statistics

from ..core import ProtocolError, number
from .aligned import AlignedPredictionPack
from .protocol import MetricSpec, register


class BinaryClassificationV1(AlignedPredictionPack):
    pack_id = "binary-classification"
    schema_version = "1.0"
    evaluator_id = "binary-accuracy-v1"

    input_fields = ("features",)
    target_field = "label"
    vector_field = "features"

    _metrics = (MetricSpec(name="accuracy", direction="max", unit="fraction",
                           description="准确率", value_domain=(0, 1)),)
    _entry = {
        "id": "binary-accuracy-v1", "metric": {"name": "accuracy", "direction": "max"},
        "definition": "id-aligned fraction of exact binary-label predictions",
        "dataset": "rows[id,features,label] with finite numeric features and label in {0,1}",
    }

    def validate_input_fields(self, row):
        values = row["features"]
        if (not isinstance(values, list) or not values
                or any(not number(value) for value in values)):
            raise ProtocolError("分类 features 必须是非空有限数值列表")

    def validate_target(self, value):
        if type(value) is not int or value not in {0, 1}:
            raise ProtocolError("分类 label 必须是整数 0 或 1")

    def validate_prediction(self, value):
        if type(value) is not int or value not in {0, 1}:
            raise ProtocolError("二分类预测必须是整数 0 或 1")

    def score_values(self, rows, values, metric_id):
        return statistics.mean(values[row["id"]] == row["label"] for row in rows)


PACK = register(BinaryClassificationV1())
