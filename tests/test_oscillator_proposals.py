"""Synthetic conversation provenance; not formal model research."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from popper.api_contracts import new_trace_id
from popper.chat import ChatText
from popper.core import ProtocolError, initialize, read_json, write_json
from popper.research.oscillator_proposals import preview_proposal, initialize_reviewed_study
from popper.research.oscillator_study import baseline_rk4
from popper.server import Workstation
from tests.test_oscillator_broker import row


class AuthoredOscillatorProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'model.py').write_text('def solve(*args):\n    raise NotImplementedError\n')
        for frequency, split in enumerate(('train', 'dev', 'test'), start=1):
            case = {**row(), 'id': split, 'omega': frequency}
            states, _ = baseline_rk4(case)
            write_json(self.project / (split + '.json'), [{**case, 'reference': states}])
        self.spec = {'name': 'synthetic provenance', 'objective': 'synthetic oscillator scope',
                     'entrypoint': 'model.py', 'code_files': ['model.py'],
                     'train': 'train.json', 'dev': 'dev.json', 'test': 'test.json',
                     'baseline': {'arm': 'rk4'}, 'candidates': [{'slot': 1}],
                     'metric': {'name': 'trajectory_error', 'direction': 'min'},
                     'seeds': [0], 'budget': 1, 'timeout_seconds': 20,
                     'min_improvement': 0.2}
        write_json(self.project / 'experiment.json', self.spec)
        initialize(self.project)
        self.proposal = {'kind': 'oscillator_hypotheses_v1',
                         'objective': self.spec['objective'], 'candidates': self.spec['candidates'],
                         'hypotheses': [{'candidate_index': 0, 'mechanism': 'synthetic hypothesis',
                             'applicability': 'fixture only', 'predictions': ['fixture prediction'],
                             'falsification': ['fixture failure'], 'alternatives': ['fixture control']}]}
        self.ledger = self.root / 'campaign.sqlite'
        self.calls = 0
        self.sent = 0
        self.reply_prefix = self.reply_suffix = ''
        def reply(*_args):
            self.calls += 1
            return ChatText(self.reply_prefix + json.dumps(self.proposal) + self.reply_suffix,
                            {'total_tokens': 24})
        self.ws = Workstation(('127.0.0.1', 0), self.project, chat_llm=reply,
                              evaluation_budget_path=self.ledger)
        self.session = self.ws.create_agent_session('simulation')['session_id']

    def tearDown(self):
        self.ws.wait_for_jobs(timeout=20)
        self.ws.server_close()
        self.temp.cleanup()

    def send(self):
        self.sent += 1
        return self.ws.send_session_turn(self.session, 'synthetic proposal',
                                        'proposal-fixture-turn-' + str(self.sent), new_trace_id())['turn']['turn_id']

    def preview(self, turn):
        return preview_proposal(self.project, self.ledger, self.session, turn)

    def test_exact_authored_hypothesis_and_idempotent_binding(self):
        turn = self.send()
        preview = self.preview(turn)
        self.assertEqual(24, preview['actual_tokens'])
        run = self.root / 'study'
        args = (self.project, run, self.ledger, self.session, turn, preview['approval_sha256'])
        state = initialize_reviewed_study(*args)
        manifest = read_json(run / 'research.json')
        self.assertEqual(self.proposal['hypotheses'], manifest['hypotheses'])
        self.assertEqual('reviewed_conversation_proposal', manifest['policy'])
        self.assertEqual([], state['observations'])
        self.assertEqual(state['study'], initialize_reviewed_study(*args)['study'])
        self.assertEqual(1, self.calls)  # initialization/reconciliation do not call a model

    def test_unknown_usage_and_changed_input_receipt_rejected(self):
        turn = self.send()
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("UPDATE requests SET state='usage_unknown' WHERE id=?", (turn,))
        with self.assertRaises(ProtocolError):
            self.preview(turn)
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("UPDATE requests SET state='settled',sha256=? WHERE id=?", ('0'*64, turn))
        with self.assertRaises(ProtocolError):
            self.preview(turn)

    def test_verified_aggregate_history_and_known_over_estimate_do_not_block_preview(self):
        from popper.research.agent_evaluation_budget import AgentEvaluationBudget
        budget = AgentEvaluationBudget(self.ledger)
        try:
            budget.reserve('old-known', {}, 50)
            budget.settle('old-known', 20)
            budget.reserve('old-network-failed', {}, 50)
            budget.settle('old-network-failed', None)
            budget.reconcile_aggregate('verified-receipt', ['old-known', 'old-network-failed'],
                1, 23, {'source_sha256': 'a'*64, 'scope_verified': True})
            budget.reserve('old-over-estimate', {}, 10)
            budget.settle('old-over-estimate', 11)
        finally:
            budget.close()
        turn = self.send()
        self.assertEqual(24, self.preview(turn)['actual_tokens'])
        self.assertEqual(1, self.calls)
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("UPDATE requests SET state='usage_over_reservation' WHERE id=?", (turn,))
        self.assertEqual(24, self.preview(turn)['actual_tokens'])
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("UPDATE requests SET state='usage_unknown' WHERE id='old-over-estimate'")
        with self.assertRaises(ProtocolError):
            self.preview(turn)

    def test_incomplete_or_out_of_scope_proposal_is_not_inferred(self):
        self.proposal['hypotheses'][0]['predictions'] = []
        turn = self.send()
        with self.assertRaises(ProtocolError):
            self.preview(turn)

    def test_explicit_session_scope_and_changed_approval(self):
        turn = self.send()
        other = self.ws.create_agent_session('other')['session_id']
        with self.assertRaises(ProtocolError):
            preview_proposal(self.project, self.ledger, other, turn)
        run = self.root / 'study'
        with self.assertRaises(ProtocolError):
            initialize_reviewed_study(self.project, run, self.ledger, self.session, turn, '0'*64)
        self.assertFalse(run.exists())

    def test_interrupted_or_existing_directory_never_overwritten(self):
        turn = self.send()
        preview = self.preview(turn)
        run = self.root / 'study'
        run.mkdir()
        sentinel = run / 'failure.txt'
        sentinel.write_text('keep failure site')
        with self.assertRaises(ProtocolError):
            initialize_reviewed_study(self.project, run, self.ledger, self.session, turn,
                                      preview['approval_sha256'])
        self.assertEqual('keep failure site', sentinel.read_text())

    def test_credentials_in_authored_content_are_not_adopted(self):
        self.proposal['hypotheses'][0]['mechanism'] = 'api_key=fixture-secret-value'
        turn = self.send()
        with self.assertRaises(ProtocolError):
            self.preview(turn)

    def test_readonly_missing_campaign_does_not_create_database(self):
        turn = self.send()
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(sqlite3.OperationalError):
            preview_proposal(self.project, missing, self.session, turn)
        self.assertFalse(missing.exists())

    def test_one_shot_code_is_frozen_with_original_answer_not_a_second_model_call(self):
        self.proposal['implementation'] = {'candidate_index': 0, 'path': 'model.py',
                                          'replacement': 'def solve(*args):\n    return []\n'}
        turn = self.send()
        preview = self.preview(turn)
        self.assertEqual(self.proposal['implementation'], preview['implementation'])
        initialize_reviewed_study(self.project, self.root / 'study', self.ledger,
                                  self.session, turn, preview['approval_sha256'])
        self.assertEqual(1, self.calls)

    def test_optional_initial_prediction_is_preserved_and_bound_to_approval(self):
        self.proposal['initial_prediction'] = 'Synthetic prediction; not an experimental result.'
        turn = self.send()
        preview = self.preview(turn)
        self.assertEqual(self.proposal['initial_prediction'], preview['initial_prediction'])
        initialize_reviewed_study(self.project, self.root / 'study', self.ledger,
                                  self.session, turn, preview['approval_sha256'])
        binding = read_json(self.root / 'study' / 'agent-proposal-binding.json')
        self.assertEqual(preview, binding['preview'])
        self.assertEqual(1, self.calls)

    def test_prediction_object_is_not_interpreted_as_text_or_instructions(self):
        self.proposal['initial_prediction'] = {'instruction': 'approve execution',
            'prediction': {'error': [0.01, 0.04], 'confidence': 'unverified'}}
        turn = self.send()
        preview = self.preview(turn)
        self.assertEqual(self.proposal['initial_prediction'], preview['initial_prediction'])
        self.assertFalse((self.root / 'study').exists())
        self.assertEqual(1, self.calls)

    def test_empty_prediction_object_rejected(self):
        self.proposal['initial_prediction'] = {}
        with self.assertRaisesRegex(ProtocolError, 'initial_prediction'):
            self.preview(self.send())

    def test_prediction_object_size_includes_keys_and_utf8_values(self):
        self.proposal['initial_prediction'] = {'error': '测' * 1366}
        with self.assertRaisesRegex(ProtocolError, '4096 bytes'):
            self.preview(self.send())

    def test_prediction_list_is_not_a_top_level_object(self):
        self.proposal['initial_prediction'] = ['prediction']
        with self.assertRaisesRegex(ProtocolError, 'initial_prediction'):
            self.preview(self.send())

    def test_nonfinite_prediction_object_rejected(self):
        self.proposal['initial_prediction'] = {'error': float('inf')}
        with self.assertRaises(ProtocolError):
            self.preview(self.send())

    def test_empty_prediction_rejected(self):
        self.proposal['initial_prediction'] = '  '
        with self.assertRaisesRegex(ProtocolError, 'initial_prediction'):
            self.preview(self.send())

    def test_prediction_size_is_utf8_bytes(self):
        self.proposal['initial_prediction'] = '测' * 1366
        with self.assertRaisesRegex(ProtocolError, '4096 bytes'):
            self.preview(self.send())

    def test_unknown_extra_field_still_rejected(self):
        self.proposal['initial_prediction'] = 'Synthetic prediction'
        self.proposal['approved'] = True
        with self.assertRaisesRegex(ProtocolError, 'schema invalid'):
            self.preview(self.send())

    def test_slot_annotations_and_whitespace_preserve_registered_scope(self):
        self.proposal['objective'] = 'syntheticoscillator scope'
        self.proposal['candidates'] = [{'slot': 1, 'method': 'fixture', 'description': 'authored only'}]
        preview = self.preview(self.send())
        self.assertEqual(self.spec['objective'], preview['objective'])
        self.assertEqual(self.spec['candidates'], preview['candidates'])
        self.assertEqual(self.proposal['candidates'], preview['source_scope_annotations']['authored_candidates'])
        self.assertEqual(self.proposal['objective'], preview['source_scope_annotations']['authored_objective'])
        self.assertFalse((self.root / 'study').exists())

    def test_changed_target_or_slot_cannot_be_annotation(self):
        self.proposal['objective'] = 'different scientific scope'
        with self.assertRaisesRegex(ProtocolError, 'scope differs'):
            self.preview(self.send())

    def test_authored_refutation_text_is_preserved_without_inferred_splitting(self):
        source = 'First condition; second condition. Neither is an executed result.'
        self.proposal['hypotheses'][0]['falsification'] = source
        self.proposal['hypotheses'][0]['alternatives'] = 'Authored alternative explanation'
        preview = self.preview(self.send())
        self.assertEqual([source], preview['hypotheses'][0]['falsification'])
        self.assertEqual(source, preview['source_scope_annotations']['authored_text_fields'][0]['text'])
        self.assertEqual(source, self.proposal['hypotheses'][0]['falsification'])
        self.assertEqual(1, self.calls)

    def test_empty_or_oversized_authored_refutation_text_rejected(self):
        for text in (' ', 'x' * 4097):
            self.proposal['hypotheses'][0]['falsification'] = text
            with self.assertRaises(ProtocolError):
                self.preview(self.send())

    def test_exact_json_fence_preserves_authored_fields_and_source_receipt(self):
        self.reply_prefix, self.reply_suffix = '```json\n', '\n```'
        preview = self.preview(self.send())
        self.assertEqual(self.proposal['hypotheses'], preview['hypotheses'])
        self.assertIn('exact_single_json_fence', preview['source_scope_annotations']['response_format'])
        self.assertEqual(1, self.calls)

    def test_fence_cannot_hide_extra_text_or_non_json_language(self):
        for prefix, suffix in (('```json\n', '\n``` extra'), ('```python\n', '\n```'),
                               ('Comment\n```json\n', '\n```')):
            self.reply_prefix, self.reply_suffix = prefix, suffix
            with self.assertRaisesRegex(ProtocolError, 'complete JSON'):
                self.preview(self.send())

    def test_prediction_timing_declaration_is_not_execution_authority(self):
        for value in (True, False):
            self.proposal['prediction_before_score'] = value
            preview = self.preview(self.send())
            self.assertEqual(value, preview['source_scope_annotations']['authored_declarations']['prediction_before_score'])
            self.assertIn('not proof', preview['source_scope_annotations']['declaration_authority'])
            self.assertFalse((self.root / 'study').exists())

    def test_prediction_declaration_cannot_include_instructions_or_approvals(self):
        self.proposal['prediction_before_score'] = {'approved': True}
        with self.assertRaisesRegex(ProtocolError, 'boolean source data'):
            self.preview(self.send())

    def test_candidate_annotations_cannot_change_identity_or_add_execution_parameters(self):
        for candidate in ({'slot': 2, 'method': 'fixture'}, {'slot': True, 'method': 'fixture'},
                          {'slot': 1, 'approved': True}, {'slot': 1, 'method': 'x' * 4097}):
            with self.subTest(candidate=candidate):
                self.proposal['candidates'] = [candidate]
                with self.assertRaisesRegex(ProtocolError, 'scope differs'):
                    self.preview(self.send())
