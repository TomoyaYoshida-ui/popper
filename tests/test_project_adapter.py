import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, write_json
from popper.capabilities import extra_available
from popper.research.project_adapter import RegisteredProjectAdapter


class StoreFixture:
    def __init__(self):
        self.rows = {'hypothesis': {h: {'version': 1} for h in ('H-a', 'H-b')},
                     'observation': {}, 'run': {}, 'judgment': {}, 'study': {'S-test': {'state_version': 1}}}

    def get(self, kind, identifier):
        return self.rows.get(kind, {}).get(identifier)

    def list(self, kind):
        return list(self.rows.get(kind, {}).values())


class ControllerFixture:
    def __init__(self, project, spec):
        self.project = project
        self.store = StoreFixture()
        self.manifest = {'metric': spec['metric'], 'budget_cap': 7, 'study_id': 'S-test',
                         'candidate_hypothesis_ids': ['H-a', 'H-b'],
                         'configs': dict(zip(['H-a', 'H-b'], spec['candidates']))}
        self.verified = 0

    def _verify_inputs(self):
        self.verified += 1
        return {'spec': json.loads((self.project/'experiment.json').read_text())}


class ProjectAdapterTests(unittest.TestCase):
    def fixture(self, root, *, metric='mae', paired=True, shifted=True):
        spec = {'objective': 'Compare frozen interventions in the declared primary scope.',
            'metric': {'name': metric, 'direction': 'min'}, 'seeds': [17],
            'baseline': {'kind': 'cart', 'features': [0, 1]},
            'candidates': [{'kind': 'cart', 'features': [0, 1]}, {'kind': 'cart', 'features': [0]}],
            'budget': 4, 'timeout_seconds': 120, 'min_improvement': .05}
        protocol = {'schema': 'paired-unit-ci/v1', 'metric': spec['metric'],
            'model_seed': 17, 'effect': 'relative_' + metric + '_improvement',
            'primary_dev_prefix': 'beta-' if shifted else '',
            'analysis_scopes': {'dev:slice:s8': 'alpha-', 'dev:slice:s3': 'beta-'} if shifted else {},
            'hypothesis_predictions': {'H-a': {'operator': 'gt', 'threshold': .05}}}
        if paired:
            spec['provenance_files'] = ['judgment-protocol.json']
        if shifted:
            spec['analysis_slices'] = [{'slice_id': 's3', 'rule': {'kind': 'id_prefix', 'value': 'beta-'}},
                {'slice_id': 's8', 'rule': {'kind': 'id_prefix', 'value': 'alpha-'}}]
            spec['analysis_slice_splits'] = ['dev']
        write_json(root/'experiment.json', spec)
        if paired:
            write_json(root/'judgment-protocol.json', protocol)
        return ControllerFixture(root, spec), protocol

    def write_protocol(self, root, protocol):
        write_json(root/'judgment-protocol.json', protocol)

    def test_primary_mapping_uses_declared_prefix_not_scope_order_or_task_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self.fixture(Path(tmp))
            adapter = RegisteredProjectAdapter(c)
            self.assertTrue(adapter.preflight()['ok'])
            self.assertEqual('dev:slice:s3', adapter.primary_scope_id())
            self.assertEqual(['dev:slice:s3', 'dev:slice:s8'], adapter.comparison_scope_ids())
            self.assertEqual('diagnostic', adapter.scope_for_observation({'scope': 'dev:slice:s8'})['role'])
            self.assertEqual('beta-', adapter.scope_for_observation('dev:slice:s3')['label'])
            self.assertTrue(adapter.scope_for_observation('dev:slice:s3')['legacy_derived'])
            with self.assertRaises(ProtocolError):
                adapter.scope_for_observation('dev:slice:s1')

    def test_missing_duplicate_and_contradictory_mappings_fail_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c, original = self.fixture(root)
            mutations = [lambda p: p.update(primary_dev_prefix='absent-'),
                lambda p: p['analysis_scopes'].pop('dev:slice:s3'),
                lambda p: p['analysis_scopes'].update({'dev:slice:s3': 'alpha-'}),
                lambda p: p['metric'].update(direction='max'),
                lambda p: p.update(model_seed=4)]
            for mutate in mutations:
                with self.subTest(mutation=mutate):
                    protocol = copy.deepcopy(original); mutate(protocol); self.write_protocol(root, protocol)
                    with self.assertRaises(ProtocolError):
                        RegisteredProjectAdapter(c).preflight()

    def test_explicit_semantics_are_public_and_cannot_contradict_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c, protocol = self.fixture(root)
            existing = RegisteredProjectAdapter(c).describe()['scopes']
            protocol['scope_schema'] = [{k: v for k, v in s.items() if k != 'legacy_derived'} for s in existing]
            protocol['scope_schema'][2]['label'] = 'Held-out environment B'
            protocol['scope_schema'][2]['applicability'] = 'Environment B as declared before evaluation'
            protocol['feature_schema'] = [{'index': 0, 'name': 'recorded measurement'}, {'index': 1, 'name': 'registered proxy'}]
            protocol['analysis_unit'] = {'kind': 'group', 'definition': 'Identical input records share one unit'}
            self.write_protocol(root, protocol)
            public = RegisteredProjectAdapter(c).describe()
            self.assertFalse(any(s['legacy_derived'] for s in public['scopes']))
            self.assertEqual([17], public['training_seeds'])
            self.assertEqual('group', public['analysis_unit']['kind'])
            self.assertEqual(2, len(public['feature_schema']))
            self.assertNotIn('protocol_path', public)
            protocol['scope_schema'][0]['role'] = 'primary'
            self.write_protocol(root, protocol)
            with self.assertRaises(ProtocolError):
                RegisteredProjectAdapter(c).preflight()

    def test_partial_or_duplicate_public_scope_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c, protocol = self.fixture(root)
            scopes = RegisteredProjectAdapter(c).describe()['scopes']
            for declared in (scopes[:1], [*scopes, scopes[0]]):
                protocol['scope_schema'] = declared; self.write_protocol(root, protocol)
                with self.assertRaises(ProtocolError):
                    RegisteredProjectAdapter(c).preflight()

    def test_no_oracle_labels_or_private_paths_are_projected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c, protocol = self.fixture(root)
            protocol.update(features={'9': [1, 2]}, groups={'9': 'g'}, oracle={'hidden_truth': True},
                            hidden_generation_mechanism='private', test_labels=[5])
            self.write_protocol(root, protocol)
            write_json(root/'oracle-private.json', {'generator': 'never expose'})
            public = json.dumps(RegisteredProjectAdapter(c).describe())
            for private in ('oracle', 'test_labels', 'hidden_truth', str(root), 'hidden_generation_mechanism'):
                self.assertNotIn(private, public)
            protocol['feature_schema'] = [{'index': 0, 'name': 'C:\\Users\\private.txt'}, {'index': 1, 'name': 'safe'}]
            self.write_protocol(root, protocol)
            with self.assertRaises(ProtocolError):
                RegisteredProjectAdapter(c).describe()

    def test_feature_names_only_from_declared_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c, protocol = self.fixture(root)
            self.assertEqual([], RegisteredProjectAdapter(c).describe()['feature_schema'])
            protocol['feature_schema'] = [{'index': 0, 'name': 'alcohol'}]
            self.write_protocol(root, protocol)
            with self.assertRaises(ProtocolError):
                RegisteredProjectAdapter(c).preflight()

    def test_legacy_native_metric_and_figs_protocol_keep_development_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c, _ = self.fixture(root, paired=False, shifted=False, metric='mse')
            adapter = RegisteredProjectAdapter(c)
            self.assertEqual('dev', adapter.primary_scope_id())
            self.assertEqual('mse', adapter.describe()['selection_metric'])
            self.assertIsNone(adapter.comparison_schema())
            spec = json.loads((root/'experiment.json').read_text()); spec['provenance_files'] = ['judgment-protocol.json']
            spec['metric'] = {'name': 'mae', 'direction': 'min'}; c.manifest['metric'] = spec['metric']
            write_json(root/'experiment.json', spec)
            self.write_protocol(root, {'schema': 'figs-grouped-ci/v1', 'hypothesis_predictions': {}})
            self.assertEqual('dev', RegisteredProjectAdapter(c).primary_scope_id())
            self.assertEqual('figs-grouped-ci/v1', RegisteredProjectAdapter(c).comparison_schema())
            self.assertEqual('candidate_group_mae', RegisteredProjectAdapter(c).describe()['selection_metric'])

    def evidence(self, c, hid, value, *, scope='dev:slice:s3', metric='mae', run_status='succeeded'):
        oid = 'O-' + hid; run = 'R-' + hid
        c.store.rows['observation'][oid] = {'observation_id': oid, 'hypothesis_id': hid,
            'hypothesis_version': 1, 'scope': scope, 'run_id': run, 'value': value + 20}
        c.store.rows['run'][run] = {'status': run_status}
        c.store.rows['judgment']['J-' + hid] = {'hypothesis_id': hid, 'hypothesis_version': 1,
            'scope': scope, 'observation_refs': [oid], 'comparison_artifact': {'registered': True},
            'evidence': {'candidate_' + metric: value}, 'judgment_id': 'J-' + hid}
        return {'hypothesis_id': hid, 'observation_id': oid, 'dev_value': -999,
                'final_model_choice': hid, 'agent_assessment': 'supported'}

    def test_finalist_uses_authoritative_primary_metric_and_stable_tie_break(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self.fixture(Path(tmp)); adapter = RegisteredProjectAdapter(c)
            a = self.evidence(c, 'H-a', 2.); b = self.evidence(c, 'H-b', 1.)
            selected = adapter.select_finalist([a, b])
            self.assertEqual('H-b', selected['hypothesis_id']); self.assertEqual(1., selected['dev_value'])
            c.store.rows['judgment']['J-H-a']['evidence']['candidate_mae'] = 1.
            self.assertEqual('H-a', adapter.select_finalist([b, a])['hypothesis_id'])
            c.store.rows['run']['R-H-a']['status'] = 'implementation_failed'
            self.assertEqual('H-b', adapter.select_finalist([a, b])['hypothesis_id'])
            c.store.rows['observation']['O-H-b']['scope'] = 'dev:slice:s8'
            self.assertIsNone(adapter.select_finalist([a, b]))

    def test_new_mse_project_uses_declaration_without_task_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); c, _ = self.fixture(root, metric='mse', paired=False, shifted=False)
            adapter = RegisteredProjectAdapter(c)
            a = self.evidence(c, 'H-a', 3., metric='mse'); b = self.evidence(c, 'H-b', 2., metric='mse')
            c.store.rows['observation']['O-H-a']['scope'] = 'dev'
            c.store.rows['observation']['O-H-b']['scope'] = 'dev'
            self.assertEqual('direction_normalized_absolute_metric_difference', adapter.describe()['effect_definition'])
            self.assertEqual('H-b', adapter.select_finalist([a, b])['hypothesis_id'])
            self.assertEqual('mse', adapter.select_finalist([a, b])['selection_metric'])

    def test_v1_cannot_claim_new_metric_support_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self.fixture(Path(tmp), metric='mse')
            with self.assertRaises(ProtocolError):
                RegisteredProjectAdapter(c).preflight()

    def test_confirmation_receipt_recomputes_all_registered_evidence(self):
        from popper.core import digest
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self.fixture(Path(tmp)); adapter = RegisteredProjectAdapter(c)
            a = self.evidence(c, 'H-a', 2.); b = self.evidence(c, 'H-b', 1.)
            receipt = adapter.confirmation_selection_receipt()
            self.assertEqual('registered-dev-selection/v1', receipt['schema'])
            self.assertEqual('H-b', receipt['hypothesis_id'])
            self.assertEqual(['H-b', 'H-a'], [e['hypothesis_id'] for e in receipt['candidate_evidence']])
            self.assertEqual(receipt, adapter.select_finalist([a, b])['selection_receipt'])
            self.assertEqual(receipt, adapter.select_finalist([a])['selection_receipt'])
            self.assertEqual(digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'}), receipt['receipt_sha256'])

    def test_stale_version_missing_evidence_and_nonfinite_scores_are_not_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self.fixture(Path(tmp)); adapter = RegisteredProjectAdapter(c)
            a = self.evidence(c, 'H-a', 1.)
            c.store.rows['hypothesis']['H-a']['version'] = 2
            self.assertIsNone(adapter.select_finalist([a]))
            c.store.rows['hypothesis']['H-a']['version'] = 1
            c.store.rows['judgment'].clear()
            self.assertIsNone(adapter.select_finalist([a]))
            a = self.evidence(c, 'H-a', float('inf'))
            with self.assertRaises(ProtocolError):
                adapter.select_finalist([a])

    def test_real_native_controller_selection_and_tampered_evidence(self):
        from popper.core import initialize
        from popper.research.controller import ResearchController
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root/'project'; run = root/'run'
            shutil.copytree(Path(__file__).resolve().parents[1]/'examples/quadratic', project,
                            ignore=shutil.ignore_patterns('.popper', '__pycache__'))
            initialize(project); ResearchController.initialize(project, run)
            c = ResearchController(run)
            try:
                hid = c.manifest['candidate_hypothesis_ids'][0]
                version = c.store.get('study', c.manifest['study_id'])['state_version']
                execution = c.execute_registered_candidate(hid, expected_version=version, trusted_local=True)
                record = {'hypothesis_id': hid, 'observation_id': execution['observation']['observation_id'],
                          'dev_value': -999}
                selected = RegisteredProjectAdapter(c).select_finalist([record])
                self.assertEqual(execution['observation']['value'], selected['dev_value'])
                self.assertEqual('mse', selected['selection_metric'])
                Path(execution['observation']['artifact_id']).write_text('{}', encoding='utf-8')
                with self.assertRaises(ProtocolError):
                    RegisteredProjectAdapter(c).select_finalist([record])
            finally:
                c.close()

    @unittest.skipUnless(extra_available('figs'), 'figs extra required')
    def test_real_paired_controller_accepts_renamed_and_permuted_scopes(self):
        from popper.core import initialize, read_json
        from popper.research.controller import ResearchController
        from evaluation.transfer_tasks import prepare
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root/'project'; run = root/'research'
            prepare('T02', project, root/'cache')
            spec = read_json(project/'experiment.json')
            spec['name'] = 'unseen-shifted-task'
            spec['analysis_slices'] = [
                {'slice_id': 's9', 'rule': {'kind': 'id_prefix', 'value': 'independent-'}},
                {'slice_id': 's4', 'rule': {'kind': 'id_prefix', 'value': 'source-'}}]
            protocol = read_json(project/'judgment-protocol.json')
            protocol['analysis_scopes'] = {'dev:slice:s4': 'source-', 'dev:slice:s9': 'independent-'}
            for scope in protocol.get('scope_schema', []):
                scope['scope_id'] = {'dev:slice:s1': 'dev:slice:s4', 'dev:slice:s2': 'dev:slice:s9'}.get(
                    scope['scope_id'], scope['scope_id'])
            write_json(project/'experiment.json', spec); write_json(project/'judgment-protocol.json', protocol)
            initialize(project); ResearchController.initialize(project, run, budget_cap=7)
            c = ResearchController(run)
            try:
                hid = c.manifest['candidate_hypothesis_ids'][2]
                version = c.store.get('study', c.manifest['study_id'])['state_version']
                executed = c.execute_registered_candidate(hid, expected_version=version, trusted_local=True)
                self.assertEqual('dev:slice:s9', executed['observation']['scope'])
                adapter = RegisteredProjectAdapter(c)
                self.assertEqual('dev:slice:s9', adapter.confirmation_selection_receipt()['scope'])
                public = adapter.describe()
                self.assertNotIn('sin(f0)', json.dumps(public))
                self.assertEqual('diagnostic', adapter.scope_for_observation('dev:slice:s4')['role'])
                self.assertTrue(c.store.verify()['ok'])
            finally:
                c.close()


if __name__ == '__main__':
    unittest.main()
