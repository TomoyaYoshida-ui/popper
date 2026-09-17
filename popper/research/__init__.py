"""popper.research（R0）· 研究契约、追加记录存储与预算总账。

每个实体只有一个权威写入方；非法状态转移直接拒绝；
预算预留与最终测试消费权限由可信内核持有，模型不可修改。
"""
from .contracts import (Decision, DesignStatus, ExperimentDesign, Hypothesis,
                        HypothesisStatus, Observation, RunStatus, Study,
                        StudyStatus, TransitionError)
from .budget import BudgetLedger
from .store import ResearchStore
from .actions import ActionProposal
from .controller import ResearchController
from .evaluation_service import IndependentEvaluator
from .models import DeepSeekResearchPolicy, EvidenceDrivenPolicy
from .revisions import CodeEdit, RevisionStore
from .workers import InputArtifact, JobSpec, LocalWorker, RemoteWorker, WorkerReceipt

__all__ = [
    "BudgetLedger", "Decision", "DesignStatus", "ExperimentDesign",
    "Hypothesis", "HypothesisStatus", "Observation", "ResearchStore",
    "RunStatus", "Study", "StudyStatus", "TransitionError", "ActionProposal",
    "ResearchController", "IndependentEvaluator", "DeepSeekResearchPolicy",
    "EvidenceDrivenPolicy", "CodeEdit", "RevisionStore", "InputArtifact",
    "JobSpec", "LocalWorker", "RemoteWorker", "WorkerReceipt",
]
