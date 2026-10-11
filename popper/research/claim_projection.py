"""Read-only scientific meaning of Claims; historical threshold facts stay intact."""
from __future__ import annotations

from ..core import ProtocolError
from .judgment import interval_verdict

SCIENTIFIC_LABELS = {
    'supported': '登记预测获得独立证据支持',
    'refuted': '登记预测被独立证据反驳',
    'inconclusive': '证据不足',
    'not_evaluated': '尚无独立科学判断',
}


def project_claim(claim, judgments):
    """Bind the exact confirmation pair/version, never the latest hypothesis prose."""
    result = dict(claim)
    result.update(scientific_status='not_evaluated', judgment_ref=None,
                  effect_type='absolute_metric_difference', effect_unit=claim.get('metric'),
                  effect_value=claim.get('oriented_delta'), threshold=claim.get('min_meaningful_effect'),
                  ci95=None, descriptive_status=claim.get('status'),
                  conclusion_basis='legacy_descriptive_threshold')
    matching = [j for j in judgments if
                j.get('study_id') == claim.get('study_id') and
                j.get('hypothesis_id') == claim.get('hypothesis_id') and
                j.get('hypothesis_version') == claim.get('hypothesis_version') and
                j.get('scope') == 'confirmation' and
                j.get('observation_refs') == claim.get('observation_refs')]
    if not matching:
        return result
    judgment = matching[-1]
    evidence = judgment.get('evidence') or {}
    prediction = (evidence.get('hypothesis_predictions') or {}).get(claim['hypothesis_id'])
    state = judgment['new_judgment']
    if prediction:
        expected = interval_verdict(prediction['operator'], prediction['threshold'],
                                    evidence.get('ci95'), quality=evidence.get('quality', 'invalid'))
        if state != expected:
            raise ProtocolError('Claim scientific judgment contradicts its registered interval')
    elif state == 'supported':
        raise ProtocolError('Statistical support requires a registered comparison prediction')
    if state not in SCIENTIFIC_LABELS:
        raise ProtocolError('Unknown independent scientific judgment')
    result.update(scientific_status=state, judgment_ref=judgment['judgment_id'],
                  effect_type=evidence.get('effect', 'relative_' + claim['metric'] + '_improvement')
                              if prediction else 'absolute_metric_difference',
                  effect_unit=evidence.get('unit', 'ratio') if prediction else claim.get('metric'),
                  effect_value=evidence.get('mean_effect', claim.get('oriented_delta')),
                  threshold=prediction['threshold'] if prediction else None,
                  ci95=evidence.get('ci95'), conclusion_basis='independent_judgment')
    return result


def claim_verdict(claim):
    return SCIENTIFIC_LABELS[claim['scientific_status']]
