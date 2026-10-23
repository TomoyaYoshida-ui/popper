"""Single scientific product entry for registered research and Adaptive fixtures.

This is the physical consolidation of *product entry*, not a line-by-line merge of
ResearchController, AdaptiveResearchRuntime, and Orchestrator into one class:

- Registered science → ``ResearchController`` (+ ``ResearchRuntime`` for planning)
- Adaptive descriptive fixture → ``AdaptiveResearchRuntime`` (evaluation publishes
  only through ``scientific_execution`` under evidence authorization)
- Campaign ``Orchestrator`` is **not** a scientific runtime; confirm must use
  Controller with ``research-runtime/v1``

Callers that need a scientific session should open it here instead of constructing
Adaptive/Controller ad hoc when a shared entry is available.
"""
from __future__ import annotations

from pathlib import Path

from ..core import ProtocolError


class ScientificRuntime:
    """Unified opener for scientific sessions (registered or adaptive fixture)."""

    __slots__ = ('kind', 'controller', 'adaptive', 'run_dir')

    def __init__(self, kind, *, controller=None, adaptive=None, run_dir=None):
        if kind not in {'registered', 'adaptive'}:
            raise ProtocolError('ScientificRuntime.kind 只能是 registered 或 adaptive')
        if kind == 'registered' and controller is None:
            raise ProtocolError('registered 会话需要 ResearchController')
        if kind == 'adaptive' and adaptive is None:
            raise ProtocolError('adaptive 会话需要 AdaptiveResearchRuntime')
        self.kind = kind
        self.controller = controller
        self.adaptive = adaptive
        self.run_dir = Path(run_dir) if run_dir is not None else None

    @classmethod
    def open_registered(cls, run_dir, **controller_kwargs):
        from .controller import ResearchController
        run_dir = Path(run_dir)
        controller = ResearchController(run_dir, **controller_kwargs)
        return cls('registered', controller=controller, run_dir=run_dir)

    @classmethod
    def open_adaptive(cls, directory, contract, data_paths, baseline_source, **kwargs):
        from .adaptive_runtime import AdaptiveResearchRuntime
        adaptive = AdaptiveResearchRuntime(
            directory, contract, data_paths, baseline_source, **kwargs)
        return cls('adaptive', adaptive=adaptive, run_dir=Path(directory))

    def close(self):
        if self.controller is not None:
            self.controller.close()
        # AdaptiveResearchRuntime has no close(); store lifetime is owned by the fixture.


def campaign_is_not_scientific_runtime():
    """Documentation hook / test anchor: Orchestrator must not claim scientific authority."""
    return {
        'scientific_runtime': False,
        'orchestration_only': True,
        'scientific_authority': 'research_store',
        'confirm_requires': 'research-runtime/v1',
    }
