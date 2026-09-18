"""Harness 接入契约 · 「Agent 提案，内核裁决」边界测试。"""
import unittest
from unittest.mock import Mock, patch

from popper.core import ProtocolError
from popper.harness import (Harness, JSONAgentHarness, PolicyHarness, for_policy,
                            harnesses, names, parse_revision_proposal, resolve)
from popper.research.controller import ResearchController
from popper.research.models import DeepSeekResearchPolicy, EvidenceDrivenPolicy


class HarnessRegistryTests(unittest.TestCase):
    def test_registry_exposes_registered_harnesses(self):
        self.assertEqual(("json_agent",), names())
        self.assertEqual(1, len(harnesses()))
        self.assertIs(JSONAgentHarness, resolve("json_agent"))

    def test_unknown_name_lists_available_harnesses(self):
        with self.assertRaisesRegex(ProtocolError, "json_agent"):
            resolve("codex")

    def test_policy_without_proposal_has_no_harness(self):
        self.assertIsNone(for_policy(EvidenceDrivenPolicy()))

    def test_json_agent_satisfies_the_protocol(self):
        harness = JSONAgentHarness(Mock(), model="m")
        self.assertIsInstance(harness, Harness)
        self.assertEqual("json_agent", harness.name)


class PolicyHarnessTests(unittest.TestCase):
    class Policy(EvidenceDrivenPolicy):
        name = "test_policy"
        model = "test-model"

        def propose_revision(self, objective, hypothesis, config, code_files, **kwargs):
            return {"edits": (), "rationale": "first"}

    def test_adapter_keeps_policy_identity_visible_to_the_kernel(self):
        harness = for_policy(self.Policy())
        self.assertIsInstance(harness, PolicyHarness)
        self.assertEqual("test_policy", harness.name)
        self.assertEqual("test-model", harness.model)

    def test_replaced_proposal_method_is_observed_after_construction(self):
        """控制器可能在构造后替换提案方法；适配器不能缓存首次取到的方法。"""
        policy = self.Policy()
        harness = for_policy(policy)
        with patch.object(policy, "propose_revision",
                          side_effect=AssertionError("No new revision")):
            with self.assertRaisesRegex(AssertionError, "No new revision"):
                harness.propose_revision("q", {}, {}, [])


class RevisionProposalParsingTests(unittest.TestCase):
    def test_accepts_replacement_and_rejects_source_alias(self):
        valid = {"edits": [{"path": "helper.py", "original_sha256": None,
                            "replacement": "x = 1\n"}], "rationale": "helper"}
        parsed = parse_revision_proposal(valid)
        self.assertEqual("helper.py", parsed["edits"][0].path)
        self.assertEqual("helper", parsed["rationale"])
        for key in ("source", "content"):
            with self.subTest(key=key):
                row = {"path": "helper.py", "original_sha256": None, key: "x = 1\n"}
                with self.assertRaisesRegex(ProtocolError, "字段不正确"):
                    parse_revision_proposal({"edits": [row], "rationale": "helper"})

    def test_empty_edits_are_a_valid_registered_implementation_proposal(self):
        self.assertEqual((), parse_revision_proposal(
            {"edits": [], "rationale": "already implemented"})["edits"])

    def test_rejects_out_of_scope_paths_and_missing_fields(self):
        cases = (
            ({"path": "../escape.py", "original_sha256": None, "replacement": "x = 1\n"},
             "相对 Python 路径"),
            # 两种宿主各自的「绝对路径」写法都必须被拒：只按当前平台判会一侧放行
            # （见 tests/test_path_portability.py）。
            ({"path": "C:/abs.py", "original_sha256": None, "replacement": "x = 1\n"},
             "相对 Python 路径"),
            ({"path": "/tmp/abs.py", "original_sha256": None, "replacement": "x = 1\n"},
             "相对 Python 路径"),
            ({"path": "notes.txt", "original_sha256": None, "replacement": "x = 1\n"},
             "相对 Python 路径"),
            ({"path": "helper.py", "original_sha256": None, "replacement": "def f(:\n"},
             "语法错误"),
        )
        for row, message in cases:
            with self.subTest(row=row):
                with self.assertRaisesRegex(ProtocolError, message):
                    parse_revision_proposal({"edits": [row], "rationale": "r"})

    def test_rejects_wrong_envelope(self):
        for response in ([], {"edits": [], "rationale": "   "}, {"edits": []},
                         {"edits": [{}], "rationale": "r"}):
            with self.subTest(response=response):
                with self.assertRaisesRegex(ProtocolError, "JSON 对象|edits|rationale|字段不正确"):
                    parse_revision_proposal(response)


class JSONAgentHarnessTests(unittest.TestCase):
    def _harness(self, *responses):
        call = Mock(side_effect=list(responses))
        return JSONAgentHarness(call, model="test-model"), call

    def test_valid_response_is_returned_without_a_second_call(self):
        harness, call = self._harness(
            {"edits": [], "rationale": "registered implementation already matches"})
        result = harness.propose_revision("q", {"hypothesis_id": "H1"}, {"x": 1}, [])
        self.assertEqual((), result["edits"])
        self.assertEqual(1, call.call_count)

    def test_one_bounded_correction_then_success(self):
        harness, call = self._harness(
            {"edits": None, "rationale": "bad"},
            {"edits": [{"path": "helper.py", "original_sha256": None,
                        "replacement": "x = 1\n"}], "rationale": "ok"})
        result = harness.propose_revision("q", {"hypothesis_id": "H1"}, {"x": 1}, [])
        self.assertEqual(2, call.call_count)
        self.assertEqual("helper.py", result["edits"][0].path)
        correction = call.call_args_list[1].args[1]
        self.assertIn("validation_error", correction)
        self.assertIn("untrusted data", correction["correction_request"])

    def test_repeated_invalid_response_stops_after_two_calls(self):
        harness, call = self._harness({"edits": None, "rationale": "bad"},
                                      {"edits": None, "rationale": "still bad"})
        with self.assertRaisesRegex(ProtocolError, "edits"):
            harness.propose_revision("q", {"hypothesis_id": "H1"}, {"x": 1}, [])
        self.assertEqual(2, call.call_count)

    def test_from_endpoint_requires_https_endpoint(self):
        with self.assertRaises(ProtocolError):
            JSONAgentHarness.from_endpoint("http://example.com", "m")


class KernelKeepsProposalAuthorityTests(unittest.TestCase):
    """内核在没有提案者时必须在入口就拒绝，而不是让 job 在沙箱里失败。"""

    def _controller(self, harness):
        controller = object.__new__(ResearchController)
        controller.harness = harness
        controller.policy = EvidenceDrivenPolicy()
        return controller

    def test_implement_without_harness_points_to_the_option(self):
        controller = self._controller(None)
        controller._verify_inputs = Mock(return_value={})
        controller._require_scoring_binding = Mock()
        controller._external_is_frozen = Mock(return_value=False)
        with self.assertRaisesRegex(ProtocolError, "--harness"):
            controller.implement("H1")

    def test_autonomous_code_without_harness_is_rejected(self):
        controller = self._controller(None)
        with patch("popper.research.controller.sandbox.available", return_value=True):
            with self.assertRaisesRegex(ProtocolError, "--harness"):
                controller.run(trusted_local=False, sandboxed=True, autonomous_code=True)

    def test_research_policy_delegates_proposals_to_the_structured_harness(self):
        """策略不再自己解析提案：提案形状只在 harness 里定义一次。"""
        client = Mock(return_value={"edits": [], "rationale": "registered implementation"})
        with patch("popper.research.models.make_json_client", return_value=client):
            policy = DeepSeekResearchPolicy("https://api.example.com", "test-model")
            result = policy.propose_revision("q", {"hypothesis_id": "H1"}, {"x": 1}, [])
        self.assertEqual((), result["edits"])
        self.assertEqual(1, client.call_count)


if __name__ == "__main__":
    unittest.main()
