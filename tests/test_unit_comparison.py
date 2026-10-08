"""Freeze generic engine first, then adapt only a new project's declared inputs."""
import copy
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, digest, file_hash, initialize, read_json, write_json
from popper.capabilities import extra_available
from popper.research.comparison_registry import supported_schemas
from popper.research.controller import ResearchController
from popper.research.judgment import interval_verdict
from popper.research.project_adapter import RegisteredProjectAdapter
from popper.research.unit_comparison import (SCHEMA, compare_observations, evaluate,
    paired_statistics, polynomial_value, validate_protocol, verify_artifact)


def polynomial_project(project):
    """A new adapter consists only of project files, never a controller branch."""
    project = Path(project); project.mkdir()
    source = (Path(__file__).resolve().parents[1]/'examples/quadratic/model.py').read_text(encoding='utf-8')
    old = '    Path(args.output).write_text(json.dumps(result, allow_nan=False), encoding="utf-8")'
    audit = '''
    import hashlib
    def sha(value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()
    config = load(args.config)
    audit = {"schema": "polynomial/v1", "seed": int(args.seed), "degree": degree,
             "coefficients": weights, "coefficients_sha256": sha(weights), "config_sha256": sha(config)}
    target = Path(args.output).parent / ("model-audit-" + args.seed + ".json")
    target.write_text(json.dumps(audit, allow_nan=False), encoding="utf-8")
'''
    if old not in source:
        raise AssertionError('Project adapter source anchor changed')
    (project/'model.py').write_text(source.replace(old, old + audit), encoding='utf-8')
    groups = {}; rng = random.Random(401027)
    for split, count, domains in [('train', 64, ['fitting']), ('dev', 80, ['diagnostic', 'transport']),
                                   ('test', 48, ['confirmation'])]:
        rows = []
        for i in range(count):
            domain = domains[i % len(domains)]
            x = rng.uniform(-2.5, 2.5)
            row = {'id': domain + '-' + split + '-' + str(i), 'x': x,
                   'y': 1 + .2*x + .4*x*x + rng.gauss(0, .04)}
            rows.append(row); groups[row['id']] = row['id']
        write_json(project/(split+'.json'), rows)
    baseline = {'degree': 0}; candidates = [{'degree': 1}, {'degree': 2}]
    registered = {digest(baseline): {'config': baseline, 'hypothesis_id': 'H-control-'+digest(baseline)[:12], 'is_baseline': True}}
    predictions = {}; contrasts = {}
    for i, config in enumerate(candidates):
        hid = 'H-'+digest({'index': i, 'config': config})[:16]; alternate = 'H-equivalence-'+str(i)
        registered[digest(config)] = {'config': config, 'hypothesis_id': hid, 'is_baseline': False}
        predictions[hid] = {'operator': 'gt', 'threshold': .05}
        predictions[alternate] = {'operator': 'between', 'threshold': [-.05, .05]}
        contrasts[hid] = {'alternative_id': alternate}
    protocol = {'schema': SCHEMA, 'metric': {'name': 'mse', 'direction': 'min'},
        'effect': 'relative_mse_improvement', 'aggregation': 'equal_analysis_unit_mean_mse', 'unit': 'ratio',
        'confidence': .95, 'bootstrap_samples': 10000, 'bootstrap_seed': 101,
        'minimum_units': 30, 'model_seed': 23, 'groups': groups,
        'configurations': registered, 'hypothesis_predictions': predictions, 'hypothesis_contrasts': contrasts,
        'primary_dev_prefix': 'transport-', 'analysis_scopes': {'dev:slice:s7': 'diagnostic-', 'dev:slice:s9': 'transport-'},
        'artifact_validator': 'polynomial/v1', 'selection_metric': 'candidate_mse',
        'analysis_unit': {'kind': 'observation', 'definition': 'One independently generated observation per frozen unit',
                          'training_seeds_are_analysis_units': False},
        'confirmation_policy': {'schema': 'selected_by_registered_dev_metric/v1',
            'selection_metric': 'candidate_mse', 'direction': 'min', 'tie_break': 'hypothesis_id'},
        'scope_schema': [
            {'scope_id': 'dev', 'label': 'Combined development data', 'role': 'diagnostic', 'split': 'dev',
             'applicability': 'Combined registered environments; excluded from candidate selection'},
            {'scope_id': 'dev:slice:s7', 'label': 'Diagnostic environment A', 'role': 'diagnostic', 'split': 'dev',
             'applicability': 'Registered diagnostic observations in environment A'},
            {'scope_id': 'dev:slice:s9', 'label': 'Primary environment B', 'role': 'primary', 'split': 'dev',
             'applicability': 'Registered primary observations in environment B'},
            {'scope_id': 'confirmation', 'label': 'Independent environment B confirmation', 'role': 'primary',
             'split': 'confirmation', 'applicability': 'Independent preregistered environment B observations'}]}
    spec = {'name': 'polynomial-transfer-unseen', 'objective': 'Compare registered polynomial interventions using MSE in primary environment B.',
        'entrypoint': 'model.py', 'code_files': ['model.py'], 'train': 'train.json', 'dev': 'dev.json', 'test': 'test.json',
        'baseline': baseline, 'candidates': candidates, 'metric': protocol['metric'], 'seeds': [23],
        'budget': 2, 'timeout_seconds': 30, 'min_improvement': .05, 'provenance_files': ['judgment-protocol.json'],
        'analysis_slices': [{'slice_id': 's9', 'rule': {'kind': 'id_prefix', 'value': 'transport-'}},
                            {'slice_id': 's7', 'rule': {'kind': 'id_prefix', 'value': 'diagnostic-'}}],
        'analysis_slice_splits': ['dev']}
    write_json(project/'experiment.json', spec); write_json(project/'judgment-protocol.json', protocol)
    return protocol


@unittest.skipUnless(extra_available('analysis'), 'analysis extra required')
class UnitComparisonTests(unittest.TestCase):
    def test_mae_mse_seed23_are_registered_without_model_family_dependency(self):
        self.assertIn(SCHEMA, supported_schemas())
        with tempfile.TemporaryDirectory() as tmp:
            p = polynomial_project(Path(tmp)/'project')
            self.assertEqual(23, validate_protocol(p)['model_seed'])
            p['metric']['name'] = 'mae'; p['effect'] = 'relative_mae_improvement'; p['aggregation'] = 'equal_analysis_unit_mean_mae'
            self.assertEqual('mae', validate_protocol(p)['metric']['name'])
            self.assertAlmostEqual(17, polynomial_value([1, 2, 3], 2))
            with self.assertRaises(ProtocolError):
                polynomial_value([1], float('nan'))

    def test_support_refutation_and_insufficient_units_are_independent_of_training_seed(self):
        for n, candidate, expected in [(30, .8, 'supported'), (30, 1.2, 'refuted'), (16, .8, 'inconclusive')]:
            with self.subTest(n=n, candidate=candidate):
                result = paired_statistics([1.]*n, [candidate]*n, 101)
                self.assertEqual(expected, interval_verdict('gt', .05, result['ci95'], quality=result['quality']))
        with self.assertRaises(ProtocolError):
            paired_statistics([0.]*40, [0.]*40, 101)

    def test_invalid_rules_and_configuration_hashes_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = polynomial_project(Path(tmp)/'project')
            for mutation in (lambda p: p.update(minimum_units=16), lambda p: p.update(bootstrap_samples=20),
                    lambda p: p['metric'].update(direction='max'), lambda p: p.update(artifact_validator='opaque/v1'),
                    lambda p: p.update(primary_dev_prefix='unmapped-'),
                    lambda p: p.pop('scope_schema'), lambda p: p['analysis_unit'].update(training_seeds_are_analysis_units=True),
                    lambda p: p['configurations'][next(iter(p['configurations']))]['config'].update(degree=12)):
                p = copy.deepcopy(original); mutation(p)
                with self.subTest(mutation=mutation), self.assertRaises(ProtocolError):
                    validate_protocol(p)

    def test_real_mae_uses_group_count_not_row_count_for_sufficiency(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)/'project'; run = Path(tmp)/'research'
            protocol = polynomial_project(project); spec = read_json(project/'experiment.json')
            protocol['metric'] = {'name': 'mae', 'direction': 'min'}; spec['metric'] = protocol['metric']
            protocol['effect'] = 'relative_mae_improvement'; protocol['aggregation'] = 'equal_analysis_unit_mean_mae'
            protocol['selection_metric'] = 'candidate_mae'
            protocol['confirmation_policy']['selection_metric'] = 'candidate_mae'
            for prefix in ('diagnostic-', 'transport-'):
                ids = sorted(r['id'] for r in read_json(project/'dev.json') if r['id'].startswith(prefix))
                for i, rid in enumerate(ids):
                    protocol['groups'][rid] = prefix + 'independent-unit-' + str(i//2)
            protocol['analysis_unit']['definition'] = 'Registered independent groups contain two observations each'
            write_json(project/'experiment.json', spec); write_json(project/'judgment-protocol.json', protocol)
            initialize(project); ResearchController.initialize(project, run, budget_cap=5)
            c = ResearchController(run)
            try:
                executed = c.execute_registered_candidate(c.manifest['candidate_hypothesis_ids'][1],
                    expected_version=c.store.get('study', c.manifest['study_id'])['state_version'], trusted_local=True)
                evidence = executed['judgments'][0]['evidence']
                self.assertEqual(20, evidence['n_units'])
                self.assertEqual('insufficient', evidence['quality'])
                self.assertEqual('inconclusive', executed['judgments'][0]['new_judgment'])
                self.assertGreater(evidence['mean_effect'], .05)
                self.assertEqual('candidate_mae', RegisteredProjectAdapter(c).select_finalist()['selection_metric'])
            finally:
                c.close()

    def test_frozen_core_accepts_unseen_polynomial_project_through_runtime_and_confirmation(self):
        # Any change to these generic files during adaptation fails this test.
        root = Path(__file__).resolve().parents[1]
        frozen_paths = [root/name for name in ('popper/research/controller.py', 'popper/research/runtime.py',
            'popper/research/runtime_protocol.py', 'popper/research/project_adapter.py',
            'popper/research/unit_comparison.py', 'popper/research/comparison_registry.py')]
        hashes = {str(p): file_hash(p) for p in frozen_paths}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)/'new-project'; run = Path(tmp)/'research'
            polynomial_project(project); initialize(project); ResearchController.initialize(project, run, budget_cap=5)
            c = ResearchController(run)
            try:
                runtime = c.planning_runtime(profile='fixed_queue', max_experiments=2)
                result = runtime.run(trusted_local=True)
                self.assertEqual('stopped', result['planner']['status'])
                self.assertEqual(2, len(result['development']['executions']))
                self.assertEqual({'degree': 2}, result['finalist']['config'])
                self.assertEqual('dev:slice:s9', result['finalist']['scope'])
                self.assertEqual('candidate_mse', result['finalist']['selection_metric'])
                self.assertTrue(result['integrity']['ok'])
                receipt = result['finalist']['selection_receipt']
                confirmed = c.confirm_registered_candidate(receipt['hypothesis_id'], expected_version=receipt['state_version'],
                    selection_receipt=receipt, trusted_local=True)
                self.assertTrue(confirmed['integrity']['ok'])
                judgments = [j for j in c.store.list('judgment') if j['scope'] == 'confirmation']
                self.assertEqual(2, len(judgments))
                self.assertEqual(48, judgments[0]['evidence']['n_units'])
                self.assertEqual(23, judgments[0]['evidence']['model_seed'])
                self.assertTrue(all(j['evidence']['artifact_validator'] == 'polynomial/v1' for j in judgments))
                runs_before = len(c.store.list('run'))
                c.confirm_registered_candidate(receipt['hypothesis_id'], expected_version=receipt['state_version'],
                    selection_receipt=receipt, trusted_local=True)
                self.assertEqual(runs_before, len(c.store.list('run')))
                self.assertEqual(hashes, {str(p): file_hash(p) for p in frozen_paths})
            finally:
                c.close()

    def test_coefficient_prediction_and_scope_tampering_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)/'project'; run = Path(tmp)/'research'; polynomial_project(project)
            initialize(project); ResearchController.initialize(project, run, budget_cap=5)
            c = ResearchController(run)
            try:
                hid = c.manifest['candidate_hypothesis_ids'][1]
                executed = c.execute_registered_candidate(hid,
                    expected_version=c.store.get('study', c.manifest['study_id'])['state_version'], trusted_local=True)
                observations = [executed['baseline'], executed['observation']]
                descriptor = executed['judgments'][0]['comparison_artifact']
                evidence = verify_artifact(descriptor, observations)
                self.assertEqual(40, evidence['n_units'])
                packet = read_json(descriptor['request'])
                altered = copy.deepcopy(packet); altered['observations'][1]['scope'] = 'dev:slice:s1'
                altered['observations'][0]['scope'] = 'dev:slice:s1'
                with self.assertRaises(ProtocolError):
                    evaluate(altered)
                altered = copy.deepcopy(packet)
                altered['observations'][0]['scope'] = 'dev:slice:s7'
                altered['observations'][1]['scope'] = 'dev:slice:s7'
                with self.assertRaises(ProtocolError):
                    evaluate(altered)
                native_request = read_json(Path(observations[1]['artifact_id']).parent/'request.json')
                audit_path = Path(native_request['predictions'][0]['path']).parent/'model-audit-23.json'
                audit = read_json(audit_path); original_bytes = audit_path.read_bytes()
                audit['coefficients'][0] += 1
                write_json(audit_path, audit)
                with self.assertRaises(ProtocolError):
                    verify_artifact(descriptor, observations)
                audit['coefficients_sha256'] = digest(audit['coefficients']); write_json(audit_path, audit)
                with self.assertRaises(ProtocolError):
                    verify_artifact(descriptor, observations)
                audit_path.write_bytes(original_bytes)
                self.assertTrue(c.store.verify()['ok'])
            finally:
                c.close()


if __name__ == '__main__':
    unittest.main()
