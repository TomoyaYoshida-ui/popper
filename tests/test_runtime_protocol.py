"""Regression tests for the single model proposal contract."""
import copy
import json

import pytest
from pydantic import ValidationError

from popper.core import ProtocolError
from popper.research.runtime_protocol import (
    AgentAssessment, DecisionEnvelope, ExecuteCandidate, ReviseHypothesis,
    StopPlanning, decision_instructions, decision_schema,
    parse_action, parse_assessment, parse_decision,
)


def execute():
    return {"kind": "execute_candidate", "candidate_id": "C-17",
            "rationale": "Compare the registered candidate.", "evidence_refs": []}


def stop():
    return {"kind": "stop_planning", "rationale": "Sufficient development evidence.",
            "evidence_refs": ["O-3"]}


def revision():
    return {"kind": "revise_hypothesis", "hypothesis_id": "H-17",
            "mechanism": "Transfer only within the observed scope.",
            "applicability": "S-2", "predictions": ["Registered relative improvement."],
            "rationale": "Independent evidence narrows applicability.", "evidence_refs": ["O-3"]}


def assessment():
    return {"subject_hypothesis_id": "H-17", "hypothesis_version": 2,
            "scope_id": "S-2", "judgment": "inconclusive",
            "rationale": "Interval overlaps the frozen threshold.", "evidence_refs": ["O-3"]}


@pytest.mark.parametrize("factory,expected", [
    (execute, ExecuteCandidate), (revision, ReviseHypothesis), (stop, StopPlanning),
])
def test_actions_have_separate_types_and_round_trip(factory, expected):
    payload = factory()
    parsed = parse_action(payload)
    assert type(parsed) is expected
    assert parsed.model_dump(mode="json") == payload
    assert parse_action(json.dumps(payload)) == parsed
    with pytest.raises(ValidationError):
        parsed.rationale = "Mutated after authorization."


def test_stop_and_assessment_subject_are_independent():
    payload = {"action": stop(), "assessment": assessment()}
    parsed = parse_decision(payload)
    assert isinstance(parsed, DecisionEnvelope)
    assert parsed.assessment.subject_hypothesis_id == "H-17"
    assert "candidate_id" not in parsed.action.model_dump()
    assert "hypothesis_id" not in parsed.action.model_dump()
    assert parse_decision({"action": stop()}).assessment is None
    assert parse_assessment(None) is None
    assert parse_assessment("null") is None


@pytest.mark.parametrize("field", ["candidate_id", "hypothesis_id", "final_candidate"])
def test_stopping_cannot_select_or_execute_a_candidate(field):
    payload = stop()
    payload[field] = "C-17"
    with pytest.raises(ProtocolError) as caught:
        parse_action(payload)
    safe_field = field if field in {"candidate_id", "hypothesis_id"} else "<unknown>"
    assert caught.value.details == [{"path": f"stop_planning.{safe_field}", "rule": "extra_forbidden"}]


@pytest.mark.parametrize("factory,field,value", [
    (execute, "candidate_id", 17), (execute, "evidence_refs", "O-3"),
    (execute, "evidence_refs", [17]), (execute, "rationale", "   \n\t"),
    (revision, "predictions", []), (revision, "predictions", [" "]),
    (revision, "predictions", "prediction"), (revision, "mechanism", True),
    (assessment, "hypothesis_version", "2"), (assessment, "hypothesis_version", 2.0),
    (assessment, "hypothesis_version", True), (assessment, "hypothesis_version", 0),
    (assessment, "hypothesis_version", -1), (assessment, "judgment", "supported_by_model"),
])
def test_strict_types_and_nonblank_text(factory, field, value):
    payload = factory()
    payload[field] = value
    parser = parse_assessment if factory is assessment else parse_action
    with pytest.raises(ProtocolError) as caught:
        parser(payload)
    assert caught.value.details
    assert any(field in error["path"] for error in caught.value.details)


@pytest.mark.parametrize("field", ["score", "ci", "confidence_interval", "verified", "fact"])
def test_assessment_cannot_write_scientific_facts(field):
    payload = assessment()
    payload[field] = 0.1
    with pytest.raises(ProtocolError) as caught:
        parse_assessment(payload)
    assert caught.value.details == [{"path": "<unknown>", "rule": "extra_forbidden"}]


def test_invalid_assessment_does_not_prevent_independent_valid_action():
    payload = {"action": execute(), "assessment": {**assessment(), "score": 0.5}}
    with pytest.raises(ProtocolError):
        parse_decision(payload)
    assert isinstance(parse_action(payload["action"]), ExecuteCandidate)
    with pytest.raises(ProtocolError):
        parse_assessment(payload["assessment"])


def test_unknown_envelope_fields_and_missing_discriminator_are_rejected():
    with pytest.raises(ProtocolError) as caught:
        parse_decision({"action": stop(), "budget": 100})
    assert caught.value.details == [{"path": "<unknown>", "rule": "extra_forbidden"}]
    payload = execute()
    del payload["kind"]
    with pytest.raises(ProtocolError) as missing:
        parse_action(payload)
    assert missing.value.details == [{"path": "$", "rule": "union_tag_not_found"}]


@pytest.mark.parametrize("payload", [
    '{"kind":"stop_planning","kind":"execute_candidate"}',
    '{"action":{"kind":"stop_planning"},"action":null}',
])
def test_duplicate_json_fields_are_not_silently_overwritten(payload):
    with pytest.raises(ProtocolError) as caught:
        parse_decision(payload)
    assert caught.value.details == [{"path": "$", "rule": "duplicate_field"}]


def test_validation_errors_do_not_echo_secret_values_keys_or_invalid_json():
    secret = "confidential_fixture_value"
    payload = execute()
    payload[secret] = {"nested": secret}
    payload["candidate_id"] = [secret]
    with pytest.raises(ProtocolError) as caught:
        parse_action(payload)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value.details)
    assert {"path": "execute_candidate.candidate_id", "rule": "string_type"} in caught.value.details
    with pytest.raises(ProtocolError) as invalid_tag:
        parse_action({"kind": secret})
    assert secret not in str(invalid_tag.value)
    with pytest.raises(ProtocolError) as invalid_json:
        parse_decision('{"action":' + secret)
    assert invalid_json.value.details == [{"path": "$", "rule": "json_invalid"}]
    assert secret not in str(invalid_json.value)


def test_schema_is_generated_from_the_exact_parser_models_and_drives_instructions():
    schema = decision_schema()
    assert schema == DecisionEnvelope.model_json_schema(mode="validation")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["action"]["discriminator"]["propertyName"] == "kind"
    for name in ("ExecuteCandidate", "ReviseHypothesis", "StopPlanning", "AgentAssessment"):
        assert schema["$defs"][name]["additionalProperties"] is False
    stop_schema = schema["$defs"]["StopPlanning"]
    assert set(stop_schema["properties"]) == {"kind", "rationale", "evidence_refs"}
    assert set(stop_schema["required"]) == set(stop_schema["properties"])
    assert schema["$defs"]["ReviseHypothesis"]["properties"]["predictions"]["minItems"] == 1
    assert schema["$defs"]["AgentAssessment"]["properties"]["hypothesis_version"]["exclusiveMinimum"] == 0
    instructions = decision_instructions()
    assert json.loads(instructions.split("\n", 1)[1]) == schema
    assert "deterministic rules select any final candidate" in instructions
    assert "independently of the action target" in instructions
    altered = copy.deepcopy(schema)
    altered["properties"].clear()
    assert decision_schema()["properties"]


def test_envelope_round_trip_includes_separate_unverified_assessment():
    envelope = parse_decision({"action": revision(), "assessment": assessment()})
    assert isinstance(envelope.assessment, AgentAssessment)
    assert parse_decision(envelope.model_dump_json()) == envelope
    assert parse_assessment(envelope.assessment.model_dump()) == envelope.assessment
