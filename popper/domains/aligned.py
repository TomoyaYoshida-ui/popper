"""aligned_prediction 形状的通用实现。

候选对每个样本给出一个标量预测，指标是逐样本对齐后的聚合值。基类承担形状机制
（字段集合、id 唯一性、向量维度一致性、输入投影、样本身份、预测覆盖与对齐），
域包只声明数据字段与三个判定钩子，因此新增指标通常只需「一个新文件 + 一行注册」。

本模块属于判定代码：只用标准库，且不依赖图之外的东西。
"""
from __future__ import annotations

from ..core import ProtocolError, digest, number
from .protocol import (ALIGNED_PREDICTION, DEFAULT_SPLITS, INPUTS_SPLIT, Invocation,
                       MetricCarrier)


class AlignedPredictionPack(MetricCarrier):
    """逐样本预测形状的领域包基类。"""

    pack_id = ""
    schema_version = "1.0"
    evaluator_id = ""
    task_shape = ALIGNED_PREDICTION

    # 候选可见的输入字段（不含 id）与标签字段。
    input_fields: tuple = ()
    target_field = ""
    # 若输入含定长数值向量，在此声明字段名；基类据此校验维度一致性。
    vector_field = None

    _metrics: tuple = ()
    _entry: dict = {}

    # 逐样本领域的重复单位是训练种子，取值来自预注册 seeds（见 protocol.unit_values）。
    units_from_data = False

    def unit_values(self, rows):
        """逐样本领域不从数据导出分析单元。"""
        return None

    # ---- 域钩子：子类必须实现 ----

    def validate_input_fields(self, row):
        """校验候选可见字段的取值（数值有限性、特征合法性……）。"""
        raise NotImplementedError

    def validate_target(self, value):
        """校验标签取值。"""
        raise NotImplementedError

    def validate_prediction(self, value):
        """校验单个预测值是否落在本指标的合法取值内。"""
        raise NotImplementedError

    def score_values(self, rows, values, metric_id):
        """按已对齐的 ``{id: prediction}`` 计分。"""
        raise NotImplementedError

    # ---- 协议实现 ----

    def evaluator_entry(self):
        return dict(self._entry)

    def invocation(self):
        """与改造前 core.py / controller.py 的字面量逐字节一致的逐样本契约。

        core.py 侧按 run_dir 布局解析、worker 侧按 inputs//outputs/ 布局解析，
        因此这里只声明「文件名」，不声明目录前缀。
        """
        return Invocation(
            args=(("--train", "train"), ("--input", "inputs"), ("--output", "prediction"),
                  ("--config", "config"), ("--seed", "seed")),
            inputs=(("train", "train.json"), ("inputs", "inputs.json"), ("config", "config.json")),
            prediction="predictions-{seed}.json",
        )

    def splits(self):
        return DEFAULT_SPLITS

    def repeat_unit(self):
        return "train_seed"

    def validate_splits(self, sets):
        """跨划分的向量维度一致性；未声明向量字段时无约束。"""
        if self.vector_field is None:
            return None
        dimensions = {len(row[self.vector_field]) for rows in sets for row in rows}
        if len(dimensions) != 1:
            raise ProtocolError(f"{self.pack_id} 训练/开发/测试特征维度不一致")
        return None

    def expected_fields(self, split):
        fields = {"id", *self.input_fields}
        if split != INPUTS_SPLIT:
            fields.add(self.target_field)
        return fields

    def validate_rows(self, rows, split):
        if not isinstance(rows, list) or not rows:
            raise ProtocolError("数据集必须是非空列表")
        expected = self.expected_fields(split)
        seen = set()
        dimensions = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected:
                raise ProtocolError(
                    f"{self.pack_id} 数据行必须且只能包含 {'/'.join(sorted(expected))}")
            if not isinstance(row["id"], str) or not row["id"] or row["id"] in seen:
                raise ProtocolError("数据 id 必须是非空唯一字符串")
            seen.add(row["id"])
            self.validate_input_fields(row)
            if self.vector_field is not None:
                dimensions.add(len(row[self.vector_field]))
            if split != INPUTS_SPLIT:
                self.validate_target(row[self.target_field])
        if len(dimensions) > 1:
            raise ProtocolError(f"{self.pack_id} 特征维度不一致")

    def project_inputs(self, rows):
        return [{"id": row["id"], **{name: row[name] for name in self.input_fields}}
                for row in rows]

    def row_identity(self, row):
        fields = (*self.input_fields, self.target_field)
        missing = [name for name in fields if name not in row]
        if missing:
            raise ProtocolError(
                f"{self.pack_id} 样本身份需要带标签的行，缺少字段: {'/'.join(missing)}")
        return digest([row[name] for name in fields])

    def validate_prediction_values(self, predictions, expected_ids, metric_id=None):
        """校验预测结构与 id 覆盖，不需要标签行。

        确认执行端（runner）刻意拿不到 held-out 标签，只能校验「形状 + 覆盖」；
        与 ``validated_values`` 共用同一份契约，避免出现第二套校验副本。
        """
        self.metric(metric_id)
        if not isinstance(predictions, list) or len(predictions) != len(set(expected_ids)):
            raise ProtocolError("预测数量不匹配")
        seen = set()
        for pred in predictions:
            if not isinstance(pred, dict) or set(pred) != {"id", "prediction"}:
                raise ProtocolError("预测只允许 id/prediction；不能自报 metric")
            if not isinstance(pred["id"], str) or pred["id"] in seen:
                raise ProtocolError("预测 id 必须是非空且不重复的字符串")
            self.validate_prediction(pred["prediction"])
            seen.add(pred["id"])
        if seen != set(expected_ids):
            raise ProtocolError("预测与评估数据 id 不匹配")
        return predictions

    def validated_values(self, rows, predictions, metric_id=None):
        """校验预测结构并按 id 对齐；返回 ``{id: prediction}``。"""
        self.validate_prediction_values(predictions, [row["id"] for row in rows], metric_id)
        return {pred["id"]: pred["prediction"] for pred in predictions}

    def validate_predictions(self, rows, predictions, metric_id):
        self.validated_values(rows, predictions, metric_id)

    def score(self, rows, predictions, metric_id=None, unit=None):
        spec = self.metric(metric_id)
        values = self.validated_values(rows, predictions, spec.name)
        result = self.score_values(rows, values, spec.name)
        if not number(result):
            raise ProtocolError(f"{spec.name} 评估结果非有限值")
        if not spec.accepts(result):
            raise ProtocolError(
                f"{spec.name} 评估结果超出声明值域（{spec.range_text()}）: {result!r}")
        return result
