"""staged_artifacts 形状的通用实现。

适用于「非逐样本、产出声明式制品」的领域：算法吞吐、数值收敛阶……候选一次执行
写出若干**原始测量**，指标值由域包从测量派生。因此评分输入是制品里的原始测量，
而不是 ``{id, prediction}`` 序列——候选只能报告测得的事实，指标解释权属于域包，
自报指标名会被立即拒绝。

与 aligned 形状的区别：不要求逐样本 id 对齐，重复单位也不是训练种子。

本模块属于判定代码：只用标准库，且只依赖 ``popper.core`` 的校验助手。
"""
from __future__ import annotations

from ..core import ProtocolError, digest, number
from .protocol import (DEFAULT_SPLITS, STAGED_ARTIFACTS, Invocation, MetricCarrier,
                       metric_spec)

# 字段声明允许的取值类型：正整数 / 有限正数值 / 非空有限正数值序列 /
# 任意符号有限数值（允许负数与零，如处理效应估计值）/ 0-1 整数标志。
_KIND_TEXT = {"int": "整数值", "number": "数值", "series": "数值序列",
              "scalar": "有限数值", "flag": "0/1 标志"}


def _check_fields(present, declared, label):
    """键集合必须恰好等于声明集合；多/少都失败并指出具体字段名。"""
    extra = sorted(present - declared)
    missing = sorted(declared - present)
    if extra:
        raise ProtocolError(f"{label}含未声明字段: {'/'.join(extra)}")
    if missing:
        raise ProtocolError(f"{label}缺少声明字段: {'/'.join(missing)}")


def _check_kind(value, kind, name):
    """按声明类型校验取值：必须为正（``int`` 还要求真整数）。

    ``series`` 表示**非空**的有限正数值序列：逐元素校验，任一元不合法即失败。
    序列用于「一次执行扫过一组参数、每组各得一测量」的领域（如数值收敛阶的
    网格尺寸与误差）。
    """
    if kind == "int":
        positive = type(value) is int and value > 0
    elif kind == "number":
        positive = number(value) and value > 0
    elif kind == "series":
        if not isinstance(value, list) or not value:
            raise ProtocolError(f"字段 {name} 必须是非空的{_KIND_TEXT[kind]}: {value!r}")
        for item in value:
            if not (number(item) and item > 0):
                raise ProtocolError(
                    f"字段 {name} 的{_KIND_TEXT[kind]}含非正有限数值: {item!r}")
        return None
    elif kind == "scalar":
        # 与 number 的差别：允许零与负数（效应估计、残差等测量天然可能为负）。
        if not number(value):
            raise ProtocolError(f"字段 {name} 必须是{_KIND_TEXT[kind]}（允许负数/零）: {value!r}")
        return None
    elif kind == "flag":
        if type(value) is not int or value not in (0, 1):
            raise ProtocolError(f"字段 {name} 必须是 0/1 整数标志: {value!r}")
        return None
    else:
        raise ProtocolError(f"字段 {name} 声明了未支持的取值类型: {kind!r}")
    if not positive:
        raise ProtocolError(f"字段 {name} 必须是正的{_KIND_TEXT[kind]}: {value!r}")


class StagedArtifactsPack(MetricCarrier):
    """声明式制品形状的领域包基类。"""

    pack_id = ""
    schema_version = "1.0"
    evaluator_id = ""
    task_shape = STAGED_ARTIFACTS

    # 数据行（工作负载清单）的字段：(name, kind)，kind ∈ {"int","number","series",
    # "scalar","flag"}
    workload_fields: tuple = ()
    # 候选制品的必需字段：(name, kind)，kind 同上
    measurement_fields: tuple = ()

    _metrics: tuple = ()
    _entry: dict = {}

    # True 表示重复单位是数据内的分析单元：unit_values(rows) 导出每划分的重复取值，
    # experiment.json 不得声明 seeds。默认 False：重复取值来自预注册 seeds。
    units_from_data = False

    # ---- 域钩子：子类必须实现 ----

    def invocation(self) -> Invocation:
        """调用契约由具体域包声明：staged 领域的输入/制品命名因领域而异。"""
        raise NotImplementedError

    def score_measurements(self, measurements, metric_id):
        """用制品里的原始测量计分。"""
        raise NotImplementedError

    def unit_values(self, rows):
        """数据内分析单元取值；默认 ``None`` = 重复取值来自预注册 seeds。"""
        return None

    # ---- 协议实现 ----

    def evaluator_entry(self):
        return dict(self._entry)

    def splits(self):
        return DEFAULT_SPLITS

    def repeat_unit(self):
        return "independent_run"

    def validate_rows(self, rows, split):
        """校验工作负载清单：id 唯一，声明字段齐全且为正数。"""
        if not isinstance(rows, list) or not rows:
            raise ProtocolError("工作负载清单必须是非空列表")
        names = {"id", *(name for name, _ in self.workload_fields)}
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ProtocolError(f"{self.pack_id} 工作负载行必须是 JSON 对象")
            _check_fields(set(row), names, "工作负载行")
            if not isinstance(row["id"], str) or not row["id"] or row["id"] in seen:
                raise ProtocolError("工作负载 id 必须是非空唯一字符串")
            seen.add(row["id"])
            for name, kind in self.workload_fields:
                _check_kind(row[name], kind, name)
        return None

    def project_inputs(self, rows):
        """工作负载清单本身就是候选可见输入，没有标签可去。"""
        return [dict(row) for row in rows]

    def row_identity(self, row):
        return digest([row[name] for name in
                       ("id", *(name for name, _ in self.workload_fields))])

    def validate_splits(self, sets):
        """跨划分无维度约束：划分之间只要求 id / 样本身份不重叠（由 core 检查）。"""
        return None

    def validate_predictions(self, rows, payload, metric_id):
        """校验候选制品：只含声明的测量字段，且不得自报指标。"""
        if not isinstance(payload, dict):
            raise ProtocolError("制品必须是 JSON 对象")
        self.metric(metric_id)  # 未声明的指标立即失败
        reported = sorted(set(payload) & {spec.name for spec in self.metrics()})
        if reported:
            raise ProtocolError(
                f"候选不得自行声明指标（制品含指标同名字段: {'/'.join(reported)}）")
        _check_fields(set(payload), {name for name, _ in self.measurement_fields}, "候选制品")
        for name, kind in self.measurement_fields:
            _check_kind(payload[name], kind, name)
        return None

    def score(self, rows, payload, metric_id, unit=None):
        spec = self.metric(metric_id)
        self.validate_predictions(rows, payload, spec.name)
        result = self.score_measurements(payload, spec.name)
        if not number(result):
            raise ProtocolError(f"{spec.name} 评估结果非有限值")
        if not spec.accepts(result):
            raise ProtocolError(
                f"{spec.name} 评估结果超出声明值域（{spec.range_text()}）: {result!r}")
        return result
