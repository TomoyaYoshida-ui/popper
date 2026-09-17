"""二分类域包（F1）：与准确率同形状，只换判定指标。

F1 的值域上限为 1，这正是「只校验下界」的旧代码会放行非法分数的地方；
本包把值域写进 MetricSpec，判定层因此不再需要指标分支。
"""
from __future__ import annotations

from ..core import ProtocolError
from .binary_classification import BinaryClassificationV1
from .protocol import MetricSpec, register


class BinaryF1V1(BinaryClassificationV1):
    pack_id = "binary-classification-f1"
    evaluator_id = "binary-f1-v1"

    _metrics = (MetricSpec(name="f1", direction="max", unit="fraction",
                           description="二分类 F1（正类为 1）",
                           value_domain=(0, 1)),)
    _entry = {
        "id": "binary-f1-v1", "metric": {"name": "f1", "direction": "max"},
        "definition": "id-aligned F1 over the positive class (label 1)",
        "dataset": "rows[id,features,label] with finite numeric features and label in {0,1}",
    }

    def score_values(self, rows, values, metric_id):
        true_positive = false_positive = false_negative = 0
        for row in rows:
            actual, predicted = row["label"], values[row["id"]]
            if actual == 1 and predicted == 1:
                true_positive += 1
            elif actual == 1:
                false_negative += 1
            elif predicted == 1:
                false_positive += 1
        # 没有正类标签时 F1 未定义：显式失败，而不是静默返回 0。
        if 2 * true_positive + false_positive + false_negative == 0:
            raise ProtocolError("f1 在评估数据不含正类标签时未定义")
        return 2 * true_positive / (2 * true_positive + false_positive + false_negative)


PACK = register(BinaryF1V1())
