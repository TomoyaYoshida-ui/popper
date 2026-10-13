"""Sampling audits for prediction comparison. No network."""
from popper.core import digest
from popper.research.prediction_comparison import evaluate
from test_adaptive_foundations import packet


def _feature_digest(features):
    return digest([float(value) for value in features])


def test_boolean_does_not_make_public_rows_valid(tmp_path):
    prepared = packet(tmp_path)
    rows = [{'id': str(i), 'features': [float(i)]} for i in range(40)]
    prepared['groups'] = {row['id']: _feature_digest(row['features']) for row in rows}
    prepared['independence_verified'] = True
    result = evaluate(prepared)
    assert result['quality'] == 'insufficient_independent_units'
    assert result['n_units'] == 40


def test_iid_row_generator_is_valid_when_groups_match_feature_hashes(tmp_path):
    prepared = packet(tmp_path)
    rows = [{'id': str(i), 'features': [float(i)]} for i in range(40)]
    prepared['groups'] = {row['id']: _feature_digest(row['features']) for row in rows}
    prepared['independence_verified'] = False
    prepared['sampling'] = {'design': 'iid_row', 'generator_version': 'adaptive-tasks/v1'}
    result = evaluate(prepared)
    assert result['quality'] == 'valid'
    assert result['n_units'] == 40


def test_iid_row_rejects_a_mismatched_group_or_generator(tmp_path):
    prepared = packet(tmp_path)
    rows = [{'id': str(i), 'features': [float(i)]} for i in range(40)]
    prepared['groups'] = {row['id']: _feature_digest(row['features']) for row in rows}
    prepared['sampling'] = {'design': 'iid_row', 'generator_version': 'other'}
    assert evaluate(prepared)['quality'] == 'insufficient_independent_units'
    prepared['sampling'] = {'design': 'iid_row', 'generator_version': 'adaptive-tasks/v1'}
    prepared['groups']['0'] = 'not-the-feature-hash'
    assert evaluate(prepared)['quality'] == 'insufficient_independent_units'


def test_cluster_without_sampling_record_is_not_valid(tmp_path):
    prepared = packet(tmp_path)
    clusters = [f'c{i}' for i in range(40)]
    prepared['groups'] = {str(i): clusters[i] for i in range(40)}
    prepared['independence_verified'] = True
    prepared['sampling'] = {'design': 'documented_cluster', 'clusters': clusters}
    assert evaluate(prepared)['quality'] == 'insufficient_independent_units'


def test_documented_cluster_is_valid_with_a_citable_record(tmp_path):
    prepared = packet(tmp_path)
    clusters = [f'c{i}' for i in range(40)]
    prepared['groups'] = {str(i): clusters[i] for i in range(40)}
    prepared['sampling'] = {
        'design': 'documented_cluster',
        'clusters': clusters,
        'sampling_record': {
            'citation': 'doi:10.0000/independent-runs',
            'statement': 'Each registered cluster is an independent experiment',
        },
    }
    result = evaluate(prepared)
    assert result['quality'] == 'valid'
    assert result['n_units'] == 40
