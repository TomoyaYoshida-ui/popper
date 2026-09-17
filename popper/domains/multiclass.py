"""多分类域包：标签从「{0,1} 二选一」放宽为非负整数类别编号。

这是旧代码最严重的静默默认之一：任何新 evaluator 都会被 ``else`` 分支按二分类
语义处理，合法的多分类标签会被静默拒绝或被误判为非法预测。这里用显式契约替代：

- 类别集合由数据声明，跨划分一致性由 ``validate_splits`` 检查；
- 预测必须是评估数据中真实存在的类别（否则立即失败，而不是静默算错）；
- 指标为类别集合上的宏平均 F1。
"""
from __future__ import annotations

import statistics

from ..core import ProtocolError, number
from .aligned import AlignedPredictionPack
from .protocol import MetricSpec, register


class MulticlassClassificationV1(AlignedPredictionPack):
    pack_id = "multiclass-classification"
    schema_version = "1.0"
    evaluator_id = "multiclass-macro-f1-v1"

    input_fields = ("features",)
    target_field = "label"
    vector_field = "features"

    _metrics = (MetricSpec(name="macro_f1", direction="max", unit="fraction",
                           description="多分类宏平均 F1",
                           value_domain=(0, 1)),)
    _entry = {
        "id": "multiclass-macro-f1-v1", "metric": {"name": "macro_f1", "direction": "max"},
        "definition": "id-aligned macro-averaged F1 over the label set present in scored rows",
        "dataset": ("rows[id,features,label] with finite numeric features and nonnegative "
                    "integer labels"),
    }

    def validate_input_fields(self, row):
        values = row["features"]
        if (not isinstance(values, list) or not values
                or any(not number(value) for value in values)):
            raise ProtocolError("多分类 features 必须是非空有限数值列表")

    def validate_target(self, value):
        if type(value) is not int or value < 0:
            raise ProtocolError("多分类 label 必须是非负整数类别编号")

    def validate_prediction(self, value):
        if type(value) is not int or value < 0:
            raise ProtocolError("多分类预测必须是非负整数类别编号")

    def validate_splits(self, sets):
        sets = list(sets)
        super().validate_splits(sets)
        if len({row[self.target_field] for rows in sets for row in rows}) < 2:
            raise ProtocolError(f"{self.pack_id} 至少需要两个类别")

    def validated_values(self, rows, predictions, metric_id=None):
        values = super().validated_values(rows, predictions, metric_id)
        labels = {row[self.target_field] for row in rows}
        unknown = sorted({value for value in values.values() if value not in labels})
        if unknown:
            raise ProtocolError(f"{self.pack_id} 预测出现评估数据中不存在的类别: {unknown}")
        return values

    def score_values(self, rows, values, metric_id):
        scores = []
        for label in sorted({row[self.target_field] for row in rows}):
            true_positive = false_positive = false_negative = 0
            for row in rows:
                actual, predicted = row[self.target_field], values[row["id"]]
                if actual == label and predicted == label:
                    true_positive += 1
                elif actual == label:
                    false_negative += 1
                elif predicted == label:
                    false_positive += 1
            scores.append(2 * true_positive / (2 * true_positive + false_positive + false_negative))
        return statistics.mean(scores)


PACK = register(MulticlassClassificationV1())
