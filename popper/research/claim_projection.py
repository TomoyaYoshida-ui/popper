"""Read-only scientific meaning of Claims; historical threshold facts stay intact."""
from __future__ import annotations

import math

from ..core import ProtocolError, digest
from .judgment import interval_verdict

SCIENTIFIC_LABELS = {
    'supported': '登记预测获得独立证据支持',
    'refuted': '登记预测被独立证据反驳',
    'inconclusive': '证据不足',
    'not_evaluated': '尚无独立科学判断',
}
_ASSERTION_STATUS = {
    'supported': '断言获得独立证据支持',
    'refuted': '断言被独立证据反驳',
    'inconclusive': '证据不足',
    'not_evaluated': '尚无独立科学判断',
}
_DIRECTION_LABELS = {'positive': '正向', 'negative': '负向'}


def directional_verdict(direction, interval):
    """方向断言只看区间是否整段落在 0 的一侧。不使用绝对 MAE 阈值。"""
    if direction not in {'positive', 'negative'} or interval is None:
        return 'inconclusive'
    if (len(interval) != 2 or any(type(value) not in (int, float) or isinstance(value, bool)
                                  or not math.isfinite(value) for value in interval)
            or interval[0] > interval[1]):
        raise ProtocolError('断言区间不合法')
    low, high = interval
    if low > 0:
        return 'supported' if direction == 'positive' else 'refuted'
    if high < 0:
        return 'supported' if direction == 'negative' else 'refuted'
    return 'inconclusive'


def _num(value):
    return f"{value:.6g}" if isinstance(value, float) else str(value)


def _observation_id(row):
    return row.get("observation_id") or row.get("entity_id")


def _best(rows, hypothesis_id, direction):
    own = [row["value"] for row in rows if row.get("hypothesis_id") == hypothesis_id
           and row.get("value") is not None]
    if not own:
        return None
    return min(own) if direction != "max" else max(own)


def _pair(rows, hypothesis_id, direction):
    candidate = _best(rows, hypothesis_id, direction)
    baseline_ids = {row.get("hypothesis_id") for row in rows if row.get("hypothesis_id") != hypothesis_id}
    baselines = [value for hid in baseline_ids if (value := _best(rows, hid, direction)) is not None]
    if candidate is None or not baselines:
        return None
    baseline = min(baselines) if direction != "max" else max(baselines)
    return {"baseline": baseline, "candidate": candidate}


def confirmation_measurement(claim, observations, repeat_values):
    """开发分、确认分、种子数，以及没有区间时的原因。不改科学状态。"""
    if observations is None:
        return None
    hypothesis_id = claim.get("hypothesis_id")
    direction = claim.get("metric_direction", "min")
    refs = set(claim.get("observation_refs") or [])
    confirmation = [row for row in observations if _observation_id(row) in refs]
    control_ids = {row.get("hypothesis_id") for row in confirmation if row.get("hypothesis_id") != hypothesis_id}
    dev = [row for row in observations if row.get("scope") == "dev"
           and (row.get("hypothesis_id") == hypothesis_id or row.get("hypothesis_id") in control_ids)]
    dev_pair = _pair(dev, hypothesis_id, direction)
    confirmation_pair = _pair(confirmation, hypothesis_id, direction)
    seeds = list(repeat_values or [])
    n_seeds = len(seeds)
    if claim.get("ci95") is not None:
        reason = None
    elif n_seeds < 2:
        reason = f"只有 {n_seeds} 个种子，不能形成区间"
    else:
        reason = "没有分组比较，点估计不能给出区间"
    parts = []
    if dev_pair:
        parts.append(f"开发分 基线 {_num(dev_pair['baseline'])}、候选 {_num(dev_pair['candidate'])}")
    if confirmation_pair:
        parts.append(f"确认分 基线 {_num(confirmation_pair['baseline'])}、候选 {_num(confirmation_pair['candidate'])}")
    parts.append(f"种子数 {n_seeds}")
    if reason:
        parts.append(reason)
    return {"dev_baseline": None if dev_pair is None else dev_pair["baseline"],
            "dev_candidate": None if dev_pair is None else dev_pair["candidate"],
            "confirmation_baseline": None if confirmation_pair is None else confirmation_pair["baseline"],
            "confirmation_candidate": None if confirmation_pair is None else confirmation_pair["candidate"],
            "n_seeds": n_seeds, "interval_reason": reason,
            "measurement_line": "；".join(parts)}


def _development_record(dev_baseline, dev_candidate, confirmation_baseline, confirmation_candidate):
    def one(value):
        return "未记录" if value is None else _num(value)
    return ("开发记录（不是支持依据）："
            f"搜索集基线 MAE {one(dev_baseline)}、候选 MAE {one(dev_candidate)}；"
            f"确认集基线 MAE {one(confirmation_baseline)}、候选 MAE {one(confirmation_candidate)}")


def _assertion_text(status, ci95, assertion):
    feature = assertion.get("feature")
    direction = assertion.get("direction")
    label = assertion.get("label") or feature
    if isinstance(label, str) and label and direction in _DIRECTION_LABELS:
        claim = f"断言为{label}与目标呈{_DIRECTION_LABELS[direction]}"
    else:
        claim = "没有断言卡"
    interval = f"确认区间 {ci95}" if ci95 is not None else "没有确认区间"
    discarded = assertion.get("rows_discarded") or 0
    rows = f"确认行数 {assertion.get('n_rows')}"
    if discarded:
        rows += f"，舍弃 {discarded} 行"
    parts = [claim]
    if assertion.get("transport_clause"):
        parts.append(assertion["transport_clause"])
    parts.extend((interval, assertion["development_record"], rows))
    return f"{_ASSERTION_STATUS[status]}。" + "；".join(parts) + "。这不建立机制。"


def transport_clause(transport):
    """有预先声明的迁移规则时，投影句写出确认人群，而不是同人群随机留出。"""
    if not isinstance(transport, dict):
        return None
    confirmation = transport.get("confirmation") or {}
    column = transport.get("column")
    operator = confirmation.get("operator")
    threshold = confirmation.get("threshold")
    symbol = {"gt": ">", "ge": "≥", "lt": "<", "le": "≤"}.get(operator)
    if not isinstance(column, str) or not column or symbol is None or threshold is None:
        return None
    return f"这是预先声明的人群迁移确认：确认人群为 {column} {symbol} {threshold}，不是同一人群的随机留出"


def compiled_sentence(status, *, ci95, threshold, quality, measurement=None, assertion=None):
    """展示句只由确认判断字段和测量编译。制品哈希不进入这句话。"""
    if assertion is not None:
        text = _assertion_text(status, ci95, assertion)
        assertion_payload = {"feature": assertion.get("feature"),
                             "label": assertion.get("label"),
                             "direction": assertion.get("direction"),
                             "n_rows": assertion.get("n_rows")}
        if assertion.get("transport_clause"):
            assertion_payload["transport_clause"] = assertion["transport_clause"]
        payload = {"scientific_status": status, "ci95": ci95,
                   "assertion": assertion_payload,
                   "development_record": assertion.get("development_record"),
                   "mechanism_proven": False}
        return text, digest(payload)
    if status == "not_evaluated":
        payload = {"scientific_status": "not_evaluated", "mechanism_proven": False}
        text = "尚无独立科学判断。这不建立机制。"
    else:
        payload = {"scientific_status": status, "ci95": ci95, "threshold": threshold,
                   "quality": quality, "mechanism_proven": False,
                   "measurement": None if measurement is None else measurement["measurement_line"]}
        details = []
        if ci95 is not None:
            details.append(f"区间 {ci95}")
        if threshold is not None:
            details.append(f"阈值 {threshold}")
        if quality is not None:
            details.append(f"质量 {quality}")
        if measurement is not None:
            details.append(measurement["measurement_line"])
        text = SCIENTIFIC_LABELS[status] + ("。" + "，".join(details) if details else "") + "。这不建立机制。"
    return text, digest(payload)


def project_claim(claim, judgments, observations=None, repeat_values=None,
                  split_confirmation=None):
    """Bind the exact confirmation pair/version, never the latest hypothesis prose."""
    result = dict(claim)
    result.update(scientific_status='not_evaluated', judgment_ref=None,
                  effect_type='absolute_metric_difference', effect_unit=claim.get('metric'),
                  effect_value=claim.get('oriented_delta'), threshold=claim.get('min_meaningful_effect'),
                  ci95=None, evidence_quality=None, descriptive_status=claim.get('status'),
                  conclusion_basis='legacy_descriptive_threshold')
    matching = [j for j in judgments if
                j.get('study_id') == claim.get('study_id') and
                j.get('hypothesis_id') == claim.get('hypothesis_id') and
                j.get('hypothesis_version') == claim.get('hypothesis_version') and
                j.get('scope') == 'confirmation' and
                j.get('observation_refs') == claim.get('observation_refs')]
    if not matching:
        return _with_sentence(result, observations, repeat_values, split_confirmation)
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
                  ci95=evidence.get('ci95'), evidence_quality=evidence.get('quality'),
                  conclusion_basis='independent_judgment')
    if split_confirmation and "claim_assertion" in split_confirmation:
        _apply_claim_assertion(result, split_confirmation)
    return _with_sentence(result, observations, repeat_values, split_confirmation)


def _apply_claim_assertion(result, split_confirmation):
    """科学状态只由锁定方向和确认区间相对 0 决定。MAE 不进入判定。"""
    assertion = split_confirmation.get("claim_assertion") or {}
    feature = assertion.get("feature")
    direction = assertion.get("direction")
    label = assertion.get("label") or feature
    result["conclusion_basis"] = "locked_directional_claim"
    result["threshold"] = 0
    result["effect_type"] = "univariate_slope"
    if not isinstance(feature, str) or not feature or direction not in _DIRECTION_LABELS:
        result.update(scientific_status="inconclusive", ci95=None, effect_value=None,
                      effect_unit=None, evidence_quality=None, claim_feature=None,
                      claim_direction=None, claim_label=None)
        return result
    n_rows = assertion.get("n_rows") or 0
    interval = assertion.get("ci95")
    if n_rows < 2 or interval is None:
        status, interval = "inconclusive", None
    else:
        status = directional_verdict(direction, interval)
    result.update(scientific_status=status, ci95=interval,
                  effect_value=None if interval is None else assertion.get("mean_effect"),
                  effect_unit=feature, evidence_quality="valid" if interval is not None else None,
                  claim_feature=feature, claim_direction=direction,
                  claim_label=label)


def _with_sentence(result, observations=None, repeat_values=None, split_confirmation=None):
    measurement = confirmation_measurement(result, observations, repeat_values)
    if split_confirmation and "claim_assertion" in split_confirmation:
        assertion = split_confirmation.get("claim_assertion") or {}
        dev_baseline = None if measurement is None else measurement.get("dev_baseline")
        dev_candidate = None if measurement is None else measurement.get("dev_candidate")
        record = _development_record(
            dev_baseline, dev_candidate, split_confirmation.get("baseline"),
            split_confirmation.get("candidate"))
        view = {"feature": result.get("claim_feature"), "label": result.get("claim_label"),
                "direction": result.get("claim_direction"),
                "n_rows": assertion.get("n_rows", split_confirmation.get("n_rows")),
                "rows_discarded": assertion.get("rows_discarded") or 0,
                "development_record": record}
        clause = transport_clause(split_confirmation.get("population_transport"))
        if clause:
            view["transport_clause"] = clause
        if measurement is not None:
            measurement["confirmation_baseline"] = split_confirmation.get("baseline")
            measurement["confirmation_candidate"] = split_confirmation.get("candidate")
            measurement["n_seeds"] = view["n_rows"]
            measurement["interval_reason"] = split_confirmation.get("interval_reason")
            measurement["measurement_line"] = record
        text, sentence_sha = compiled_sentence(
            result["scientific_status"], ci95=result.get("ci95"), threshold=0,
            quality=result.get("evidence_quality"), assertion=view)
        result.update(compiled_sentence=text, sentence_sha256=sentence_sha, measurement_line=record)
        if measurement is not None:
            result.update(measurement)
        return result
    if split_confirmation and measurement is not None and split_confirmation.get("unit") == "confirmation_row":
        if split_confirmation.get("baseline") is not None:
            measurement["confirmation_baseline"] = split_confirmation["baseline"]
            measurement["confirmation_candidate"] = split_confirmation["candidate"]
        measurement["n_seeds"] = split_confirmation["n_rows"]
        parts = []
        if measurement.get("dev_baseline") is not None:
            parts.append(f"开发分 基线 {_num(measurement['dev_baseline'])}、候选 {_num(measurement['dev_candidate'])}")
        if measurement.get("confirmation_baseline") is not None:
            parts.append("确认分 基线 "
                         f"{_num(measurement['confirmation_baseline'])}、候选 {_num(measurement['confirmation_candidate'])}")
        parts.append(f"确认行数 {split_confirmation['n_rows']}")
        if split_confirmation.get("interval_reason"):
            parts.append(split_confirmation["interval_reason"])
        measurement["measurement_line"] = "；".join(parts)
        measurement["interval_reason"] = split_confirmation.get("interval_reason")
    elif split_confirmation and measurement is not None and split_confirmation.get("per_seed"):
        rows = split_confirmation["per_seed"]
        measurement["confirmation_baseline"] = sum(row["baseline"] for row in rows) / len(rows)
        measurement["confirmation_candidate"] = sum(row["candidate"] for row in rows) / len(rows)
        parts = []
        if measurement.get("dev_baseline") is not None:
            parts.append(f"开发分 基线 {_num(measurement['dev_baseline'])}、候选 {_num(measurement['dev_candidate'])}")
        parts.append("确认分 基线 "
                     f"{_num(measurement['confirmation_baseline'])}、候选 {_num(measurement['confirmation_candidate'])}")
        parts.append(f"种子数 {measurement['n_seeds']}")
        if measurement.get("interval_reason"):
            parts.append(measurement["interval_reason"])
        measurement["measurement_line"] = "；".join(parts)
    text, sentence_sha = compiled_sentence(
        result['scientific_status'], ci95=result.get('ci95'), threshold=result.get('threshold'),
        quality=result.get('evidence_quality'), measurement=measurement)
    result.update(compiled_sentence=text, sentence_sha256=sentence_sha)
    if measurement is not None:
        result.update(measurement)
    return result


def claim_verdict(claim):
    return SCIENTIFIC_LABELS[claim['scientific_status']]
