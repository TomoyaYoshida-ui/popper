"""研究契约层（R0）· 实体 schema 与状态机。

每个实体只有一个权威写入方；状态转移非法即抛 TransitionError。
schema 合法只是入口，不替代语义与科学设计检查。

- Study：研究问题、范围、授权、全局预算、研究族 ID 与数据暴露记录。
- Hypothesis：机制、适用条件、预测、反证条件、替代解释；修订只能新增版本。
- ExperimentDesign：假设 ID、干预/对照、基线、指标、分析单位、数据划分、种子、
  最小有意义效应、分析与停止规则；执行前必须冻结，冻结后不可变。
- Observation：版本化计分器从运行产物计算的事实及不确定性、作用范围。
- Decision：可见状态版本、观察引用、所选动作、备选解释、简洁理由、预算请求。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Optional

from ..core import ProtocolError


class TransitionError(ProtocolError):
    """非法状态转移。"""


# ---- 执行与研究判断状态（不再共用一个「完成」状态） ----

class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    INFRA_FAILED = "infrastructure_failed"
    IMPLEMENTATION_FAILED = "implementation_failed"
    CANCELLED = "cancelled"


class HypothesisStatus(str, Enum):
    UNTESTED = "untested"
    SUPPORTED_IN_SCOPE = "supported_in_scope"
    CONTRADICTED_IN_SCOPE = "contradicted_in_scope"
    INCONCLUSIVE = "inconclusive"
    RETIRED = "retired"


class DesignStatus(str, Enum):
    DRAFT = "draft"
    FROZEN = "frozen"


class StudyStatus(str, Enum):
    ACTIVE = "active"
    CONCLUDED = "concluded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED_EXTERNAL = "blocked_external"
    INTEGRITY_FAILED = "integrity_failed"


_RUN_TERMINAL = {RunStatus.SUCCEEDED, RunStatus.INFRA_FAILED,
                 RunStatus.IMPLEMENTATION_FAILED, RunStatus.CANCELLED}

RUN_TRANSITIONS = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: set(_RUN_TERMINAL),
    RunStatus.SUCCEEDED: set(),
    RunStatus.INFRA_FAILED: set(),
    RunStatus.IMPLEMENTATION_FAILED: set(),
    RunStatus.CANCELLED: set(),
}

# 修复工程错误产生新 CodeRevision 与新 Run，不在旧 Run 上继续流转。
HYPOTHESIS_TRANSITIONS = {
    HypothesisStatus.UNTESTED: {HypothesisStatus.SUPPORTED_IN_SCOPE,
                                HypothesisStatus.CONTRADICTED_IN_SCOPE,
                                HypothesisStatus.INCONCLUSIVE,
                                HypothesisStatus.RETIRED},
    HypothesisStatus.SUPPORTED_IN_SCOPE: {HypothesisStatus.CONTRADICTED_IN_SCOPE,
                                          HypothesisStatus.INCONCLUSIVE,
                                          HypothesisStatus.RETIRED},
    HypothesisStatus.CONTRADICTED_IN_SCOPE: {HypothesisStatus.INCONCLUSIVE,
                                             HypothesisStatus.RETIRED},
    HypothesisStatus.INCONCLUSIVE: {HypothesisStatus.SUPPORTED_IN_SCOPE,
                                    HypothesisStatus.CONTRADICTED_IN_SCOPE,
                                    HypothesisStatus.RETIRED},
    HypothesisStatus.RETIRED: set(),
}

DESIGN_TRANSITIONS = {
    DesignStatus.DRAFT: {DesignStatus.FROZEN},
    DesignStatus.FROZEN: set(),
}

_STUDY_TERMINAL = {StudyStatus.CONCLUDED, StudyStatus.BUDGET_EXHAUSTED,
                   StudyStatus.BLOCKED_EXTERNAL, StudyStatus.INTEGRITY_FAILED}

STUDY_TRANSITIONS = {
    StudyStatus.ACTIVE: set(_STUDY_TERMINAL),
    StudyStatus.CONCLUDED: set(),
    StudyStatus.BUDGET_EXHAUSTED: set(),
    StudyStatus.BLOCKED_EXTERNAL: set(),
    StudyStatus.INTEGRITY_FAILED: set(),
}

# 会话终态：任务有结论、实验成功和科学假设成立是三个不同事实。
VALID_DIRECTIONS = ("min", "max")


def _require_transition(entity, current, target, table):
    allowed = table.get(current, set())
    if target not in allowed:
        raise TransitionError(
            f"非法状态转移: {entity} {getattr(current, 'value', current)!r} -> "
            f"{getattr(target, 'value', target)!r}（允许: {sorted(v.value for v in allowed)}）")


def validate_run_transition(current, target):
    _require_transition("run", current, target, RUN_TRANSITIONS)


def validate_hypothesis_transition(current, target):
    _require_transition("hypothesis", current, target, HYPOTHESIS_TRANSITIONS)


def validate_design_transition(current, target):
    _require_transition("design", current, target, DESIGN_TRANSITIONS)


def validate_study_transition(current, target):
    _require_transition("study", current, target, STUDY_TRANSITIONS)


def validate_hypothesis_revision(current):
    """修订产生新版本并重新进入 untested：这是独立于状态机的合法通道。

    两条修订路径（公开 revise_hypothesis 与反思通道）必须用同一张守卫，
    否则其中一条会绕过「已退休假设不可修订」。
    """
    if current == HypothesisStatus.RETIRED:
        raise TransitionError("已退休假设不能再修订")


# ---- 实体（不可变快照；修订 = 新版本） ----

@dataclass(frozen=True)
class Study:
    study_id: str
    family_id: str
    question: str
    scope: str
    status: StudyStatus = StudyStatus.ACTIVE
    data_exposure: tuple = field(default_factory=tuple)
    budget_cap: Optional[float] = None

    def __post_init__(self):
        if not self.study_id or not self.family_id:
            raise ProtocolError("study_id/family_id 不能为空")
        if self.budget_cap is None:
            raise ProtocolError("study 必须配置预算硬上限；未配置上限不能当作无限授权")
        if (isinstance(self.budget_cap, bool) or not isinstance(self.budget_cap, (int, float))
                or not math.isfinite(self.budget_cap) or self.budget_cap < 0):
            raise ProtocolError("budget_cap 不能为负")
        if not isinstance(self.status, StudyStatus):
            raise ProtocolError(f"status 必须是 StudyStatus: {self.status!r}")


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    study_id: str
    version: int
    parent_version: Optional[int]
    mechanism: str
    applicability: str
    predictions: tuple = field(default_factory=tuple)
    falsification: tuple = field(default_factory=tuple)
    alternatives: tuple = field(default_factory=tuple)
    status: HypothesisStatus = HypothesisStatus.UNTESTED

    def __post_init__(self):
        if not self.hypothesis_id or not self.study_id or not self.mechanism:
            raise ProtocolError("hypothesis_id/study_id/mechanism 不能为空")
        if self.version < 1:
            raise ProtocolError("version 必须 ≥ 1")
        if self.parent_version is not None and not (0 <= self.parent_version < self.version):
            raise ProtocolError("parent_version 必须小于当前 version")
        if not isinstance(self.status, HypothesisStatus):
            raise ProtocolError(f"status 必须是 HypothesisStatus: {self.status!r}")


@dataclass(frozen=True)
class ExperimentDesign:
    design_id: str
    hypothesis_id: str
    interventions: tuple = field(default_factory=tuple)
    control: str = ""
    baseline: str = ""
    metric: str = ""
    scorer_id: str = ""
    metric_direction: str = "max"
    analysis_unit: str = ""
    splits: tuple = field(default_factory=tuple)
    seeds: tuple = field(default_factory=tuple)
    min_meaningful_effect: float = 0.0
    status: DesignStatus = DesignStatus.DRAFT

    def __post_init__(self):
        if not self.design_id or not self.hypothesis_id:
            raise ProtocolError("design_id/hypothesis_id 不能为空")
        if self.metric_direction not in VALID_DIRECTIONS:
            raise ProtocolError(f"metric_direction 必须为 {'/'.join(VALID_DIRECTIONS)}")
        if not self.metric or not self.scorer_id:
            raise ProtocolError("metric/scorer_id 不能为空")
        if not self.analysis_unit:
            raise ProtocolError("analysis_unit 不能为空")
        if (isinstance(self.min_meaningful_effect, bool)
                or not isinstance(self.min_meaningful_effect, (int, float))
                or not math.isfinite(self.min_meaningful_effect)
                or self.min_meaningful_effect < 0):
            raise ProtocolError("min_meaningful_effect 不能为负")
        if not isinstance(self.status, DesignStatus):
            raise ProtocolError(f"status 必须是 DesignStatus: {self.status!r}")


@dataclass(frozen=True)
class Observation:
    observation_id: str
    run_id: str
    scorer_id: str
    value: float
    unit: str = ""
    uncertainty: Optional[float] = None
    scope: str = ""
    artifact_id: Optional[str] = None
    artifact_sha256: Optional[str] = None
    selector: Optional[str] = None
    evaluator_service_hash: Optional[str] = None
    trust: str = "logical_evaluation_service"

    def __post_init__(self):
        if not self.observation_id or not self.run_id or not self.scorer_id:
            raise ProtocolError("observation_id/run_id/scorer_id 不能为空")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
            raise ProtocolError("observation value 必须是有限数值")
        if (self.uncertainty is not None
                and (isinstance(self.uncertainty, bool)
                     or not isinstance(self.uncertainty, (int, float))
                     or not math.isfinite(self.uncertainty)
                     or self.uncertainty < 0)):
            raise ProtocolError("uncertainty 不能为负")
        supplied = (self.artifact_id, self.artifact_sha256, self.selector)
        if any(value is not None for value in supplied) and not all(supplied):
            raise ProtocolError("Observation 制品绑定必须同时提供 artifact_id/sha256/selector")


@dataclass(frozen=True)
class Decision:
    decision_id: str
    study_id: str
    state_version: int
    observation_refs: tuple = field(default_factory=tuple)
    action: str = ""
    alternatives: tuple = field(default_factory=tuple)
    rationale: str = ""
    budget_request: float = 0.0
    actor: str = "research_store"
    model: Optional[str] = None

    def __post_init__(self):
        if not self.decision_id or not self.study_id or not self.action:
            raise ProtocolError("decision_id/study_id/action 不能为空")
        if self.state_version < 1:
            raise ProtocolError("state_version 必须 ≥ 1")
        if (isinstance(self.budget_request, bool)
                or not isinstance(self.budget_request, (int, float))
                or not math.isfinite(self.budget_request)
                or self.budget_request < 0):
            raise ProtocolError("budget_request 不能为负")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ProtocolError("Decision actor 不能为空")
