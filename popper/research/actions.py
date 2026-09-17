"""研究控制器允许的动作与严格校验。"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core import ProtocolError


RUN_EXPERIMENT = "run_experiment"
ADD_CONTROL = "add_control"
REQUEST_CONFIRMATION = "request_confirmation"
REQUEST_SCOPE_BOUNDARY_CONFIRMATION = "request_scope_boundary_confirmation"
STOP = "stop"
CONCLUDE_SCOPE_BOUNDARY = "conclude_scope_boundary"
IMPLEMENT_REVISION = "implement_revision"
REPAIR_IMPLEMENTATION = "repair_implementation"

ALLOWED_ACTIONS = {RUN_EXPERIMENT, ADD_CONTROL, REQUEST_CONFIRMATION, STOP,
                   REQUEST_SCOPE_BOUNDARY_CONFIRMATION, CONCLUDE_SCOPE_BOUNDARY,
                   IMPLEMENT_REVISION, REPAIR_IMPLEMENTATION}


@dataclass(frozen=True)
class ActionProposal:
    kind: str
    rationale: str
    hypothesis_id: str | None = None
    alternatives: tuple = field(default_factory=tuple)
    source: str = "evidence_policy"
    model: str | None = None

    def __post_init__(self):
        if self.kind not in ALLOWED_ACTIONS:
            raise ProtocolError(f"未知研究动作: {self.kind}")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ProtocolError("研究动作必须给出可审计理由")
        if self.kind in {RUN_EXPERIMENT, ADD_CONTROL, REQUEST_CONFIRMATION,
                         IMPLEMENT_REVISION, REPAIR_IMPLEMENTATION} \
                and not self.hypothesis_id:
            raise ProtocolError(f"{self.kind} 必须指定 hypothesis_id")
        if not isinstance(self.alternatives, tuple):
            raise ProtocolError("alternatives 必须为 tuple")

    def as_dict(self):
        return {"kind": self.kind, "hypothesis_id": self.hypothesis_id,
                "rationale": self.rationale, "alternatives": list(self.alternatives),
                "source": self.source, "model": self.model}
