"""Registered selection uses the existing one-consumption confirmation engine."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.transfer_tasks import prepare
from popper.capabilities import extra_available
from popper.core import ProtocolError, digest, initialize, read_json, write_json
from popper.research.actions import ADD_CONTROL, ActionProposal
from popper.research.contracts import HypothesisStatus
from popper.research.controller import ResearchController
from popper.research.project_adapter import RegisteredProjectAdapter


@unittest.skipUnless(extra_available('figs'), 'figs validation environment required')
class RegisteredConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.run = self.root / 'research'
        prepare('T03', self.project, self.root / 'cache')
        # Exercise the explicit registered-confirmation API while preserving
        # the old confirm wrapper's descriptive eligibility gate in this fixture.
        spec = read_json(self.project / 'experiment.json')
        spec.pop('metadata', None)
        write_json(self.project / 'experiment.json', spec)
        protocol = read_json(self.project / 'judgment-protocol.json')
        protocol['confirmation_policy'] = {
            'schema': 'selected_by_registered_dev_metric/v1',
            'selection_metric': 'candidate_mae', 'direction': 'min',
            'tie_break': 'hypothesis_id'}
        write_json(self.project / 'judgment-protocol.json', protocol)
        self.controller = None

    def tearDown(self):
        if self.controller:
            self.controller.close()
        self.temp.cleanup()

    def develop(self, *, registered_policy=True):
        if not registered_policy:
            path = self.project / 'judgment-protocol.json'
            protocol = read_json(path)
            protocol.pop('confirmation_policy')
            write_json(path, protocol)
        initialize(self.project)
        ResearchController.initialize(self.project, self.run, budget_cap=7)
        self.controller = ResearchController(self.run)
        c = self.controller
        hid = c.manifest['candidate_hypothesis_ids'][2]  # CART, no model request.
        c.execute_registered_candidate(hid,
            expected_version=c.store.get('study', c.manifest['study_id'])['state_version'],
            trusted_local=True)
        c.store.set_hypothesis_status(hid, 1, HypothesisStatus.INCONCLUSIVE)
        receipt = RegisteredProjectAdapter(c).confirmation_selection_receipt()
        self.assertEqual(hid, receipt['hypothesis_id'])
        return receipt

    def confirm(self, receipt):
        return self.controller.confirm_registered_candidate(receipt['hypothesis_id'],
            expected_version=receipt['state_version'], selection_receipt=receipt,
            trusted_local=True)

    def consumed(self):
        return self.controller.exp.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='test_consumed'").fetchone()[0]

    def reopen(self):
        self.controller.close()
        self.controller = ResearchController(self.run)

    def test_inconclusive_selection_confirms_without_fabricating_support_and_replays(self):
        receipt = self.develop()
        with self.assertRaisesRegex(ProtocolError, '没有达到开发阈值'):
            self.controller.confirm(trusted_local=True)
        status = self.confirm(receipt)
        self.assertEqual('concluded', status['phase'])
        self.assertEqual('inconclusive', self.controller.store.get(
            'hypothesis', receipt['hypothesis_id'])['status'])
        self.assertEqual(1, self.consumed())
        self.assertEqual(4., status['budget']['spent'])
        self.assertTrue(status['integrity']['ok'])
        kinds = self.controller.store._conn.execute(
            "SELECT kind FROM reservations WHERE item LIKE 'RUN-confirm-%'").fetchall()
        self.assertEqual([('confirmation',), ('confirmation',)], [tuple(row) for row in kinds])
        self.reopen()
        with patch.object(self.controller.exp, 'evaluate', side_effect=AssertionError('test replay')):
            self.assertEqual('concluded', self.confirm(receipt)['phase'])
        self.assertEqual(1, self.consumed())

    def test_unregistered_policy_cannot_bypass_original_gate(self):
        receipt = self.develop(registered_policy=False)
        with self.assertRaisesRegex(ProtocolError, '未冻结候选选择确认策略'):
            self.confirm(receipt)
        self.assertEqual(0, self.consumed())

    def test_stale_or_rehashed_wrong_selection_is_rejected_before_consumption(self):
        receipt = self.develop()
        altered = {**receipt, 'value': receipt['value'] + .001}
        altered['receipt_sha256'] = digest({k: v for k, v in altered.items() if k != 'receipt_sha256'})
        with self.assertRaisesRegex(ProtocolError, '重算不一致'):
            self.confirm(altered)
        self.controller._record(ActionProposal(ADD_CONTROL, 'An unrelated durable decision',
            receipt['hypothesis_id']))
        with self.assertRaisesRegex(ProtocolError, '陈旧状态版本'):
            self.confirm(receipt)
        self.assertEqual(0, self.consumed())

    def test_scorer_failure_recovers_saved_predictions_without_reexecution(self):
        receipt = self.develop()
        c = self.controller
        candidate_config = c.manifest['configs'][receipt['hypothesis_id']]
        score = c.evaluator.score_core_result
        def fail_candidate(state, result):
            if result['config'] == candidate_config:
                raise RuntimeError('scorer interruption')
            return score(state, result)
        with patch.object(c.evaluator, 'score_core_result', side_effect=fail_candidate):
            with self.assertRaisesRegex(RuntimeError, 'scorer interruption'):
                self.confirm(receipt)
        self.assertEqual('completed', c.exp.state()['phase'])
        self.assertEqual(1, self.consumed())
        self.reopen()
        with patch.object(self.controller.exp, 'evaluate', side_effect=AssertionError('test replay')):
            status = self.confirm(receipt)
        self.assertEqual('concluded', status['phase'])
        self.assertEqual(4., status['budget']['spent'])
        self.assertEqual(1, self.consumed())

    def test_legacy_dev_kind_confirmation_reservations_recover_in_place(self):
        receipt = self.develop()
        c = self.controller
        ledger = c.store.budget(c.manifest['study_id'])
        for role, hid in (('control', c.manifest['control_hypothesis_id']),
                          ('candidate', receipt['hypothesis_id'])):
            ledger.reserve(c.manifest['family_id'], f'RUN-confirm-{role}-{hid}', 1., kind='dev')
        self.assertEqual('concluded', self.confirm(receipt)['phase'])
        rows = c.store._conn.execute(
            "SELECT kind,status,spent FROM reservations WHERE item LIKE 'RUN-confirm-%'").fetchall()
        self.assertEqual([('dev', 'settled', 1.), ('dev', 'settled', 1.)], [tuple(row) for row in rows])
        self.assertEqual(1, self.consumed())

    def test_consumed_execution_failure_is_accounted_and_never_retried(self):
        receipt = self.develop()
        c = self.controller
        candidate_config = c.manifest['configs'][receipt['hypothesis_id']]
        evaluate = c.exp.evaluate
        def fail_candidate(config, split, *args, **kwargs):
            if split == 'test' and config == candidate_config:
                raise RuntimeError('candidate interruption')
            return evaluate(config, split, *args, **kwargs)
        with patch.object(c.exp, 'evaluate', side_effect=fail_candidate):
            with self.assertRaisesRegex(RuntimeError, 'candidate interruption'):
                self.confirm(receipt)
        self.assertEqual('confirmation_failed', c.exp.state()['phase'])
        self.reopen()
        with patch.object(self.controller.exp, 'evaluate', side_effect=AssertionError('test replay')):
            with self.assertRaisesRegex(ProtocolError, '禁止重复执行'):
                self.confirm(receipt)
        budget = self.controller.status()['budget']
        self.assertEqual(4., budget['spent'])
        self.assertEqual(0., budget['reserved'])
        self.assertEqual(1, self.consumed())
