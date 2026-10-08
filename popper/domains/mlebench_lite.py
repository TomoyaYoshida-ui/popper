"""MLE-bench Lite 的三道小题。

每道题的官方隐藏划分对应研究内核的一次性确认集：开发计分只看 train/dev，
确认集在协议上只消费一次。计分只用标准库，不读取比赛文件、不调用模型。

三道题都来自 Lite（low）切分里体积最小、且能落在逐样本预测形状上的任务：
作者分类用对数损失，透明导体用两列 RMSLE 的平均，披萨请求用 ROC AUC。
"""
from __future__ import annotations

import math
import statistics

from ..core import ProtocolError, number
from .aligned import AlignedPredictionPack
from .protocol import MetricSpec, register

SPOOKY_AUTHORS = ("EAP", "HPL", "MWS")
_PROBABILITY_SUM_TOLERANCE = 1e-3


def _probability(value):
    return number(value) and 0 < value <= 1


class SpookyAuthorLogLossV1(AlignedPredictionPack):
    """spooky-author-identification：三类作者的对数损失，越小越好。"""

    pack_id = "mlebench-spooky"
    schema_version = "1.0"
    evaluator_id = "mlebench-spooky-logloss-v1"

    input_fields = ("text",)
    target_field = "label"

    _metrics = (MetricSpec(name="spooky_log_loss", direction="min", unit="nats",
                           description="三类作者对数损失",
                           value_domain=(0, None)),)
    _entry = {
        "id": "mlebench-spooky-logloss-v1",
        "metric": {"name": "spooky_log_loss", "direction": "min"},
        "definition": "mean negative log probability of the true author; authors are EAP, HPL, MWS",
        "dataset": "rows[id,text,label] with nonempty text and label in {0,1,2}",
    }

    def smoke_repeat_limit(self):
        return 1

    def validate_input_fields(self, row):
        if not isinstance(row["text"], str) or not row["text"].strip():
            raise ProtocolError("作者分类 text 必须是非空字符串")

    def validate_target(self, value):
        if type(value) is not int or value not in {0, 1, 2}:
            raise ProtocolError("作者分类 label 必须是 0、1 或 2")

    def validate_prediction(self, value):
        if (not isinstance(value, list) or len(value) != 3
                or any(not _probability(item) for item in value)
                or abs(sum(value) - 1) > _PROBABILITY_SUM_TOLERANCE):
            raise ProtocolError("作者分类预测必须是和为 1 的三类正概率")

    def validate_splits(self, sets):
        labels = {row[self.target_field] for rows in sets for row in rows}
        if labels != {0, 1, 2}:
            raise ProtocolError("作者分类的划分合起来必须同时包含三个类别")

    def score_values(self, rows, values, metric_id):
        return statistics.mean(-math.log(values[row["id"]][row["label"]]) for row in rows)


class NomadMeanRmsleV1(AlignedPredictionPack):
    """nomad2018：形成能与带隙两列 RMSLE 的算术平均，越小越好。"""

    pack_id = "mlebench-nomad"
    schema_version = "1.0"
    evaluator_id = "mlebench-nomad-rmsle-v1"

    input_fields = ("features",)
    target_field = "y"
    vector_field = "features"

    _metrics = (MetricSpec(name="nomad_mean_rmsle", direction="min", unit="rmsle",
                           description="两列 RMSLE 的平均",
                           value_domain=(0, None)),)
    _entry = {
        "id": "mlebench-nomad-rmsle-v1",
        "metric": {"name": "nomad_mean_rmsle", "direction": "min"},
        "definition": "mean of column-wise RMSLE for formation energy and bandgap",
        "dataset": "rows[id,features,y] with finite features and two nonnegative targets",
    }

    def smoke_repeat_limit(self):
        return 1

    def validate_input_fields(self, row):
        values = row["features"]
        if (not isinstance(values, list) or not values
                or any(not number(value) for value in values)):
            raise ProtocolError("导体特征必须是非空有限数值列表")

    def validate_target(self, value):
        self._pair(value, "导体标签")

    def validate_prediction(self, value):
        self._pair(value, "导体预测")

    def _pair(self, value, label):
        if (not isinstance(value, list) or len(value) != 2
                or any(not number(item) or item < 0 for item in value)):
            raise ProtocolError(f"{label}必须是两个非负有限数")

    def score_values(self, rows, values, metric_id):
        columns = []
        for index in (0, 1):
            squared = [(_log1p(values[row["id"]][index]) - _log1p(row["y"][index])) ** 2
                       for row in rows]
            columns.append(math.sqrt(statistics.mean(squared)))
        return statistics.mean(columns)


class PizzaRocAucV1(AlignedPredictionPack):
    """random-acts-of-pizza：收到披萨的 ROC AUC，越大越好。"""

    pack_id = "mlebench-pizza"
    schema_version = "1.0"
    evaluator_id = "mlebench-pizza-auc-v1"

    input_fields = ("text",)
    target_field = "label"

    _metrics = (MetricSpec(name="pizza_roc_auc", direction="max", unit="fraction",
                           description="二分类 ROC AUC",
                           value_domain=(0, 1)),)
    _entry = {
        "id": "mlebench-pizza-auc-v1",
        "metric": {"name": "pizza_roc_auc", "direction": "max"},
        "definition": "Mann-Whitney ROC AUC of the probability a request received pizza",
        "dataset": "rows[id,text,label] with nonempty text and label in {0,1}",
    }

    def smoke_repeat_limit(self):
        return 1

    def validate_input_fields(self, row):
        if not isinstance(row["text"], str) or not row["text"].strip():
            raise ProtocolError("披萨请求 text 必须是非空字符串")

    def validate_target(self, value):
        if type(value) is not int or value not in {0, 1}:
            raise ProtocolError("披萨标签必须是整数 0 或 1")

    def validate_prediction(self, value):
        if not _probability(value):
            raise ProtocolError("披萨预测必须是 (0, 1] 内的概率")

    def validate_splits(self, sets):
        labels = {row[self.target_field] for rows in sets for row in rows}
        if labels != {0, 1}:
            raise ProtocolError("披萨划分合起来必须同时包含两个类别")

    def score_values(self, rows, values, metric_id):
        positive = [values[row["id"]] for row in rows if row["label"] == 1]
        negative = [values[row["id"]] for row in rows if row["label"] == 0]
        if not positive or not negative:
            raise ProtocolError("ROC AUC 需要正负两类样本")
        wins = 0.0
        for left in positive:
            for right in negative:
                if left > right:
                    wins += 1
                elif left == right:
                    wins += 0.5
        return wins / (len(positive) * len(negative))


def _log1p(value):
    if not number(value) or value < 0:
        raise ProtocolError("RMSLE 只接受非负有限数")
    return math.log1p(value)


register(SpookyAuthorLogLossV1())
register(NomadMeanRmsleV1())
register(PizzaRocAucV1())
