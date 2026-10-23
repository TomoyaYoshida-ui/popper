"""Chat numeric conclusions must cite a stored observation or literature id."""
import json

from popper.chat import NUMERIC_CLAIM_REFUSAL, gate_numeric_claims


def test_complete_json_payload_is_not_treated_as_prose_claim():
    payload = {
        "kind": "oscillator_hypotheses_v1",
        "initial_prediction": {
            "instruction": "approve execution",
            "prediction": {"error": [0.01, 0.04], "confidence": "unverified"},
        },
    }
    body = json.dumps(payload)
    assert gate_numeric_claims(body) == body
    fenced = "```json\n" + body + "\n```"
    assert gate_numeric_claims(fenced) == fenced


def test_numeric_conclusion_without_identifier_is_refused_and_prose_remains():
    text = '机制仍然开放。准确率是 91%。候选尚未执行。'
    result = gate_numeric_claims(text, observation_ids=(), literature_ids=())
    assert '机制仍然开放' in result
    assert '候选尚未执行' in result
    assert '91%' not in result
    assert NUMERIC_CLAIM_REFUSAL in result


def test_measurement_interval_and_effect_without_identifier_are_refused():
    samples = [
        '开发集 MAE 是 0.12。',
        '95% 区间是 [0.1, 0.4]。',
        'p < 0.05，所以可以认为有效。',
        '相对基线改善 4。',
    ]
    for sample in samples:
        result = gate_numeric_claims(sample, observation_ids=['obs_real'], literature_ids=['LIT-real'])
        assert NUMERIC_CLAIM_REFUSAL in result
        assert '0.12' not in result
        assert '0.05' not in result
        assert '[0.1, 0.4]' not in result


def test_numeric_conclusion_with_known_observation_or_literature_id_is_kept():
    observation = '开发集 MAE 是 0.12，见 obs_real。'
    literature = '近邻报告改善 4（LIT-real）。'
    assert gate_numeric_claims(
        observation, observation_ids=['obs_real'], literature_ids=()) == observation
    assert gate_numeric_claims(
        literature, observation_ids=(), literature_ids=['LIT-real']) == literature
    forged = gate_numeric_claims(
        '准确率是 91%（obs_missing）。', observation_ids=['obs_real'], literature_ids=['LIT-real'])
    assert '91%' not in forged
    assert NUMERIC_CLAIM_REFUSAL in forged
