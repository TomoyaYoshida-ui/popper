import copy
import pytest
from popper.core import ProtocolError
from popper.research.claim_projection import project_claim


def records(state='inconclusive', interval=(-.1067, .1876)):
    claim = dict(study_id='S-test', hypothesis_id='H-candidate', hypothesis_version=1,
                 observation_refs=['OBS-control', 'OBS-candidate'], metric='mae',
                 oriented_delta=.4029, min_meaningful_effect=.05, status='supported_in_scope')
    judgment = dict(study_id='S-test', hypothesis_id='H-candidate', hypothesis_version=1,
                    scope='confirmation', observation_refs=claim['observation_refs'],
                    judgment_id='J-test', new_judgment=state,
                    evidence=dict(mean_effect=.04936, ci95=interval, quality='valid', unit='ratio',
                                  hypothesis_predictions={'H-candidate': dict(operator='gt', threshold=.05)}))
    return claim, judgment


def test_absolute_improvement_does_not_become_relative_support():
    claim, judgment = records()
    original = copy.deepcopy(claim)
    result = project_claim(claim, [judgment])
    assert result['scientific_status'] == 'inconclusive'
    assert result['effect_value'] == .04936
    assert result['descriptive_status'] == 'supported_in_scope'
    assert claim == original


@pytest.mark.parametrize('field,value', [('hypothesis_version', 2), ('scope', 'dev'),
                                       ('study_id', 'S-other'), ('observation_refs', ['OBS-other'])])
def test_judgment_must_bind_exact_confirmation_version(field, value):
    claim, judgment = records()
    judgment[field] = value
    assert project_claim(claim, [judgment])['scientific_status'] == 'not_evaluated'


def test_interval_tampering_is_rejected():
    claim, judgment = records(state='supported')
    with pytest.raises(ProtocolError):
        project_claim(claim, [judgment])


def test_supported_interval_and_insufficient_units():
    claim, judgment = records(state='supported', interval=(.1188, .2990))
    assert project_claim(claim, [judgment])['scientific_status'] == 'supported'
    judgment['evidence']['quality'] = 'insufficient_units'
    judgment['new_judgment'] = 'inconclusive'
    assert project_claim(claim, [judgment])['scientific_status'] == 'inconclusive'
