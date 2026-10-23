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


def test_sentence_changes_when_the_interval_changes():
    claim, judgment = records(state='inconclusive', interval=(-.1067, .1876))
    first = project_claim(claim, [judgment])
    claim, judgment = records(state='inconclusive', interval=(.01, .2))
    second = project_claim(claim, [judgment])
    assert first['compiled_sentence'] != second['compiled_sentence']
    assert first['sentence_sha256'] != second['sentence_sha256']
    assert '这不建立机制' in first['compiled_sentence']
    assert 'artifact' not in first['compiled_sentence']


def test_unmatched_claim_keeps_the_fixed_sentence():
    claim, judgment = records()
    judgment['scope'] = 'dev'
    result = project_claim(claim, [judgment])
    assert result['scientific_status'] == 'not_evaluated'
    assert result['compiled_sentence'] == '尚无独立科学判断。这不建立机制。'
    assert result['descriptive_status'] == 'supported_in_scope'


def test_split_seed_mae_interval_is_not_scientific_support():
    from popper.research.search_session import bootstrap_interval
    summary = bootstrap_interval([2.0, 2.2, 1.8, 2.1, 1.9])
    claim, judgment = records(state='inconclusive')
    judgment['evidence'] = {}
    original = copy.deepcopy(claim)
    result = project_claim(claim, [judgment], observations=[], repeat_values=[11],
                           split_confirmation=summary)
    assert result['scientific_status'] == 'inconclusive'
    assert result['ci95'] is None
    assert claim == original
    assert claim['min_meaningful_effect'] == 0.05
    assert '断言获得独立证据支持' not in result['compiled_sentence']
    assert '登记预测获得独立证据支持' not in result['compiled_sentence']
    assert '这不建立机制' in result['compiled_sentence']


def test_one_split_seed_does_not_invent_an_interval():
    claim, judgment = records(state='inconclusive')
    judgment['evidence'] = {}
    summary = {'n_seeds': 1, 'ci95': None, 'mean_effect': None}
    result = project_claim(claim, [judgment], repeat_values=[11], split_confirmation=summary)
    assert result['scientific_status'] == 'inconclusive'
    assert result['ci95'] is None
    assert '支持' not in result['compiled_sentence']


def test_sealed_confirmation_rows_do_not_count_as_split_seeds():
    claim, judgment = records(state='inconclusive')
    judgment['evidence'] = {}
    summary = {'schema': 'popper.confirmation-row-ci/v1', 'unit': 'confirmation_row',
               'n_rows': 4, 'mean_effect': 0.0, 'ci95': [-1.0, 1.2],
               'baseline': 10.0, 'candidate': 10.0, 'interval_reason': None}
    result = project_claim(claim, [judgment], observations=[], split_confirmation=summary)
    assert result['scientific_status'] == 'inconclusive'
    assert '确认行数 4' in result['compiled_sentence']
    assert '种子数' not in result['compiled_sentence']
    assert '支持' not in result['compiled_sentence']
    assert '这不建立机制' in result['compiled_sentence']


def test_single_seed_confirmation_reports_scores_without_calling_it_support():
    claim, judgment = records(state='inconclusive')
    judgment['evidence'] = {}
    observations = [
        {'observation_id': 'OBS-dev-base', 'hypothesis_id': 'H-control', 'scope': 'dev', 'value': 10.8},
        {'observation_id': 'OBS-dev-cand', 'hypothesis_id': 'H-candidate', 'scope': 'dev', 'value': 1.2},
        {'observation_id': 'OBS-control', 'hypothesis_id': 'H-control', 'scope': 'confirmation', 'value': 11.0},
        {'observation_id': 'OBS-candidate', 'hypothesis_id': 'H-candidate', 'scope': 'confirmation', 'value': 1.5},
    ]
    result = project_claim(claim, [judgment], observations, [11])
    assert result['scientific_status'] == 'inconclusive'
    assert result['dev_baseline'] == 10.8
    assert result['dev_candidate'] == 1.2
    assert result['confirmation_baseline'] == 11.0
    assert result['confirmation_candidate'] == 1.5
    assert result['n_seeds'] == 1
    assert result['interval_reason'] == '只有 1 个种子，不能形成区间'
    assert '开发分' in result['compiled_sentence']
    assert '确认分' in result['compiled_sentence']
    assert '种子数 1' in result['compiled_sentence']
    assert '这不建立机制' in result['compiled_sentence']
    assert '支持' not in result['compiled_sentence']


def _scores():
    return [
        {'observation_id': 'OBS-control', 'hypothesis_id': 'H-control', 'scope': 'confirmation', 'value': 50},
        {'observation_id': 'OBS-candidate', 'hypothesis_id': 'H-candidate', 'scope': 'confirmation', 'value': 1},
        {'observation_id': 'OBS-dev-base', 'hypothesis_id': 'H-control', 'scope': 'dev', 'value': 13.0},
        {'observation_id': 'OBS-dev-cand', 'hypothesis_id': 'H-candidate', 'scope': 'dev', 'value': 4.0},
    ]


def _assertion_receipt(feature, direction, interval, n_rows=20, mean=1.0):
    claim, judgment = records(state='inconclusive')
    judgment['evidence'] = {}
    return claim, judgment, {
        'schema': 'popper.confirmation-row-ci/v1', 'unit': 'confirmation_row',
        'n_rows': n_rows, 'mean_effect': 99.0, 'ci95': [80.0, 110.0],
        'baseline': 100.0, 'candidate': 1.0, 'threshold': 0,
        'development_threshold': 0.05, 'interval_reason': None,
        'claim_assertion': {
            'feature': feature, 'label': feature,
            'direction': direction, 'n_rows': n_rows, 'rows_discarded': 0,
            'ci95': interval, 'mean_effect': mean, 'scientific_status': 'supported',
            'interval_reason': None,
        },
    }


def test_mae_improvement_with_interval_crossing_zero_is_insufficient():
    claim, judgment, summary = _assertion_receipt('dose', 'negative', [-0.2, 0.3])
    result = project_claim(claim, [judgment], _scores(), split_confirmation=summary)
    assert result['scientific_status'] == 'inconclusive'
    assert result['min_meaningful_effect'] == 0.05
    text = result['compiled_sentence']
    assert text.startswith('证据不足。')
    assert '断言为dose与目标呈负向' in text
    assert '确认区间 [-0.2, 0.3]' in text
    assert '开发记录（不是支持依据）' in text
    assert '搜索集基线 MAE 13、候选 MAE 4' in text
    assert '确认集基线 MAE 100、候选 MAE 1' in text
    assert '确认行数 20' in text
    assert text.endswith('这不建立机制。')
    assert '断言获得独立证据支持' not in text
    assert '预测更准' not in text
    assert '迁移确认' not in text
    assert '龄期 > 28' not in text


def test_interval_on_the_opposite_side_refutes_despite_mae_improvement():
    claim, judgment, summary = _assertion_receipt('span', 'positive', [-2.0, -0.4], mean=-1.2)
    result = project_claim(claim, [judgment], _scores(), split_confirmation=summary)
    assert result['scientific_status'] == 'refuted'
    text = result['compiled_sentence']
    assert '断言被独立证据反驳' in text
    assert '断言为span与目标呈正向' in text
    assert '确认区间 [-2.0, -0.4]' in text
    assert '确认集基线 MAE 100、候选 MAE 1' in text
    assert '预测更准' not in text
    assert text.endswith('这不建立机制。')


def test_interval_on_the_declared_side_supports_the_assertion_not_accuracy():
    claim, judgment, summary = _assertion_receipt('span', 'positive', [0.2, 1.4], mean=0.8)
    first = project_claim(claim, [judgment], _scores(), split_confirmation=summary)
    claim, judgment, other = _assertion_receipt('span', 'positive', [0.01, 0.04], mean=0.02)
    small = project_claim(claim, [judgment], _scores(), split_confirmation=other)
    assert first['scientific_status'] == 'supported'
    assert small['scientific_status'] == 'supported'
    assert small['min_meaningful_effect'] == 0.05
    text = first['compiled_sentence']
    assert '断言获得独立证据支持' in text
    assert '断言为span与目标呈正向' in text
    assert '确认区间 [0.2, 1.4]' in text
    assert '预测更准' not in text
    assert '登记预测获得独立证据支持' not in text
    assert text.endswith('这不建立机制。')
    assert first['sentence_sha256'] != small['sentence_sha256']
    assert first['compiled_sentence'] != small['compiled_sentence']


def test_missing_preregistered_feature_cannot_project_support():
    claim, judgment = records(state='inconclusive')
    judgment['evidence'] = {}
    summary = {
        'unit': 'confirmation_row', 'n_rows': 40, 'mean_effect': 12.0, 'ci95': [8.0, 14.0],
        'baseline': 20.0, 'candidate': 1.0, 'claim_assertion': {
            'feature': None, 'direction': None, 'n_rows': 40, 'rows_discarded': 0,
            'ci95': None, 'mean_effect': None, 'scientific_status': 'inconclusive',
            'interval_reason': '没有断言卡',
        }, 'interval_reason': '没有断言卡',
    }
    result = project_claim(claim, [judgment], _scores(), split_confirmation=summary)
    assert result['scientific_status'] == 'inconclusive'
    assert result['ci95'] is None
    text = result['compiled_sentence']
    assert '没有断言卡' in text
    assert '没有确认区间' in text
    assert '确认集基线 MAE 20、候选 MAE 1' in text
    assert '断言获得独立证据支持' not in text
    assert text.endswith('这不建立机制。')


def test_fewer_than_two_confirmation_rows_do_not_invent_an_interval():
    claim, judgment, summary = _assertion_receipt('span', 'positive', [1.0, 2.0], n_rows=1, mean=1.5)
    summary['claim_assertion']['interval_reason'] = '确认行少于 2，不能形成区间'
    summary['n_rows'] = 1
    result = project_claim(claim, [judgment], _scores(), split_confirmation=summary)
    assert result['scientific_status'] == 'inconclusive'
    assert result['ci95'] is None
    text = result['compiled_sentence']
    assert '没有确认区间' in text
    assert '确认行数 1' in text
    assert '断言获得独立证据支持' not in text
    assert '预测更准' not in text
    assert text.endswith('这不建立机制。')


def _population_shift():
    return {'column': 'batch', 'search': {'operator': 'le', 'threshold': 10},
            'confirmation': {'operator': 'gt', 'threshold': 10}, 'rows_discarded': 2}


def test_transport_interval_crossing_zero_is_insufficient_even_if_mae_and_same_population_support():
    claim, judgment, summary = _assertion_receipt('dose', 'negative', [-0.2, 0.3])
    summary['population_transport'] = _population_shift()
    summary['same_population_ci95'] = [-4.0, -1.0]
    result = project_claim(claim, [judgment], _scores(), split_confirmation=summary)
    assert result['scientific_status'] == 'inconclusive'
    assert result['ci95'] == [-0.2, 0.3]
    assert result['min_meaningful_effect'] == 0.05
    text = result['compiled_sentence']
    assert text.startswith('证据不足。')
    assert '断言为dose与目标呈负向' in text
    assert '这是预先声明的人群迁移确认：确认人群为 batch > 10，不是同一人群的随机留出' in text
    assert '确认区间 [-0.2, 0.3]' in text
    assert '确认集基线 MAE 100、候选 MAE 1' in text
    assert '搜索集基线 MAE 13、候选 MAE 4' in text
    assert text.endswith('这不建立机制。')
    assert '断言获得独立证据支持' not in text
    assert '同一人群的随机留出' in text
    assert result['ci95'] != summary['same_population_ci95']


def test_transport_interval_on_the_opposite_side_refutes():
    claim, judgment, summary = _assertion_receipt('dose', 'negative', [0.4, 2.0], mean=1.1)
    summary['population_transport'] = _population_shift()
    result = project_claim(claim, [judgment], _scores(), split_confirmation=summary)
    assert result['scientific_status'] == 'refuted'
    text = result['compiled_sentence']
    assert '断言被独立证据反驳' in text
    assert '这是预先声明的人群迁移确认：确认人群为 batch > 10，不是同一人群的随机留出' in text
    assert '确认区间 [0.4, 2.0]' in text
    assert text.endswith('这不建立机制。')
    assert '断言获得独立证据支持' not in text


def test_transport_interval_on_the_declared_side_supports():
    claim, judgment, summary = _assertion_receipt('dose', 'negative', [-3.0, -0.5], mean=-1.6)
    summary['population_transport'] = _population_shift()
    result = project_claim(claim, [judgment], _scores(), split_confirmation=summary)
    assert result['scientific_status'] == 'supported'
    text = result['compiled_sentence']
    assert '断言获得独立证据支持' in text
    assert '这是预先声明的人群迁移确认：确认人群为 batch > 10，不是同一人群的随机留出' in text
    assert '确认区间 [-3.0, -0.5]' in text
    assert '登记预测获得独立证据支持' not in text
    assert text.endswith('这不建立机制。')


def test_supported_interval_and_insufficient_units():
    claim, judgment = records(state='supported', interval=(.1188, .2990))
    assert project_claim(claim, [judgment])['scientific_status'] == 'supported'
    judgment['evidence']['quality'] = 'insufficient_units'
    judgment['new_judgment'] = 'inconclusive'
    assert project_claim(claim, [judgment])['scientific_status'] == 'inconclusive'
