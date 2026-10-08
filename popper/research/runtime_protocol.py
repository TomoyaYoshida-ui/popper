"""One typed contract for model proposals, their schema and validation feedback.

These values are proposals, not execution authorizations or scientific facts.
Runtime identity, versions, budget and independently verified evidence remain
owned by the research controller. An invalid assessment does not invalidate a
separately parsed action.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, StringConstraints,
                      TypeAdapter, ValidationError)

from ..core import ProtocolError


NonBlankText = Annotated[
    str, StringConstraints(strict=True, min_length=1, pattern=r"\S")
]
EvidenceRefs = list[NonBlankText]


class RuntimeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExecuteCandidate(RuntimeProposal):
    """Propose one registered candidate; the controller authorizes execution."""

    kind: Literal["execute_candidate"]
    candidate_id: NonBlankText = Field(
        description="Registered execution candidate ID from the current context."
    )
    rationale: NonBlankText = Field(description="Reason for the proposed experiment.")
    evidence_refs: EvidenceRefs = Field(
        description="References to available evidence; an initial proposal may use an empty list."
    )


class ReviseHypothesis(RuntimeProposal):
    """Propose an appended hypothesis version, never an edit to evaluated facts."""

    kind: Literal["revise_hypothesis"]
    hypothesis_id: NonBlankText = Field(
        description="Registered hypothesis ID whose next version is proposed."
    )
    mechanism: NonBlankText = Field(description="Proposed explanatory mechanism.")
    applicability: NonBlankText = Field(
        description="Proposed applicability expressed using registered scope semantics."
    )
    predictions: Annotated[list[NonBlankText], Field(min_length=1)] = Field(
        description="Nonempty proposed predictions for the new hypothesis version."
    )
    rationale: NonBlankText = Field(description="Reason for revising the hypothesis.")
    evidence_refs: EvidenceRefs = Field(description="References justifying the revision proposal.")


class StopPlanning(RuntimeProposal):
    """Stop proposing experiments; deterministic rules select any final candidate."""

    kind: Literal["stop_planning"]
    rationale: NonBlankText = Field(description="Reason to stop proposing further experiments.")
    evidence_refs: EvidenceRefs = Field(description="References informing the stopping proposal.")


RuntimeAction = Annotated[
    ExecuteCandidate | ReviseHypothesis | StopPlanning,
    Field(discriminator="kind"),
]


class AgentAssessment(RuntimeProposal):
    """Unverified model interpretation; it never overwrites evaluator judgments."""

    subject_hypothesis_id: NonBlankText = Field(
        description="Registered hypothesis being interpreted, independently of the action target."
    )
    hypothesis_version: Annotated[int, Field(strict=True, gt=0)] = Field(
        description="Positive integer version of the hypothesis being interpreted."
    )
    scope_id: NonBlankText = Field(description="Registered evidence scope being interpreted.")
    judgment: Literal["supported", "refuted", "inconclusive", "not_evaluated"] = Field(
        description="Unverified model assessment; the independent evaluator owns the scientific judgment."
    )
    rationale: NonBlankText = Field(description="Interpretation of the available verified evidence.")
    evidence_refs: EvidenceRefs = Field(description="References supporting this interpretation.")


class DecisionEnvelope(RuntimeProposal):
    """Return a proposal and, optionally, a separate unverified interpretation."""

    action: RuntimeAction
    assessment: AgentAssessment | None = Field(
        default=None,
        description="Optional unverified interpretation. It is separate from any execution or stopping proposal.",
    )


_ACTION_ADAPTER = TypeAdapter(RuntimeAction)
_SAFE_PATH_COMPONENTS = frozenset(
    {"action", "assessment", "execute_candidate", "revise_hypothesis", "stop_planning"}
    | {name for model in (ExecuteCandidate, ReviseHypothesis, StopPlanning,
                          AgentAssessment, DecisionEnvelope)
       for name in model.model_fields}
)


def _protocol_error(details: list[dict[str, str]]) -> ProtocolError:
    """Expose field paths and machine-readable rules, without echoing input."""
    exc = ProtocolError("Runtime protocol validation failed: " + "; ".join(
        f"{item['path']}: {item['rule']}" for item in details
    ))
    exc.details = details
    return exc


def _safe_path(location: tuple[Any, ...]) -> str:
    components = []
    for part in location:
        if type(part) is int:
            components.append(f"[{part}]")
        elif isinstance(part, str) and part in _SAFE_PATH_COMPONENTS:
            components.append(("." if components else "") + part)
        else:
            # A forbidden field name can itself contain a secret or arbitrary text.
            components.append(("." if components else "") + "<unknown>")
    return "".join(components) or "$"


def _validation_error(exc: ValidationError) -> ProtocolError:
    return _protocol_error([
        {"path": _safe_path(tuple(error["loc"])), "rule": error["type"]}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ])


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _protocol_error([{"path": "$", "rule": "duplicate_field"}])
        result[key] = value
    return result


def _payload(payload: Any) -> Any:
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            return json.loads(payload, object_pairs_hook=_object_pairs)
        except (ValueError, UnicodeDecodeError) as exc:
            if isinstance(exc, ProtocolError):
                raise
            raise _protocol_error([{"path": "$", "rule": "json_invalid"}]) from None
    return payload


def parse_action(payload: Any) -> ExecuteCandidate | ReviseHypothesis | StopPlanning:
    """Parse an independent action object (or JSON), without reading assessment."""
    try:
        return _ACTION_ADAPTER.validate_python(_payload(payload))
    except ValidationError as exc:
        raise _validation_error(exc) from None


def parse_assessment(payload: Any) -> AgentAssessment | None:
    """Parse an independent optional assessment; no scores or CIs are accepted."""
    payload = _payload(payload)
    if payload is None:
        return None
    try:
        return AgentAssessment.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error(exc) from None


def parse_decision(payload: Any) -> DecisionEnvelope:
    """Strict envelope parsing when both parts must be valid together."""
    try:
        return DecisionEnvelope.model_validate(_payload(payload))
    except ValidationError as exc:
        raise _validation_error(exc) from None


def decision_schema() -> dict[str, Any]:
    """Generate the exact wire schema from the models used for parsing."""
    return DecisionEnvelope.model_json_schema(mode="validation")


def decision_instructions() -> str:
    """Use the same typed schema and descriptions as validation, not a second contract."""
    return (
        "Return one JSON object conforming to the following schema. "
        "The field descriptions define action and assessment semantics. "
        "Use registered IDs and evidence references from the provided context.\n"
        + json.dumps(decision_schema(), ensure_ascii=False, sort_keys=True)
    )


__all__ = [
    "AgentAssessment", "DecisionEnvelope", "ExecuteCandidate", "ReviseHypothesis",
    "RuntimeAction", "StopPlanning", "decision_instructions", "decision_schema",
    "parse_action", "parse_assessment", "parse_decision",
]
