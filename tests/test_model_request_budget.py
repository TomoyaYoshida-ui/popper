import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from popper.core import ProtocolError
from popper.model_budget import ModelRequestBudget


class RequestBudgetTests(unittest.TestCase):
    def budget(self, path, scope="one", calls=2):
        return ModelRequestBudget(path, global_tokens=20000, global_calls=calls,
                                  scope=scope, scope_tokens=20000, scope_calls=calls)

    def test_missing_usage_keeps_reservation_and_blocks_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calls.sqlite"
            budget = self.budget(path)
            request = budget.reserve({"messages": [], "max_tokens": 100})
            with self.assertRaises(ProtocolError):
                budget.settle(request, None)
            self.assertGreater(budget.tokens_used, 0)
            budget.close()
            restored = self.budget(path)
            with self.assertRaises(ProtocolError):
                restored.reserve({"messages": [], "max_tokens": 100})
            self.assertEqual(1, restored.snapshot()["global"]["calls"])
            restored.close()

    def test_concurrent_scopes_cannot_overbook_global_call_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calls.sqlite"
            first, second = self.budget(path, "a", 1), self.budget(path, "b", 1)
            def reserve(budget):
                try:
                    return budget.reserve({"messages": [], "max_tokens": 100})
                except ProtocolError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(reserve, (first, second)))
            self.assertEqual(1, sum(r is not None for r in results))
            for b in (first, second):
                b.close()

    def test_settlement_releases_unused_tokens_but_attempts_remain_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = self.budget(Path(tmp) / "calls.sqlite")
            body = {"messages": [], "max_tokens": 100}
            request = budget.reserve(body)
            budget.settle(request, {"total_tokens": 25})
            budget.settle(budget.reserve(body), {"total_tokens": 25})
            self.assertEqual(50, budget.tokens_used)
            with self.assertRaises(ProtocolError):
                budget.reserve(body)
            budget.close()

    def test_availability_policy_retains_unknown_bounds_and_prevents_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget=ModelRequestBudget(Path(tmp)/'calls.sqlite',global_tokens=100000,global_calls=3,
                scope='interactive',scope_tokens=100000,scope_calls=3,unknown_usage_policy='reserve')
            body={'messages':[],'max_tokens':100}
            request=budget.reserve(body,logical_request_id='turn-one')
            budget.settle(request,None,complete=False,output_started=True,transport_phase='generating')
            with self.assertRaises(ProtocolError):
                budget.reserve(body,logical_request_id='turn-one',attempt_index=2,pre_output_retry=True)
            next_request=budget.reserve(body,logical_request_id='turn-two')
            budget.settle(next_request,{'total_tokens':25})
            balance=budget.snapshot()['global']
            self.assertEqual(25,balance['known_tokens'])
            self.assertGreater(balance['unresolved_reserved_tokens'],0)
            self.assertGreater(balance['accounted_tokens'],25)
            budget.close()

    def test_partial_usage_is_not_final_bill_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget=ModelRequestBudget(Path(tmp)/'calls.sqlite',global_tokens=100000,global_calls=2,
                scope='interactive',scope_tokens=100000,scope_calls=2,unknown_usage_policy='reserve')
            request=budget.reserve({'messages':[],'max_tokens':100})
            for _ in range(2):
                budget.settle(request,{'total_tokens':17},complete=False,output_started=True)
            row=budget.snapshot()['attempts'][0]
            self.assertEqual('partial',row['usage_status'])
            self.assertEqual('provider_partial',row['usage_source'])
            self.assertEqual(17,row['reported_tokens'])
            self.assertIsNone(row['used_tokens'])
            self.assertGreater(budget.tokens_used,17)
            budget.close()

    def test_proven_not_sent_releases_call_and_token_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget=self.budget(Path(tmp)/'calls.sqlite',calls=1)
            body={'messages':[],'max_tokens':100}
            request=budget.reserve(body,logical_request_id='one')
            budget.settle(request,None,sent=False,complete=False,transport_phase='connecting')
            self.assertEqual(0,budget.tokens_used)
            self.assertEqual(0,budget.snapshot()['global']['calls'])
            self.assertEqual(1,budget.snapshot()['global']['attempts'])
            budget.settle(request,None,sent=False,complete=False)
            budget.settle(budget.reserve(body,logical_request_id='one',attempt_index=2),{'total_tokens':25})
            self.assertEqual(1,budget.snapshot()['global']['calls'])
            budget.close()
