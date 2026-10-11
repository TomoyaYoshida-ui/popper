"""Typed v2 proposals: the model cannot supply scores or confirmation data."""
import json
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from ..core import ProtocolError


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    rationale: str = Field(min_length=1, max_length=8000)
    evidence_refs: list[str] = Field(default_factory=list)


class ProblemProposal(Proposal):
    kind: Literal['propose_problem']
    evidence_refs: list[str] = Field(min_length=1)
    question: str = Field(min_length=1, max_length=2000)
    motivation: str = Field(min_length=1, max_length=4000)
    explanations: list[str] = Field(min_length=2, max_length=2)
    discriminating_intervention: str = Field(min_length=1, max_length=2000)


class ExperimentProposal(Proposal):
    kind: Literal['run_experiment', 'repair_implementation']
    mechanism: str = Field(min_length=1, max_length=4000)
    intervention: str = Field(min_length=1, max_length=2000)
    config: dict
    source_code: str = Field(min_length=1, max_length=64000)
    parent_candidate_id: str | None = None


class RevisionProposal(Proposal):
    kind: Literal['revise_hypothesis']
    hypothesis_id: str
    mechanism: str = Field(min_length=1, max_length=4000)
    predictions: list[str] = Field(min_length=1, max_length=4)


class StopProposal(Proposal):
    kind: Literal['stop_planning']


Action = Annotated[ProblemProposal | ExperimentProposal | RevisionProposal | StopProposal,
                   Field(discriminator='kind')]
ADAPTER = TypeAdapter(Action)


def parse_action(value):
    try:
        return ADAPTER.validate_python(value)
    except ValidationError as failure:
        error=ProtocolError('Adaptive action contract rejected')
        allowed=set().union(*(set(c.model_fields) for c in (ProblemProposal,ExperimentProposal,RevisionProposal,StopProposal)))
        error.details=[{'path':'action.'+'.'.join(str(x) if x in allowed else '<field>' for x in e['loc']),
                        'rule':e['type']} for e in failure.errors(include_input=False)]
        raise error from None


def instructions():
    return ('Call one registered tool with action and optional assessment. Tool name must equal action.kind. '
            'First propose a testable research question, two substantive competing explanations and an intervention '
            'that could distinguish them; a mere improvement/not-improvement restatement is not a mechanism. '
            'Cite at least one supplied material ID in problem.evidence_refs (for example dataset-card). '
            'Then design experiments, implement model.py with the documented CLI, and cite observed evidence '
            'when revising explanations. Do not invent evidence, scores, statistical support or confirmation results. '
            'Use Python, numpy, pandas and scikit-learn already installed. Source cannot modify the evaluator. '
            'Your explanations remain unverified; the frozen primary prediction is relative MAE improvement >5%. '
            'Repairs must name a failed parent_candidate_id and consume the experiment budget. '
            'Revising either member versions the whole competing pair; retest via its parent_candidate_id. '
            'Stop when evidence or resources justify it.\n'
            + json.dumps(ADAPTER.json_schema(), ensure_ascii=False))
