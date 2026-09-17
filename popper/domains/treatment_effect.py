"""处理效应估计域包：staged_artifacts 形状的第三个真实领域（计量/因果）。

候选在每个**分析单元**（区块 block）内估计平均处理效应（ATE），只报告自己的估计值
``ate_estimate``；单元真值（该区块的平均个体处理效应）由本域包用控制器持有的
``tau`` 字段计算，候选看不到 ``tau``，自报同名指标会被立即拒绝。主指标
``ate_error`` 是区块级绝对误差，方向为越小越好。

这个领域把批次 B 只在 docstring 里预示的两件事落地：

1. **分析单元成为一等统计单位**：``repeat_unit`` 为 ``analysis_unit``，重复取值
   （区块 id）由 ``unit_values(rows)`` 从每个数据划分导出，experiment.json 不声明
   seeds；对区块做配对 bootstrap 是合法的单元层抽样不确定性程序，满足预注册判据时
   claim 允许 ``statistical_claim=True``。
2. **非随机切分**：区块必须**整块**分配到划分，且区块 id 按 train < dev < test
   严格排序（冻结的确定性分配规则，不是随机切行）；``validate_splits`` 同时拒绝
   区块跨划分混用、乱序与单臂区块。

接入本领域的判定层改动 = 本文件 + ``popper/domains/__init__.py`` 一行注册；统计
抽象（repeat_unit 词汇表、unit_values、unit 计分参数）在协议层与核心层一次回炉完成。
"""
from __future__ import annotations

from ..core import ProtocolError, number
from .protocol import (REPEAT_ANALYSIS_UNIT, Invocation, MetricSpec, register)
from .staged import StagedArtifactsPack

COHORT_BASENAME = "cohort.json"
CONFIG_BASENAME = "estimator.json"
ESTIMATE_TEMPLATE = "estimate-{seed}.json"

# 每个划分至少要有两个区块：单区块没有任何单元层抽样变异性可言。
MIN_BLOCKS_PER_SPLIT = 2


class TreatmentEffectV1(StagedArtifactsPack):
    pack_id = "treatment-effect"
    schema_version = "1.0"
    evaluator_id = "treatment-effect-v1"

    # 重复单位是数据内的分析单元：区块 id 随划分不同而不同，由 unit_values 导出。
    units_from_data = True

    # 队列行：id + 区块 + 协变量 + 处理标志 + 观测结果；tau 是控制器专用的个体真实
    # 处理效应（标签），project_inputs 会剥掉它，候选进程永远读不到。
    workload_fields = (("block", "int"), ("x", "scalar"), ("treated", "flag"),
                       ("outcome", "scalar"), ("tau", "scalar"))
    # 候选制品：只有一个该区块的 ATE 估计值（允许任意符号，估计可能为负）。
    measurement_fields = (("ate_estimate", "scalar"),)

    _metrics = (MetricSpec(name="ate_error", direction="min",
                           unit="absolute_ate_error",
                           description="区块级 ATE 估计与区块真实平均处理效应的绝对误差",
                           value_domain=(0, None)),)
    _entry = {
        "id": "treatment-effect-v1",
        "metric": {"name": "ate_error", "direction": "min"},
        "definition": ("absolute error between the candidate's block-level ATE estimate "
                       "and the controller-held true mean unit treatment effect within "
                       "that block"),
        "dataset": ("rows[id,block,x,treated,outcome,tau] where block is the positive "
                    "integer analysis unit assigned wholesale and in strictly ordered "
                    "id ranges to train/dev/test, treated is 0/1, and tau (the unit "
                    "treatment effect) is stripped from candidate inputs"),
    }

    def invocation(self):
        """调用契约：全队列（去标签）+ 估计器配置 + 区块 id（seed 槽）→ 一份估计制品。"""
        return Invocation(
            args=(("--cohort", "inputs"), ("--output", "prediction"),
                  ("--config", "config"), ("--seed", "seed")),
            inputs=(("inputs", COHORT_BASENAME), ("config", CONFIG_BASENAME)),
            prediction=ESTIMATE_TEMPLATE,
        )

    def repeat_unit(self):
        return REPEAT_ANALYSIS_UNIT

    def unit_values(self, rows):
        """分析单元取值 = 区块 id 升序列表；每个划分导出各自的区块集合。"""
        return tuple(sorted({row["block"] for row in rows}))

    def validate_rows(self, rows, split):
        """区块必须可估计处理效应：区块数 ≥ 2，且每个区块内处理组/对照组都非空。"""
        super().validate_rows(rows, split)
        blocks = {}
        for row in rows:
            blocks.setdefault(row["block"], [0, 0])[row["treated"]] += 1
        if len(blocks) < MIN_BLOCKS_PER_SPLIT:
            raise ProtocolError(
                f"处理效应研究每个划分至少需要 {MIN_BLOCKS_PER_SPLIT} 个区块（分析单元）")
        single_arm = sorted(block for block, counts in blocks.items() if not counts[0] or not counts[1])
        if single_arm:
            raise ProtocolError(f"区块内必须同时含处理组与对照组，单臂区块: {single_arm}")
        return None

    def validate_splits(self, sets):
        """非随机切分规则：区块整块分配、跨划分不重叠、按 train < dev < test 严格排序。"""
        sets = list(sets)
        block_sets = [self.unit_values(rows) for rows in sets]
        for blocks in block_sets:
            if len(blocks) < MIN_BLOCKS_PER_SPLIT:
                raise ProtocolError(
                    f"处理效应研究每个划分至少需要 {MIN_BLOCKS_PER_SPLIT} 个区块（分析单元）")
        for i, left in enumerate(block_sets):
            left_set = set(left)
            for right in block_sets[i + 1:]:
                overlap = left_set & set(right)
                if overlap:
                    raise ProtocolError(
                        f"区块必须整块分配到划分，禁止跨划分混用：区块 {sorted(overlap)}")
        for earlier, later in zip(block_sets, block_sets[1:]):
            if max(earlier) >= min(later):
                raise ProtocolError(
                    "区块必须按冻结的确定性顺序分配（train 的区块 id 全部小于 dev，"
                    f"dev 全部小于 test）：{list(earlier)} 不先于 {list(later)}")
        return None

    def project_inputs(self, rows):
        """剥掉控制器专用真值 tau；其余队列字段候选可见（含处理标志与观测结果）。"""
        return [{"id": row["id"], "block": row["block"], "x": row["x"],
                 "treated": row["treated"], "outcome": row["outcome"]} for row in rows]

    def score(self, rows, payload, metric_id, unit=None):
        """按区块（分析单元）计分：|ATE 估计 − 区块真实平均处理效应|。

        单元取值必须随调用传入：制品本身不声明区块，区块归属由控制器的执行循环决定，
        候选无法通过在制品里自报区块来挑选对自己有利的真值。
        """
        spec = self.metric(metric_id)
        self.validate_predictions(rows, payload, spec.name)
        if unit is None:
            raise ProtocolError("处理效应域包必须按分析单元（block）计分：缺少单元取值")
        members = [row for row in rows if row["block"] == unit]
        if not members:
            raise ProtocolError(f"制品对应的分析单元 {unit!r} 不在当前数据划分中")
        truth = sum(row["tau"] for row in members) / len(members)
        result = abs(payload["ate_estimate"] - truth)
        if not number(result):
            raise ProtocolError(f"{spec.name} 评估结果非有限值")
        if not spec.accepts(result):
            raise ProtocolError(
                f"{spec.name} 评估结果超出声明值域（{spec.range_text()}）: {result!r}")
        return result


PACK = register(TreatmentEffectV1())
