import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.research.agent_evaluation_budget import AgentEvaluationBudget


class CampaignBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "campaign.sqlite"
        self.ledger = AgentEvaluationBudget(self.path, 2, 100)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_all_arms_share_ceiling_and_snapshot(self):
        receipt = self.ledger.reserve("single-shot-1", {"system": "rules", "messages": []}, 60)
        self.assertEqual([], receipt["snapshot"]["messages"])
        self.ledger.settle("single-shot-1", 50)
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("loop-1", {"messages": []}, 51)
        self.ledger.reserve("loop-1", {"messages": []}, 50)
        self.ledger.settle("loop-1", 30)
        self.ledger.settle("loop-1", 30)
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("third", {}, 1)
        self.assertEqual(80, self.ledger.status()["used_tokens"])

    def test_unsent_transport_proof_releases_budget_but_ambiguous_failure_does_not(self):
        from popper.request_lifecycle import RequestFailure
        self.ledger.reserve('unsent',{},100)
        ambiguous=RequestFailure('connect_timeout',sent=True,transport_phase='connecting')
        with self.assertRaises(ProtocolError): self.ledger.settle_not_sent('unsent',ambiguous)
        proof=RequestFailure('connect_timeout',sent=False,transport_phase='connecting',output_started=False)
        self.ledger.settle_not_sent('unsent',proof)
        self.ledger.settle_not_sent('unsent',proof)
        self.assertEqual(0,self.ledger.status()['unresolved_requests'])
        self.assertEqual(2,self.ledger.status()['remaining_calls'])
        self.ledger.reserve('next',{},100)
        self.ledger.settle('next',100)
        self.assertEqual(100,self.ledger.status()['used_tokens'])
        with self.assertRaises(ProtocolError): self.ledger.settle('unsent',10)

    def test_ambiguous_request_blocks_after_restart(self):
        self.ledger.reserve("one", {}, 50)
        self.ledger.close()
        self.ledger = AgentEvaluationBudget(self.path, 2, 100)
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("two", {}, 20)
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("one", {}, 50)

    def test_unknown_usage_blocks_new_calls(self):
        self.ledger.reserve("one", {}, 50)
        self.ledger.settle("one", None)
        self.assertEqual(1, self.ledger.status()["unresolved_requests"])
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("two", {}, 10)

    def test_known_over_reservation_uses_actual_charge_and_survives_restart(self):
        self.ledger.reserve("one", {}, 50)
        self.ledger.settle("one", 51)
        self.ledger.settle("one", 51)
        with self.assertRaises(ProtocolError):
            self.ledger.settle("one", None)
        with self.assertRaises(ProtocolError):
            self.ledger.settle("one", 50)
        self.ledger.close()
        self.ledger = AgentEvaluationBudget(self.path, 2, 100)
        self.assertEqual(0, self.ledger.status()["unresolved_requests"])
        self.assertEqual(1, self.ledger.status()["over_reservation_requests"])
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("two", {}, 50)
        self.ledger.reserve("two", {}, 49)

    def test_actual_usage_above_total_ceiling_blocks(self):
        self.ledger.reserve("one", {}, 50)
        self.ledger.settle("one", 101)
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("two", {}, 1)

    def test_snapshot_redacted_and_duplicate_not_redispatched(self):
        receipt = self.ledger.reserve("one", {"messages": [{"role": "user", "content": '{"api_key":"secret-value"}'}]}, 50)
        self.assertNotIn("secret-value", str(receipt))
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("one", {}, 50)
        self.ledger.settle("one", 10)
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("one", receipt["snapshot"], 50)

    def test_no_silent_budget_increase(self):
        with self.assertRaises(ProtocolError):
            AgentEvaluationBudget(self.path, 3, 1000)

    def test_explicit_amendment_retains_history_and_restart_policy(self):
        self.ledger.reserve("prior", {"messages": []}, 50)
        self.ledger.settle("prior", 30)
        rows = [tuple(r) for r in self.ledger.connection.execute("SELECT * FROM requests")]
        old, new = self.ledger.policy(), {"max_requests": 2, "max_tokens": 400000000}
        auth = {"actor": "user", "text": "4个亿的tocten额度"}
        self.assertEqual(new, self.ledger.amend_policy("human-1", old, new, auth))
        self.assertEqual(new, self.ledger.amend_policy("human-1", old, new, auth))
        self.assertEqual(rows, [tuple(r) for r in self.ledger.connection.execute("SELECT * FROM requests")])
        self.ledger.close()
        self.ledger = AgentEvaluationBudget(self.path)
        self.assertEqual(new, self.ledger.policy())
        self.assertEqual(30, self.ledger.status()["used_tokens"])
        self.ledger.reserve("next", {}, 1000)
        self.ledger.settle("next", 40)
        with self.assertRaises(ProtocolError):
            self.ledger.reserve("third", {}, 1)

    def test_amendment_conflicts_and_unknown_usage_fail_closed(self):
        old, new = self.ledger.policy(), {"max_requests": 2, "max_tokens": 200}
        auth = {"actor": "user", "text": "approved"}
        with self.assertRaises(ProtocolError):
            self.ledger.amend_policy("x", old, new, {"actor": "agent", "text": "approved"})
        with self.assertRaises(ProtocolError):
            self.ledger.amend_policy("x", {"max_requests": 3, "max_tokens": 100}, new, auth)
        self.ledger.reserve("unknown", {}, 50)
        self.ledger.settle("unknown", None)
        with self.assertRaises(ProtocolError):
            self.ledger.amend_policy("x", old, new, auth)
        self.ledger.settle("unknown", 30)
        self.ledger.amend_policy("x", old, new, auth)
        with self.assertRaises(ProtocolError):
            self.ledger.amend_policy("x", old, {"max_requests": 3, "max_tokens": 200}, auth)
        with self.assertRaises(ProtocolError):
            self.ledger.amend_policy("y", new, {"max_requests": 2, "max_tokens": 20}, auth)

    def aggregate_fixture(self):
        self.ledger.reserve('known', {}, 50)
        self.ledger.settle('known', 20)
        self.ledger.reserve('network-failed', {}, 50)
        self.ledger.settle('network-failed', None)
        return {'source_sha256': 'a'*64, 'scope_verified': True, 'kind': 'synthetic evidence'}

    def test_aggregate_retains_unknown_allocation_and_survives_restart(self):
        evidence = self.aggregate_fixture()
        result = self.ledger.reconcile_aggregate('receipt', ['known','network-failed'], 1, 23, evidence)
        self.assertEqual(23, result['used_tokens'])
        self.assertEqual(0, result['unresolved_requests'])
        self.assertEqual(1, result['unallocated_usage_requests'])
        self.assertIsNone(self.ledger.connection.execute("SELECT used FROM requests WHERE id='network-failed'").fetchone()[0])
        self.assertEqual(result, self.ledger.reconcile_aggregate('receipt', ['network-failed','known'], 1, 23, evidence))
        self.ledger.close()
        self.ledger = AgentEvaluationBudget(self.path, 2, 100)
        self.assertEqual(result, self.ledger.status())
        with self.assertRaises(ProtocolError):
            self.ledger.reserve('third', {}, 1)
        with self.assertRaises(ProtocolError):
            self.ledger.settle('network-failed', 3)

    def test_aggregate_rejects_mismatches_and_overlap(self):
        evidence = self.aggregate_fixture()
        for count, tokens in ((2,23),(1,19),(1,71)):
            with self.assertRaises(ProtocolError):
                self.ledger.reconcile_aggregate('bad', ['known','network-failed'], count, tokens, evidence)
        self.assertEqual(1, self.ledger.status()['unresolved_requests'])
        self.ledger.reconcile_aggregate('receipt', ['known','network-failed'], 1, 23, evidence)
        with self.assertRaises(ProtocolError):
            self.ledger.reconcile_aggregate('receipt', ['known','network-failed'], 1, 24, evidence)
        with self.assertRaises(ProtocolError):
            self.ledger.reconcile_aggregate('overlap', ['known','network-failed'], 1, 23, evidence)

    def test_aggregate_adjustment_participates_in_token_ceiling(self):
        self.ledger.close()
        path = Path(self.temp.name) / 'three.sqlite'
        self.ledger = AgentEvaluationBudget(path, 3, 100)
        evidence = self.aggregate_fixture()
        self.ledger.reconcile_aggregate('receipt', ['known','network-failed'], 1, 23, evidence)
        with self.assertRaises(ProtocolError):
            self.ledger.reserve('third', {}, 78)
        self.ledger.reserve('third', {}, 77)

    def test_aggregate_cannot_clear_inflight_or_unverified_scope(self):
        self.ledger.reserve('inflight', {}, 50)
        with self.assertRaises(ProtocolError):
            self.ledger.reconcile_aggregate('receipt', ['inflight'], 0, 0,
                {'source_sha256':'a'*64,'scope_verified':True})
        self.ledger.settle('inflight', None)
        with self.assertRaises(ProtocolError):
            self.ledger.reconcile_aggregate('receipt', ['inflight'], 0, 0,
                {'source_sha256':'a'*64,'scope_verified':False})


if __name__ == "__main__":
    unittest.main()
