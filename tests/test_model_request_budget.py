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
