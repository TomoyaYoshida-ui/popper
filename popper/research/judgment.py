"""Scientific judgments separate from descriptive prediction threshold outcomes."""
from __future__ import annotations
import math
from ..core import ProtocolError, digest


def interval_verdict(operator, threshold, interval, *, quality='valid'):
    if quality != 'valid' or interval is None:
        return 'inconclusive'
    if (len(interval) != 2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in interval)
            or interval[0] > interval[1]):
        raise ProtocolError('判断区间不合法')
    low, high = interval
    if operator == 'gt':
        return 'supported' if low > threshold else ('refuted' if high <= threshold else 'inconclusive')
    if operator == 'between':
        lower, upper = threshold
        return 'supported' if low >= lower and high <= upper else ('refuted' if high < lower or low > upper else 'inconclusive')
    if operator == 'gte':
        return 'supported' if low >= threshold else ('refuted' if high < threshold else 'inconclusive')
    if operator == 'lt':
        return 'supported' if high < threshold else ('refuted' if low >= threshold else 'inconclusive')
    raise ProtocolError('判断预测运算符不合法')


def derive(hypothesis, design, observations, previous=None, comparison_artifact=None):
    refs = [o['observation_id'] for o in observations]
    state, reason, evidence = 'not_evaluated', 'execution_or_evaluation_not_completed', None
    if len(observations) == 2:
        baseline, candidate = observations
        if (baseline['scope'] != candidate['scope'] or baseline['scorer_id'] != candidate['scorer_id']
                or candidate.get('hypothesis_version') != hypothesis['version']):
            raise ProtocolError('判断证据范围、计分器或假设版本不一致')
        if not all(o.get('artifact_id') and o.get('artifact_sha256') and o.get('evaluator_service_hash')
                   for o in observations):
            raise ProtocolError('判断必须引用独立评估制品')
        state, reason = 'inconclusive', 'descriptive_threshold_is_not_statistical_support'
        if comparison_artifact:
            from .grouped_comparison import verify_artifact
            evidence = verify_artifact(comparison_artifact, observations)
            prediction = evidence['hypothesis_predictions'].get(hypothesis['hypothesis_id'])
            if prediction is None:
                raise ProtocolError('假设不在冻结的分组预测中')
            state = interval_verdict(prediction['operator'], prediction['threshold'],
                                     evidence['ci95'], quality=evidence['quality'])
            reason = 'frozen_grouped_bootstrap_prediction'
        elif candidate.get('hypothesis_id') != hypothesis['hypothesis_id']:
            raise ProtocolError('判断观察不属于当前假设')
    action = {'supported': 'continue_sampling', 'refuted': 'revise_hypothesis',
              'inconclusive': 'add_control', 'not_evaluated': 'stop'}[state]
    result = {'schema': 'research-judgment/v1', 'study_id': hypothesis['study_id'],
              'hypothesis_id': hypothesis['hypothesis_id'], 'hypothesis_version': hypothesis['version'],
              'previous_judgment_id': previous.get('judgment_id') if previous else None,
              'old_judgment': previous['new_judgment'] if previous else 'not_recorded',
              'new_judgment': state, 'observation_refs': refs,
              'scope': observations[-1]['scope'] if observations else 'not_evaluated',
              'reason': reason, 'next_action': action, 'mechanism_proven': False,
              'comparison_artifact': comparison_artifact, 'evidence': evidence,
              'prediction_contract': design.get('prediction_contract')}
    result['judgment_id'] = 'J-' + digest(result)[:24]
    return result
