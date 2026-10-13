"""Independent grouped comparison of authenticated scalar prediction packets.

Candidate code and serialized models are never imported by this evaluator.
"""
from __future__ import annotations
import argparse
import copy
import json
from functools import lru_cache
from pathlib import Path
import numpy as np
from ..core import ProtocolError, digest, file_hash, read_json, write_json, dataset, model_inputs

SCHEMA = 'prediction-packet-ci/v1'


def checked(path, expected):
    if not Path(path).is_file() or Path(path).is_symlink() or file_hash(path) != expected:
        raise ProtocolError('Comparison source missing or changed')
    return read_json(path)


def _predictions(rows, descriptor):
    values = checked(descriptor['path'], descriptor['sha256'])
    if not isinstance(values, list) or len(values) != len(rows):
        raise ProtocolError('Prediction coverage mismatch')
    mapped = {}
    for row in values:
        if not isinstance(row, dict) or set(row) != {'id', 'prediction'} or row['id'] in mapped:
            raise ProtocolError('Invalid or duplicated prediction identity')
        value = row['prediction']
        if type(value) not in (int, float) or not np.isfinite(value):
            raise ProtocolError('Prediction must be finite')
        mapped[row['id']] = value
    if set(mapped) != {r['id'] for r in rows}:
        raise ProtocolError('Prediction IDs do not cover the frozen data')
    return mapped


def _worker_binding(rows, descriptor, bindings):
    """Authenticate a packet against frozen Worker inputs and executable source."""
    worker=descriptor.get('worker')
    if not worker:
        raise ProtocolError('Prediction packet requires Worker provenance')
    authenticated={b['path']:b['sha256'] for b in bindings}
    documents={}
    for role in ('receipt','manifest','revision'):
        ref=worker[role]
        if authenticated.get(ref['path'])!=ref['sha256']:
            raise ProtocolError('Worker provenance is outside authenticated bindings')
        documents[role]=checked(ref['path'],ref['sha256'])
    receipt,manifest,revision=(documents[k] for k in ('receipt','manifest','revision'))
    spec=manifest['spec']
    if (receipt['status']!='succeeded' or manifest['status']!='succeeded'
        or manifest['receipt_sha256']!=worker['receipt']['sha256']
        or receipt['job_id']!=manifest['job_id']
        or receipt['revision_id']!=revision['revision_id'] or spec['revision_id']!=revision['revision_id']
        or receipt['revision_manifest_sha256']!=worker['revision']['sha256']
        or spec['revision_manifest_sha256']!=worker['revision']['sha256']
        or receipt['artifacts'].get('outputs/predictions.json')!=descriptor['sha256']):
        raise ProtocolError('Worker output/source identity mismatch')
    source=Path(worker['revision']['path']).parent/'code/model.py'
    if file_hash(source)!=revision['files']['model.py']:
        raise ProtocolError('Executed source identity changed')
    inputs={x['role']:x for x in spec['inputs']}
    if set(inputs)!={'training_data','features_without_labels','config'}:
        raise ProtocolError('Worker input permissions changed')
    loaded={role:checked(ref['source'],ref['sha256']) for role,ref in inputs.items()}
    if loaded['features_without_labels']!=model_inputs(rows,'vector-mae-v1'):
        raise ProtocolError('Worker received different evaluation features or labels')
    return inputs['training_data']['sha256']


def _feature_unit(row):
    features = row.get('features')
    if not isinstance(features, list) or not features:
        return None
    normalized = []
    for value in features:
        if isinstance(value, bool) or type(value) not in (int, float) or not np.isfinite(value):
            normalized.append(None)
        else:
            normalized.append(float(value))
    return digest(normalized)


def _citable_record(record):
    if not isinstance(record, dict):
        return False
    citation, statement = record.get('citation'), record.get('statement')
    return (isinstance(citation, str) and bool(citation.strip())
            and isinstance(statement, str) and bool(statement.strip()))


def _sampling_audited(packet, rows, groups, n_units):
    """Validity requires an auditable design, not a caller-supplied boolean."""
    if n_units < 30:
        return False
    sampling = packet.get('sampling')
    if not isinstance(sampling, dict):
        return False
    design = sampling.get('design')
    if design == 'iid_row':
        if sampling.get('generator_version') != 'adaptive-tasks/v1':
            return False
        return all(groups[row['id']] == _feature_unit(row) for row in rows)
    if design == 'documented_cluster':
        clusters = sampling.get('clusters')
        if not _citable_record(sampling.get('sampling_record')) or not isinstance(clusters, list):
            return False
        if not clusters or any(not isinstance(name, str) or not name for name in clusters):
            return False
        if len(set(clusters)) != len(clusters):
            return False
        return set(groups.values()) <= set(clusters)
    return False


def evaluate(packet):
    required = {'schema', 'dataset', 'dataset_sha256', 'baseline', 'candidate', 'groups',
                'independence_verified', 'bootstrap_seed', 'hypothesis_predictions', 'bindings'}
    keys = set(packet)
    if packet.get('schema') != SCHEMA or keys not in (required, required | {'sampling'}):
        raise ProtocolError('Prediction comparison packet is invalid')
    checked(packet['dataset'], packet['dataset_sha256'])
    rows = dataset(packet['dataset'], 'vector-mae-v1')
    baseline, candidate = (_predictions(rows, packet[k]) for k in ('baseline', 'candidate'))
    for binding in packet['bindings']:
        checked(binding['path'], binding['sha256'])
    if _worker_binding(rows,packet['baseline'],packet['bindings']) != _worker_binding(rows,packet['candidate'],packet['bindings']):
        raise ProtocolError('Baseline and candidate must use the same frozen training input')
    groups = packet['groups']
    if set(groups) != {r['id'] for r in rows}:
        raise ProtocolError('Analysis groups must cover the exact comparison split')
    losses = {}
    for row in rows:
        if not isinstance(groups[row['id']], str) or not groups[row['id']]:
            raise ProtocolError('Missing analysis-unit identity')
        losses.setdefault(groups[row['id']], []).append(
            (abs(baseline[row['id']] - row['y']), abs(candidate[row['id']] - row['y'])))
    means = np.array([np.mean(losses[g], axis=0) for g in sorted(losses)])
    b, c = np.mean(means, axis=0)
    # independence_verified is retained for older packets and does not grant validity.
    quality = ('valid' if _sampling_audited(packet, rows, groups, len(means))
               else 'insufficient_independent_units')
    if b <= 0:
        quality, effect, interval = 'zero_baseline_denominator', None, None
    else:
        effect = float((b - c) / b)
        rng = np.random.default_rng(packet['bootstrap_seed'])
        samples = []
        for _ in range(100):
            selected = means[rng.integers(0, len(means), size=(100, len(means)))].mean(axis=1)
            if np.any(selected[:, 0] <= 0):
                quality = 'zero_bootstrap_denominator'
            valid = selected[:, 0] > 0
            samples.extend(((selected[valid, 0] - selected[valid, 1]) / selected[valid, 0]).tolist())
        interval = [float(x) for x in np.quantile(samples, [.025, .975])] if len(samples) == 10000 else None
    return {'schema': SCHEMA, 'packet_sha256': digest(packet),
            'evaluator_implementation_sha256': file_hash(Path(__file__)), 'metric': {'name': 'mae', 'direction': 'min'},
            'effect': 'relative_mae_improvement', 'unit': 'ratio',
            'aggregation': 'equal_analysis_unit_mean_mae', 'quality': quality,
            'n_units': len(means), 'baseline_mae': float(b), 'candidate_mae': float(c),
            'mean_effect': effect, 'ci95': interval,
            'hypothesis_predictions': packet['hypothesis_predictions'], 'bootstrap_samples': 10000}


@lru_cache(maxsize=256)
def _recomputed(packet_json, implementation_sha):
    return evaluate(json.loads(packet_json))


def verify_artifact(descriptor, observations):
    packet = checked(descriptor['packet'], descriptor['packet_sha256'])
    response = checked(descriptor['response'], descriptor['response_sha256'])
    # Revalidate every dependency before using a cached numerical recomputation.
    checked(packet['dataset'], packet['dataset_sha256'])
    for key in ('baseline', 'candidate'):
        checked(packet[key]['path'], packet[key]['sha256'])
    for binding in packet['bindings']:
        checked(binding['path'], binding['sha256'])
    expected = _recomputed(json.dumps(packet, sort_keys=True), file_hash(Path(__file__)))
    rows=dataset(packet['dataset'],'vector-mae-v1')
    for key in ('baseline','candidate'):
        _worker_binding(rows,packet[key],packet['bindings'])
    if response != expected:
        raise ProtocolError('Independent comparison cannot be reproduced')
    if len(observations) != 2 or any(o.get('scorer_id') != 'vector-mae-v1' for o in observations):
        raise ProtocolError('Comparison must bind two independent vector-MAE observations')
    if observations[0]['value'] != expected['baseline_mae'] or observations[1]['value'] != expected['candidate_mae']:
        raise ProtocolError('Comparison scores contradict authoritative observations')
    return copy.deepcopy(expected)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--packet', required=True)
    parser.add_argument('--response', required=True)
    args = parser.parse_args()
    write_json(args.response, evaluate(read_json(args.packet)))


if __name__ == '__main__':
    main()
