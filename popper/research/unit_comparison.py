"""Registered independent-unit comparison without a FIGS/model-family dependency.

The first artifact verifier reproduces predictions from polynomial coefficients.
It establishes artifact/configuration consistency, not model novelty or causal
mechanisms. Training seeds are execution settings, never independent units.
"""
from __future__ import annotations

import argparse
import math
import subprocess
import sys
from pathlib import Path

from ..core import ProtocolError, digest, file_hash, read_json, write_json

SCHEMA = 'paired-unit-ci/v2'


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_protocol(protocol):
    required = {'schema', 'metric', 'effect', 'aggregation', 'unit', 'confidence',
        'bootstrap_samples', 'bootstrap_seed', 'minimum_units', 'model_seed', 'groups',
        'configurations', 'hypothesis_predictions', 'hypothesis_contrasts',
        'primary_dev_prefix', 'artifact_validator', 'scope_schema', 'analysis_unit'}
    if not isinstance(protocol, dict) or not required <= protocol.keys() or protocol['schema'] != SCHEMA:
        raise ProtocolError('Independent-unit comparison protocol is incomplete or unsupported')
    p = protocol; metric = p['metric']
    if not isinstance(metric, dict) or metric not in ({'name': 'mae', 'direction': 'min'}, {'name': 'mse', 'direction': 'min'}):
        raise ProtocolError('Independent-unit comparison supports registered MAE/MSE minimization')
    name = metric['name']
    if (p['effect'] != 'relative_' + name + '_improvement'
            or p['aggregation'] != 'equal_analysis_unit_mean_' + name
            or p['unit'] != 'ratio' or p['confidence'] != .95
            or p['bootstrap_samples'] != 10000 or p['minimum_units'] != 30
            or type(p['bootstrap_seed']) is not int or not 0 <= p['bootstrap_seed'] < 2**64
            or type(p['model_seed']) is not int
            or p['artifact_validator'] != 'polynomial/v1'):
        raise ProtocolError('Unsupported frozen statistics or artifact validator')
    if not isinstance(p['groups'], dict) or not p['groups'] or any(
            not isinstance(k, str) or not isinstance(v, str) or not v for k, v in p['groups'].items()):
        raise ProtocolError('Independent analysis units require a frozen row-ID grouping map')
    unit = p['analysis_unit']
    if (not isinstance(unit, dict) or not isinstance(unit.get('kind'), str) or unit['kind'] == 'train_seed'
            or not isinstance(unit.get('definition'), str) or not unit['definition'].strip()
            or unit.get('training_seeds_are_analysis_units') is not False
            or not isinstance(p['scope_schema'], list) or not p['scope_schema']):
        raise ProtocolError('New comparisons require explicit scope and independent-unit semantics')
    configurations = p['configurations']
    if not isinstance(configurations, dict) or not configurations:
        raise ProtocolError('Frozen configurations are required')
    baseline = 0; primary = set()
    for sha, registered in configurations.items():
        if (not isinstance(registered, dict) or set(registered) != {'config', 'hypothesis_id', 'is_baseline'}
                or not isinstance(registered['config'], dict) or digest(registered['config']) != sha
                or type(registered['is_baseline']) is not bool
                or not isinstance(registered['hypothesis_id'], str) or not registered['hypothesis_id']
                or type(registered['config'].get('degree')) is not int
                or not 0 <= registered['config']['degree'] <= 12):
            raise ProtocolError('Registered polynomial configuration is invalid or hash differs')
        baseline += int(registered['is_baseline'])
        if not registered['is_baseline']:
            if registered['hypothesis_id'] in primary:
                raise ProtocolError('Duplicate registered candidate hypothesis')
            primary.add(registered['hypothesis_id'])
    if baseline != 1:
        raise ProtocolError('Exactly one frozen baseline is required')
    if not isinstance(p['hypothesis_predictions'], dict) or not isinstance(p['hypothesis_contrasts'], dict):
        raise ProtocolError('Frozen prediction conditions and contrasts are required')
    if set(p['hypothesis_contrasts']) != primary:
        raise ProtocolError('Frozen contrasts must cover every registered candidate')
    alternatives = set()
    for hid, contrast in p['hypothesis_contrasts'].items():
        alternative = contrast.get('alternative_id') if isinstance(contrast, dict) else None
        if (not isinstance(alternative, str) or not alternative or alternative in primary or alternative in alternatives
                or hid not in p['hypothesis_predictions'] or alternative not in p['hypothesis_predictions']):
            raise ProtocolError('Contrast must bind a registered candidate and alternative prediction')
        alternatives.add(alternative)
    if set(p['hypothesis_predictions']) != primary | alternatives:
        raise ProtocolError('Prediction predicates must cover exactly registered candidate contrasts')
    for prediction in p['hypothesis_predictions'].values():
        if not isinstance(prediction, dict) or set(prediction) != {'operator', 'threshold'}:
            raise ProtocolError('Frozen prediction predicate is invalid')
        op, threshold = prediction['operator'], prediction['threshold']
        if (op not in {'gt', 'gte', 'lt', 'between'}
                or (op != 'between' and not _finite(threshold))
                or (op == 'between' and (not isinstance(threshold, list) or len(threshold) != 2
                    or not all(_finite(v) for v in threshold) or threshold[0] >= threshold[1]))):
            raise ProtocolError('Frozen prediction threshold is invalid')
    scopes = p.get('analysis_scopes', {})
    if (not isinstance(scopes, dict) or any(not isinstance(k, str) or not k.startswith('dev:slice:')
            or not isinstance(v, str) or not v for k, v in scopes.items())
            or len(set(scopes.values())) != len(scopes)
            or not isinstance(p['primary_dev_prefix'], str)
            or (p['primary_dev_prefix'] and p['primary_dev_prefix'] not in scopes.values())):
        raise ProtocolError('Frozen development scope mapping is invalid')
    return protocol


def polynomial_value(coefficients, x):
    if not _finite(x):
        raise ProtocolError('Polynomial input must be finite')
    value = 0.
    for coefficient in reversed(coefficients):
        value = value*x + coefficient
    if not math.isfinite(value):
        raise ProtocolError('Polynomial prediction must be finite')
    return value


def _identity(observation):
    return {key: value for key, value in observation.items() if key != 'status'}


def _prediction_rows(observation, protocol):
    from .evaluation_service import evaluate_request
    artifact = Path(observation['artifact_id'])
    if file_hash(artifact) != observation['artifact_sha256']:
        raise ProtocolError('Independent evaluation artifact changed')
    request = read_json(artifact.parent/'request.json'); response = read_json(artifact)
    if (request['evaluator_id'] != protocol['metric']['name'] + '-v1'
            or observation['scorer_id'] != request['evaluator_id']
            or observation['unit'] != protocol['metric']['name']
            or request['expected_seeds'] != [protocol['model_seed']]
            or response != evaluate_request(request)
            or response['service_sha256'] != observation['evaluator_service_hash']):
        raise ProtocolError('Independent evaluation cannot be reproduced with frozen metric and seed')
    scope = observation['scope']; selector = 'mean'
    if scope.startswith('dev:slice:'):
        sid = scope.removeprefix('dev:slice:')
        matches = [(i, registered) for i, registered in enumerate(request.get('analysis_slices', []))
                   if registered['slice_id'] == sid]
        if (len(matches) != 1 or matches[0][1]['rule'] != {'kind': 'id_prefix',
                'value': protocol.get('analysis_scopes', {}).get(scope)}):
            raise ProtocolError('Observation scope does not bind the frozen evaluator slice')
        selector = 'slices.' + str(matches[0][0]) + '.mean'
    elif scope not in {'dev', 'confirmation'}:
        raise ProtocolError('Unknown independently evaluated observation scope')
    if observation['selector'] != selector:
        raise ProtocolError('Observation selector contradicts its registered scope')
    selected = response
    try:
        for part in observation['selector'].split('.'):
            selected = selected[int(part)] if isinstance(selected, list) else selected[part]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProtocolError('Observation selector cannot be independently reproduced') from None
    if selected != observation['value']:
        raise ProtocolError('Observation value differs from independently reproduced evaluator')
    rows = read_json(request['dataset']); entry = request['predictions'][0]
    predicted = read_json(entry['path'])
    values = {}
    for row in predicted:
        if (not isinstance(row, dict) or set(row) != {'id', 'prediction'}
                or not isinstance(row['id'], str) or row['id'] in values or not _finite(row['prediction'])):
            raise ProtocolError('Polynomial prediction coverage or value is invalid')
        values[row['id']] = row['prediction']
    if set(values) != {row['id'] for row in rows}:
        raise ProtocolError('Polynomial predictions must cover every evaluated row once')
    seed = protocol['model_seed']; audit_path = Path(entry['path']).parent/f'model-audit-{seed}.json'
    audit = read_json(audit_path)
    required = {'schema', 'seed', 'degree', 'coefficients', 'coefficients_sha256', 'config_sha256'}
    if (not isinstance(audit, dict) or set(audit) != required or audit['schema'] != 'polynomial/v1'
            or type(audit['seed']) is not int or audit['seed'] != seed or type(audit['degree']) is not int):
        raise ProtocolError('Polynomial artifact schema or seed is not registered')
    registered = protocol['configurations'].get(audit['config_sha256'])
    coefficients = audit['coefficients']
    if (registered is None or audit['degree'] != registered['config']['degree']
            or not isinstance(coefficients, list) or len(coefficients) != audit['degree'] + 1
            or not all(_finite(c) for c in coefficients)
            or digest(coefficients) != audit['coefficients_sha256']):
        raise ProtocolError('Polynomial coefficients or configuration binding is invalid')
    for row in rows:
        if not math.isclose(polynomial_value(coefficients, row['x']), values[row['id']], abs_tol=1e-10, rel_tol=1e-10):
            raise ProtocolError('Serialized polynomial does not reproduce actual predictions')
    return rows, values, request['dataset_sha256'], file_hash(audit_path), registered


def paired_statistics(baseline, candidate, seed):
    import numpy as np
    a, b = np.asarray(baseline, dtype=float), np.asarray(candidate, dtype=float)
    if (a.ndim != 1 or a.shape != b.shape or not len(a) or not np.isfinite(a).all()
            or not np.isfinite(b).all() or np.any(a < 0) or np.any(b < 0)):
        raise ProtocolError('Independent-unit errors are invalid')
    if a.mean() <= 1e-12:
        raise ProtocolError('Relative improvement baseline denominator is zero')
    indices = np.random.default_rng(seed).integers(0, len(a), size=(10000, len(a)))
    aa, bb = a[indices].mean(axis=1), b[indices].mean(axis=1)
    if np.any(aa <= 1e-12):
        raise ProtocolError('Bootstrap relative-improvement denominator is zero')
    return {'mean_effect': float((a.mean()-b.mean())/a.mean()),
            'ci95': np.quantile((aa-bb)/aa, [.025, .975]).tolist(),
            'quality': 'valid' if len(a) >= 30 else 'insufficient', 'n_units': len(a),
            'baseline_value': float(a.mean()), 'candidate_value': float(b.mean())}


def evaluate(packet):
    import numpy as np
    protocol_path = Path(packet['protocol_path'])
    if file_hash(protocol_path) != packet['protocol_sha256']:
        raise ProtocolError('Frozen comparison protocol changed')
    protocol = validate_protocol(read_json(protocol_path)); observations = packet['observations']
    if len(observations) != 2 or observations[0]['scope'] != observations[1]['scope']:
        raise ProtocolError('Comparison requires paired observations in one registered scope')
    decoded = [_prediction_rows(o, protocol) for o in observations]
    if decoded[0][2] != decoded[1][2]:
        raise ProtocolError('Comparison requires identical frozen evaluation data')
    candidate = observations[1]['hypothesis_id']
    if (decoded[0][4]['is_baseline'] is not True
            or decoded[0][4]['hypothesis_id'] != observations[0]['hypothesis_id']
            or decoded[1][4]['is_baseline'] is not False or decoded[1][4]['hypothesis_id'] != candidate):
        raise ProtocolError('Polynomial artifacts do not bind the registered baseline/candidate')
    scope = observations[1]['scope']
    if scope == 'dev':
        if protocol['primary_dev_prefix']:
            raise ProtocolError('Whole development split is not a registered comparison scope')
        prefix = ''
    elif scope.startswith('dev:slice:'):
        if scope not in protocol.get('analysis_scopes', {}):
            raise ProtocolError('Unknown frozen comparison scope')
        prefix = protocol['analysis_scopes'][scope]
    elif scope == 'confirmation':
        prefix = ''
    else:
        raise ProtocolError('Unknown frozen comparison split')
    name = protocol['metric']['name']; grouped = []
    for rows, values, *_ in decoded:
        errors = {}
        for row in rows:
            if prefix and not row['id'].startswith(prefix):
                continue
            unit = protocol['groups'].get(row['id'])
            if unit is None:
                raise ProtocolError('Evaluated row has no frozen independent analysis unit')
            error = abs(values[row['id']] - row['y'])
            errors.setdefault(unit, []).append(error**2 if name == 'mse' else error)
        grouped.append({unit: float(np.mean(values)) for unit, values in errors.items()})
    if not grouped[0] or set(grouped[0]) != set(grouped[1]):
        raise ProtocolError('Independent analysis units do not pair across baseline and candidate')
    units = sorted(grouped[0]); stats = paired_statistics([grouped[0][u] for u in units],
                                                        [grouped[1][u] for u in units], protocol['bootstrap_seed'])
    baseline_value, candidate_value = stats.pop('baseline_value'), stats.pop('candidate_value')
    alternative = protocol['hypothesis_contrasts'][candidate]['alternative_id']
    return {'schema': SCHEMA, 'request_sha256': digest(packet), 'service_sha256': file_hash(Path(__file__)),
        'metric': protocol['effect'], 'unit': protocol['unit'], 'aggregation': protocol['aggregation'],
        **stats, 'baseline_' + name: baseline_value, 'candidate_' + name: candidate_value,
        'analysis_prefix': prefix, 'model_audit_sha256': [d[3] for d in decoded],
        'hypothesis_predictions': {h: protocol['hypothesis_predictions'][h] for h in (candidate, alternative)},
        'artifact_validator': protocol['artifact_validator'], 'model_seed': protocol['model_seed'],
        'mechanism_proven': False, 'trust': 'separate_process_same_account'}


def verify_artifact(descriptor, observations):
    request, response = Path(descriptor['request']), Path(descriptor['response'])
    if file_hash(request) != descriptor['request_sha256'] or file_hash(response) != descriptor['response_sha256']:
        raise ProtocolError('Independent comparison artifact changed')
    packet = read_json(request)
    if packet['observations'] != [_identity(o) for o in observations]:
        raise ProtocolError('Independent comparison references different observations')
    result = read_json(response)
    if evaluate(packet) != result:
        raise ProtocolError('Independent-unit comparison cannot be reproduced')
    return result


def compare_observations(project, run_dir, baseline, candidate):
    source = Path(project)/'judgment-protocol.json'
    packet = {'protocol_path': str(source), 'protocol_sha256': file_hash(source),
              'observations': [_identity(o) for o in (baseline, candidate)]}
    root = Path(run_dir)/'comparisons'/digest(packet)[:24]; root.mkdir(parents=True, exist_ok=True)
    request, response = root/'request.json', root/'response.json'
    if request.exists() and read_json(request) != packet:
        raise ProtocolError('Comparison request identity conflict')
    if not request.exists():
        write_json(request, packet)
    if not response.exists():
        result = subprocess.run([sys.executable, '-m', 'popper.research.unit_comparison',
            '--request', str(request), '--response', str(response)], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            write_json(root/'failure.json', {'returncode': result.returncode, 'error_type': 'comparison_process_failed'})
            raise ProtocolError('Independent-unit comparison process failed')
    descriptor = {'comparison_schema': SCHEMA, 'request': str(request), 'response': str(response),
                  'request_sha256': file_hash(request), 'response_sha256': file_hash(response)}
    verify_artifact(descriptor, (baseline, candidate))
    return descriptor


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--response', required=True, type=Path); args = parser.parse_args()
    write_json(args.response, evaluate(read_json(args.request)))
