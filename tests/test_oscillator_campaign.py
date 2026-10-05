"""Disposable simulation fixtures. No paid model calls or confirmations."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from popper.chat import ChatText
from popper.core import ProtocolError, file_hash, read_json, write_json
from popper.domains.oscillator_trajectory import PACK
from popper.research.agent_evaluation_budget import AgentEvaluationBudget
from popper.research.oscillator_campaign import (prepare, load_campaign, arm_campaign, propose,
    approve_study, initial_code_preview, approve_initial_code, freeze_execution_revision, protected_files)
from popper.research.oscillator_study import baseline_rk4
from tests.test_oscillator_broker import row


class CampaignPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.study = self.root / 'synthetic-study'
        private, public = self.study / 'evaluator-private', self.study / 'agent-visible'
        private.mkdir(parents=True)
        public.mkdir()
        for frequency, split in enumerate(('train', 'dev', 'test'), start=1):
            case = {**row(), 'id': split, 'omega': frequency}
            states, _ = baseline_rk4(case)
            write_json(private / (split + '.json'), [{**case, 'reference': states}])
            if split != 'test':
                write_json(public / (split + '.json'), PACK.project_inputs([case]))
        write_json(self.study / 'baseline-development.json', {'trajectory_error': .15})
        write_json(public / 'initial-material-v1.json', {'objective': 'synthetic scope',
            'common_instructions': ['synthetic materials only'], 'literature': {'scope': 'fixture'},
            'arm_rules': {'single_shot': 'no feedback', 'popper_loop': 'feedback allowed'}})
        repo = Path(__file__).resolve().parents[1]
        write_json(self.study / 'initial-material-receipt-v1.json', {
            'study_artifacts_sha256': {str(path.relative_to(self.study)).replace('\\', '/'): file_hash(path)
                                      for path in public.glob('*.json')},
            'repository_artifacts_sha256': {'popper/research/oscillator_baseline.py':
                                            file_hash(repo / 'popper/research/oscillator_baseline.py')}})
        artifacts = {str(path.relative_to(self.study)).replace('\\', '/'): file_hash(path)
                     for path in self.study.rglob('*.json')}
        write_json(self.study / 'protocol.json', {'artifacts': artifacts})
        write_json(self.study / 'protocol-receipt.json', {'protocol_sha256': file_hash(self.study / 'protocol.json')})
        self.campaign = self.root / 'campaign'
        self.manifest = prepare(self.study, self.campaign, 'synthetic-model', 'http://127.0.0.1:1/v1')

    def tearDown(self):
        self.temp.cleanup()

    def arm_simulated(self):
        log = self.root / 'simulated-regression.log'
        # Explicit synthetic gate fixture; never reused as real regression evidence.
        log.write_text('1102 passed, 5 skipped, 581 subtests passed in 1s\n', encoding='utf-8')
        with patch('popper.research.oscillator_campaign.probe_read_isolation', return_value={
                'passed': True, 'protected_count': 1, 'host_read_verified': True, 'network_block': False}):
            return arm_campaign(self.campaign, log)

    def reply(self, run_id):
        manifest, run = load_campaign(self.campaign, run_id)
        spec = read_json(Path(run['project']) / 'experiment.json')
        return {'kind': 'oscillator_hypotheses_v1', 'objective': spec['objective'],
                'candidates': spec['candidates'], 'hypotheses': [{
                    'candidate_index': index, 'mechanism': 'synthetic mechanism',
                    'applicability': 'simulation', 'predictions': ['synthetic prediction'],
                    'falsification': ['synthetic refutation'], 'alternatives': ['synthetic alternative']}
                    for index in range(6)],
                'implementation': {'candidate_index': 0, 'path': 'model.py',
                                   'replacement': 'def solve(rhs,y0,times,budget,config):\n    return [list(y0) for _ in times]\n'}}

    def test_six_independent_projects_same_material_no_studies(self):
        self.assertEqual(6, len(self.manifest['runs']))
        sources = []
        for run in self.manifest['runs']:
            self.assertFalse(Path(run['research']).exists())
            sources.append(file_hash(Path(run['project']) / 'model.py'))
            self.assertNotIn('reference', read_json(Path(run['project']) / 'public-dev.json')[0])
        self.assertEqual(1, len(set(sources)))
        with self.assertRaises(ProtocolError):
            prepare(self.study, self.campaign, 'synthetic-model', 'http://127.0.0.1:1/v1')

    def test_new_provider_campaign_keeps_shared_request_and_token_usage(self):
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as budget:
            budget.reserve('old-provider-failed', {}, 50)
            budget.settle('old-provider-failed', 30)
        fresh = self.root / 'new-provider'
        manifest = prepare(self.study, fresh, 'new-synthetic-model', 'http://127.0.0.1:2/v1',
                           shared_budget=self.manifest['budget_ledger'])
        self.assertEqual(['old-provider-failed'], manifest['preexisting_request_ids'])
        self.assertEqual(self.manifest['budget_ledger'], manifest['budget_ledger'])
        old_private = Path(self.manifest['runs'][0]['project']) / 'test.json'
        self.assertIn(old_private, protected_files(manifest))
        log = self.root / 'synthetic-new-log.txt'
        log.write_text('1120 passed, 5 skipped, 581 subtests passed in 1s\n')
        with patch('popper.research.oscillator_campaign.probe_read_isolation', return_value={
                'passed':True,'protected_count':1,'host_read_verified':True,'network_block':False}):
            arm = arm_campaign(fresh, log)
        self.assertEqual(1, arm['formal_requests_before_arm'])
        with closing(AgentEvaluationBudget(manifest['budget_ledger'])) as budget:
            self.assertEqual(30, budget.status()['used_tokens'])

    def test_shared_budget_change_after_prepare_blocks_arm(self):
        fresh = self.root / 'new-provider'
        prepare(self.study, fresh, 'new-synthetic-model', 'http://127.0.0.1:2/v1',
                shared_budget=self.manifest['budget_ledger'])
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as budget:
            budget.reserve('unexpected', {}, 50)
            budget.settle('unexpected', 1)
        log = self.root / 'synthetic-log.txt'
        log.write_text('1120 passed, 5 skipped, 581 subtests passed in 1s\n')
        with self.assertRaises(ProtocolError):
            arm_campaign(fresh, log)

    def test_unarmed_or_changed_material_cannot_dispatch(self):
        with self.assertRaises(FileNotFoundError):
            propose(self.campaign, 'single_shot-1', chat_llm=lambda *_: 'never')
        run = self.manifest['runs'][0]
        Path(run['project'], 'public-dev.json').write_text('[]')
        with self.assertRaises(ProtocolError):
            load_campaign(self.campaign)

    def test_execution_revision_retains_failed_attempt_and_shared_budget(self):
        self.arm_simulated()
        original = file_hash(self.campaign / 'campaign.json')
        first = propose(self.campaign, 'single_shot-1', chat_llm=lambda *_:
                        ChatText('invalid JSON', {'total_tokens': 11}))
        prior_answer = file_hash(self.campaign / 'single_shot-1/initial-answer.json')
        prior_arm = file_hash(self.campaign / 'armed.json')
        frozen = freeze_execution_revision(self.campaign, 'synthetic infrastructure revision')
        self.assertEqual('frozen_not_armed', frozen['status'])
        with self.assertRaises(FileNotFoundError):
            propose(self.campaign, 'single_shot-2', chat_llm=lambda *_: 'must not dispatch')
        self.arm_simulated()
        self.assertEqual('already_attempted_do_not_redispatch', propose(
            self.campaign, 'single_shot-1', chat_llm=lambda *_: 'never')['status'])
        self.assertEqual('invalid_initial_answer', first['status'])
        self.assertEqual(original, file_hash(self.campaign / 'campaign.json'))
        self.assertEqual(prior_arm, file_hash(self.campaign / 'armed.json'))
        self.assertEqual(prior_answer, file_hash(self.campaign / 'single_shot-1/initial-answer.json'))
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as ledger:
            self.assertEqual(11, ledger.status()['used_tokens'])
            self.assertEqual(1, ledger.status()['request_count'])

    def test_execution_revision_cannot_hide_material_changes_or_unknown_usage(self):
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as ledger:
            ledger.reserve('unknown', {'synthetic': True}, 100)
            ledger.settle('unknown', None)
        with self.assertRaises(ProtocolError):
            freeze_execution_revision(self.campaign, 'cannot reset budget')
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as ledger:
            ledger.settle('unknown', 12)
        Path(self.manifest['runs'][0]['project'], 'public-dev.json').write_text('[]')
        with self.assertRaises(ProtocolError):
            freeze_execution_revision(self.campaign, 'cannot change research')

    def test_execution_revision_chain_and_arm_are_bound(self):
        freeze_execution_revision(self.campaign, 'first synthetic revision')
        self.arm_simulated()
        freeze_execution_revision(self.campaign, 'second synthetic revision')
        with self.assertRaises(FileNotFoundError):
            propose(self.campaign, 'single_shot-2', chat_llm=lambda *_: 'never')
        second = self.campaign / 'execution-revisions/0002.json'
        data = read_json(second)
        data['parent_sha256'] = '0' * 64
        write_json(second, data)
        with self.assertRaises(ProtocolError):
            load_campaign(self.campaign)

    def test_execution_revision_requires_post_freeze_full_regression(self):
        import os
        log = self.root / 'old-regression.log'
        log.write_text('1115 passed, 5 skipped, 581 subtests passed in 1s\n')
        os.utime(log, (1, 1))
        freeze_execution_revision(self.campaign, 'new source snapshot')
        with self.assertRaises(ProtocolError):
            arm_campaign(self.campaign, log)

    def test_offline_repair_snapshot_never_clears_unknown_usage_or_arms(self):
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as budget:
            budget.reserve('unresolved', {}, 50)
            budget.settle('unresolved', None)
        result = freeze_execution_revision(self.campaign, 'offline repair evidence only', offline_only=True)
        self.assertEqual('frozen_not_armed', result['status'])
        self.assertEqual(1, result['unresolved_requests_at_freeze'])
        with self.assertRaises(ProtocolError):
            self.arm_simulated()
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as budget:
            self.assertEqual(1, budget.status()['unresolved_requests'])
            with self.assertRaises(ProtocolError):
                budget.reserve('must-not-send', {}, 1)

    def test_execution_revision_cannot_change_scientific_sources(self):
        freeze_execution_revision(self.campaign, 'synthetic source revision')
        path = self.campaign / 'execution-revisions/0001.json'
        revision = read_json(path)
        key = str((Path(__file__).resolve().parents[1] / 'popper/domains/oscillator_trajectory.py').resolve())
        revision['implementation_sha256'][key] = '0' * 64
        write_json(path, revision)
        with self.assertRaisesRegex(ProtocolError, 'scoring or Research Kernel'):
            load_campaign(self.campaign)

    def test_one_budgeted_proposal_then_stops_and_no_repeat(self):
        self.arm_simulated()
        calls = []
        def fake(*_args):
            calls.append(True)
            return ChatText(json.dumps(self.reply('single_shot-1')), {'total_tokens': 27})
        result = propose(self.campaign, 'single_shot-1', chat_llm=fake)
        self.assertEqual('awaiting_user_study_approval', result['status'], result)
        self.assertEqual('synthetic', result['provider'])
        self.assertFalse(Path(self.manifest['runs'][0]['research']).exists())
        self.assertEqual('already_attempted_do_not_redispatch', propose(
            self.campaign, 'single_shot-1', chat_llm=fake)['status'])
        self.assertEqual(1, len(calls))
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as ledger:
            self.assertEqual(27, ledger.status()['used_tokens'])

    def test_unknown_usage_stops_other_runs_before_provider_io(self):
        self.arm_simulated()
        calls = []
        def unknown(*_args):
            calls.append(True)
            return ChatText(json.dumps(self.reply('single_shot-1')), {})
        result = propose(self.campaign, 'single_shot-1', chat_llm=unknown)
        self.assertEqual('failed', result['status'])
        def must_not_dispatch(*_args):
            raise AssertionError('unresolved ledger must block dispatch')
        with self.assertRaises(ProtocolError):
            propose(self.campaign, 'popper_loop-1', chat_llm=must_not_dispatch)
        self.assertEqual(1, len(calls))

    def test_crashed_model_job_restart_preserves_unknown_usage_and_never_redispatches(self):
        from popper.server import Workstation
        from popper.sessions import AgentSessionStore
        self.arm_simulated()
        _, run = load_campaign(self.campaign, 'single_shot-1')
        calls = []
        def must_not_call(*args):
            calls.append(args)
            raise AssertionError('interrupted request must never be redispatched')
        ws = Workstation(('127.0.0.1', 0), run['project'], trusted_local=True,
                         chat_llm=must_not_call, evaluation_budget_path=self.manifest['budget_ledger'])
        try:
            session = ws.create_agent_session('Synthetic timeout recovery')
            with closing(AgentSessionStore(run['project'])) as store:
                turn, _ = store.start_turn(session['session_id'], 'synthetic research',
                    'synthetic-timeout', actor='test-user', trace_id='trc_timeout', enqueue=True)
                job = store.agent_job_for_turn(turn['turn_id'])
                store.claim_agent_job(job['job_id'], worker_id='exited-test-worker')
            with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as ledger:
                ledger.reserve(turn['turn_id'], {'synthetic': 'persisted input'}, 30)
                ledger.settle(turn['turn_id'], None)
            write_json(self.campaign / 'single_shot-1/initial-request.json', {
                'session_id': session['session_id'], 'provider': 'synthetic'})
        finally:
            ws.server_close()
        reopened = Workstation(('127.0.0.1', 0), run['project'], trusted_local=True,
            chat_llm=must_not_call, evaluation_budget_path=self.manifest['budget_ledger'])
        try:
            self.assertEqual(0, reopened.wait_for_jobs(timeout=0))
            with closing(AgentSessionStore.readonly(run['project'])) as store:
                self.assertEqual('interrupted', store.get_turn(turn['turn_id'])['status'])
                self.assertEqual('interrupted', store.get_agent_job(job['job_id'])['status'])
                self.assertTrue(store.verify_turn_events()['ok'])
        finally:
            reopened.server_close()
        self.assertEqual('already_attempted_do_not_redispatch', propose(
            self.campaign, 'single_shot-1', chat_llm=must_not_call)['status'])
        with self.assertRaisesRegex(ProtocolError, 'unknown usage'):
            propose(self.campaign, 'single_shot-2', chat_llm=must_not_call)
        with closing(AgentEvaluationBudget(self.manifest['budget_ledger'])) as ledger:
            self.assertEqual(1, ledger.status()['request_count'])
            self.assertEqual(1, ledger.status()['unresolved_requests'])
            self.assertIsNone(ledger.connection.execute('SELECT used FROM requests').fetchone()[0])
        self.assertEqual([], calls)

    def test_incomplete_answer_is_retained_not_rescued(self):
        self.arm_simulated()
        result = propose(self.campaign, 'single_shot-1', chat_llm=lambda *_: ChatText('invalid JSON', {'total_tokens': 5}))
        self.assertEqual('invalid_initial_answer', result['status'])
        self.assertEqual('already_attempted_do_not_redispatch', propose(
            self.campaign, 'single_shot-1', chat_llm=lambda *_: 'never')['status'])

    def test_authored_study_and_first_source_are_separate_approvals_without_execution(self):
        self.arm_simulated()
        result = propose(self.campaign, 'single_shot-1', chat_llm=lambda *_: ChatText(
            json.dumps(self.reply('single_shot-1')), {'total_tokens': 27}))
        with self.assertRaises(ProtocolError):
            initial_code_preview(self.campaign, 'single_shot-1')
        with patch('popper.research.oscillator_campaign.freeze_binding', return_value={}):
            state = approve_study(self.campaign, 'single_shot-1', result['preview']['approval_sha256'])
        self.assertEqual('study_created_no_revision_or_experiment_executed', state['status'])
        checked = initial_code_preview(self.campaign, 'single_shot-1')
        with self.assertRaises(ProtocolError):
            approve_initial_code(self.campaign, 'single_shot-1', '0'*64)
        created = approve_initial_code(self.campaign, 'single_shot-1', checked['preview']['proposal_sha256'])
        self.assertEqual('immutable_unexecuted', created['result']['status'])
        self.assertEqual('synthetic', created['provider'])
        self.assertEqual(created, approve_initial_code(self.campaign, 'single_shot-1',
                                                     checked['preview']['proposal_sha256']))
        self.assertEqual(1, len(list((self.campaign / 'single_shot-1/research/revisions').glob('REV-*'))))

    def test_single_shot_development_limit_counts_queue_and_rejects_second_candidate(self):
        from popper.server import Workstation
        from popper.research.revisions import RevisionStore, CodeEdit
        self.arm_simulated()
        run_id = 'single_shot-1'
        answer = propose(self.campaign, run_id, chat_llm=lambda *_: ChatText(
            json.dumps(self.reply(run_id)), {'total_tokens': 27}))
        with patch('popper.research.oscillator_campaign.freeze_binding', return_value={}):
            approve_study(self.campaign, run_id, answer['preview']['approval_sha256'])
        checked = initial_code_preview(self.campaign, run_id)
        first = approve_initial_code(self.campaign, run_id, checked['preview']['proposal_sha256'])['result']
        _, run = load_campaign(self.campaign, run_id)
        revisions = RevisionStore(Path(run['research']) / 'revisions')
        second = revisions.create(run['project'], checked['spec']['hypothesis_id'], checked['spec']['design_id'],
            [CodeEdit('model.py', 'def solve(*args):\n    print("second fixture")\n    return []\n',
                      checked['spec']['expected_sha256'])], actor='agent')
        ws = Workstation(('127.0.0.1', 0), run['project'], trusted_local=True,
                         research_runs=[run['research']], evaluation_budget_path=self.manifest['budget_ledger'])
        try:
            with patch.object(ws, '_start_revision_job'):
                queued = ws.execute_agent_revision(first['revision_id'], first['manifest_sha256'])
                repeated = ws.execute_agent_revision(first['revision_id'], first['manifest_sha256'])
                self.assertEqual(queued['job']['job_id'], repeated['job']['job_id'])
                with self.assertRaises(ProtocolError):
                    ws.execute_agent_revision(second['revision_id'], file_hash(
                        revisions.path(second['revision_id']) / 'revision.json'))
        finally:
            ws.server_close()
