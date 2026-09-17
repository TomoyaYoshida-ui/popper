import json
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.orchestrator import Orchestrator, CampaignFatal, SUCCESS


def _make_run():
    return Orchestrator(Path(tempfile.mkdtemp()))


def _ok(key_result):
    return lambda run_dir, state: {"outcome": SUCCESS, "value": key_result}


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.run = _make_run()

    def test_init_and_topo_respects_dependencies(self):
        steps = [
            {"key": "ideate", "fn": _ok("card")},
            {"key": "scoop", "needs": ["ideate"], "fn": _ok("evidence")},
            {"key": "eval", "needs": ["scoop"], "fn": _ok("delta")},
        ]
        self.run.init("objective", steps)
        compiled, chain = self.run.build_graph()
        # 拓扑序应遵守依赖
        self.assertEqual(["ideate", "scoop", "eval"], chain)
        final = compiled.invoke({"objective": "objective", "status": "pending",
                                 "step": None, "steps": []})
        self.assertEqual("completed", final["status"])
        self.assertIn("ideate", [s["key"] for s in final["steps"]])

    def test_fatal_stops_with_failed_status(self):
        def boom(run_dir, state):
            raise CampaignFatal("argh")
        steps = [{"key": "a", "fn": _ok("x")}, {"key": "b", "fn": boom}]
        self.run.init("obj", steps)
        compiled, chain = self.run.build_graph()
        final = compiled.invoke({"objective": "obj", "status": "pending", "step": None, "steps": []})
        self.assertEqual("failed", final["status"])

    def test_fatal_prevents_downstream_side_effects(self):
        for raised in (True, False):
            with self.subTest(raised=raised):
                calls = []
                def fail(run_dir, state):
                    if raised:
                        raise CampaignFatal("controlled failure")
                    return {"outcome": "fatal"}
                def downstream(run_dir, state):
                    calls.append("executed")
                    return {"outcome": SUCCESS}
                self.run.init("obj", [{"key": "a", "fn": fail},
                                      {"key": "b", "needs": ["a"], "fn": downstream}])
                result = self.run.run()
                self.assertEqual("failed", result["status"])
                self.assertEqual("a", result["final_step"])
                self.assertEqual([], calls)
                self.assertEqual("failed", self.run._load()["status"])
                self.assertFalse(self.run._all_done())

    def test_retry_stops_and_can_resume_successfully(self):
        calls = []
        outcomes = iter(["retryable", "success", "retryable"])
        def attempt(run_dir, state):
            return {"outcome": next(outcomes)}
        def downstream(run_dir, state):
            calls.append("executed")
            return {"outcome": SUCCESS}
        self.run.init("obj", [{"key": "a", "fn": attempt},
                              {"key": "b", "needs": ["a"], "fn": downstream}])
        self.assertEqual("retryable", self.run.run()["status"])
        self.assertEqual([], calls)
        self.assertFalse(self.run._all_done())
        self.assertEqual("completed", self.run.run()["status"])
        self.assertEqual(["executed"], calls)
        # A previous success must not hide the latest failed attempt.
        self.assertEqual("retryable", self.run.run()["status"])
        self.assertEqual(["executed"], calls)
        self.assertFalse(self.run._all_done())

    def test_single_retry_is_not_completed(self):
        self.run.init("obj", [{"key": "a", "fn": lambda *_: {"outcome": "retryable"}}])
        self.assertEqual("retryable", self.run.run()["status"])
        self.assertFalse(self.run._all_done())

    def test_legacy_variant_approval_requires_actual_review_proposal(self):
        self.run.init("obj", [{"key": "variant", "approval": "proposal_approved",
                                "fn": _ok("variant")}])
        self.assertEqual("completed", self.run.run()["status"])
        proposal = self.run.run_dir / "proposal"
        proposal.mkdir()
        path = proposal / "proposal.json"
        for payload in ('{"status":"failed"}', 'not json', '[]'):
            path.write_text(payload, encoding="utf-8")
            self.assertEqual("completed", self.run.run()["status"])
        path.write_text(json.dumps({"status": "review_required"}), encoding="utf-8")
        self.assertEqual("waiting_approval", self.run.run()["status"])
        self.assertEqual("completed", self.run.run(config={"proposal_approved": True})["status"])
        self.assertNotIn("waiting_approval", self.run._load())

    def test_custom_unconditional_approval_still_waits(self):
        self.run.init("obj", [{"key": "publish", "approval": "human_approval",
                                "fn": _ok("published")}])
        self.assertEqual("waiting_approval", self.run.run()["status"])
        self.assertEqual("completed", self.run.run(config={"human_approval": True})["status"])

    def test_cycle_after_valid_prefix_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "依赖环"):
            self.run._topo([{"key": "a"}, {"key": "b", "needs": ["c"]},
                            {"key": "c", "needs": ["b"]}])

    def test_history_records_outcome_for_resume(self):
        steps = [{"key": "a", "fn": _ok("x")}, {"key": "b", "fn": _ok("y")}]
        self.run.init("obj", steps)
        self.run.run()
        data = self.run._load()
        self.assertEqual(["a", "b"], [h["step"] for h in data["history"]])
        self.assertEqual("completed", data["status"])

    def test_topology_cycle_rejected(self):
        steps = [{"key": "a", "needs": ["b"], "fn": _ok("x")},
                 {"key": "b", "needs": ["a"], "fn": _ok("y")}]
        self.run.init("obj", steps)
        with self.assertRaisesRegex(ProtocolError, "依赖环"):
            self.run.build_graph()


if __name__ == "__main__":
    unittest.main()
