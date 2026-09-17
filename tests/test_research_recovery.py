import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from popper.core import ProtocolError
from popper.scoop import AXES, ScoopRun, make_json_client
from popper.candidate_contract import validate_candidate
from popper.vendor_worker import run_source_workers


def response(content, finish="stop"):
    return io.BytesIO(json.dumps({"choices": [{"finish_reason": finish,
        "message": {"content": content}}], "usage": {"total_tokens": 9}}).encode())


class JsonRecoveryTests(unittest.TestCase):
    def test_malformed_response_recovers_and_redacts_diagnostics(self):
        with tempfile.TemporaryDirectory() as d, patch.dict("os.environ", {"POPPER_API_KEY": "sk-test-secret"}):
            opener = Mock()
            opener.open.side_effect = [response('{"secret":"sk-test-secret",'), response('{"ok":true}')]
            with patch("popper.scoop.urllib.request.build_opener", return_value=opener):
                self.assertEqual({"ok": True}, make_json_client("https://example.org", "m", d)("JSON", {}))
            self.assertEqual(2, opener.open.call_count)
            records = [json.loads(f.read_text()) for f in Path(d).glob('*.json')]
            self.assertEqual({"success", "failed"}, {r["status"] for r in records})
            self.assertNotIn("sk-test-secret", json.dumps(records))
            self.assertTrue(all("finish_reason" in r for r in records))

    def test_length_is_retried_even_when_content_parses(self):
        with patch.dict("os.environ", {"POPPER_API_KEY": "test"}):
            opener = Mock()
            opener.open.side_effect = [response('{}', "length"), response('{"complete":true}')]
            with patch("popper.scoop.urllib.request.build_opener", return_value=opener):
                self.assertEqual({"complete": True}, make_json_client("https://example.org", "m")("JSON", {}))
            budgets = [json.loads(c.args[0].data)["max_tokens"] for c in opener.open.call_args_list]
            self.assertEqual([8000, 16000], budgets)

    def test_transient_network_error_has_one_recovery_attempt(self):
        with patch.dict("os.environ", {"POPPER_API_KEY": "test"}):
            opener = Mock()
            opener.open.side_effect = [urllib.error.URLError(TimeoutError()), response('{"ok":true}')]
            with patch("popper.scoop.urllib.request.build_opener", return_value=opener):
                self.assertEqual({"ok": True}, make_json_client("https://example.org", "m")("JSON", {}))
            self.assertEqual(2, opener.open.call_count)

    def test_exhaustion_and_auth_errors_are_bounded(self):
        with patch.dict("os.environ", {"POPPER_API_KEY": "test"}):
            for failures, count in [([response('['), response('[')], 2),
                    ([urllib.error.HTTPError("https://example.org", 401, "unauthorized", {}, None)], 1)]:
                opener = Mock()
                opener.open.side_effect = failures
                with patch("popper.scoop.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(ProtocolError):
                        make_json_client("https://example.org", "m")("JSON", {})
                self.assertEqual(count, opener.open.call_count)


class CandidateContractTests(unittest.TestCase):
    def test_other_metric_mentions_are_allowed_but_statistical_claims_are_not(self):
        contract = {"metric": {"name": "accuracy", "direction": "max"}}
        candidate = {"title": "t", "core_mechanism": "m", "evaluation_contract": contract,
                     "falsification_prediction": "Accuracy improves by 0.02."}
        validate_candidate(candidate, contract)
        # 指标名不再被反向门禁枚举：提到其他指标（如 ROC AUC）不再被误拒。
        validate_candidate({**candidate, "falsification_prediction": "ROC AUC improves by 0.01."},
                           contract)
        with self.assertRaises(ProtocolError):
            validate_candidate({**candidate, "falsification_prediction": "Accuracy improves (p < 0.05)."},
                               contract)
        with self.assertRaises(ProtocolError):
            validate_candidate({**candidate, "evaluation_contract": {}}, contract)

    def test_generator_repairs_contract_before_saving_candidate(self):
        from popper.vendors import VendorRegistry
        contract = {"metric": {"name": "accuracy", "direction": "max"}}
        valid = {"title": "t", "core_mechanism": "m", "evaluation_contract": contract,
                 "falsification_prediction": "Accuracy improves by 0.02."}
        invalid = {**valid, "falsification_prediction": "Accuracy improves (p = 0.01)."}
        client = Mock(side_effect=[invalid, valid])
        with tempfile.TemporaryDirectory() as d, patch("popper.scoop.make_json_client", return_value=client):
            result = VendorRegistry()._execute_llm_subagent(
                Path(d), {"type": "llm_subagent"}, "https://example.org", "m", "question", contract)
            self.assertEqual("automated", result["process"])
            saved = json.loads(Path(result["candidate"]).read_text())
            self.assertEqual(valid, saved)
            self.assertEqual(2, client.call_count)
            self.assertIn("validation_error", client.call_args.args[1])
            self.assertEqual(contract, client.call_args.args[1]["evaluation_contract"])

    def test_generator_never_saves_exhausted_invalid_candidate(self):
        from popper.vendors import VendorRegistry
        client = Mock(return_value={"title": "invalid"})
        with tempfile.TemporaryDirectory() as d, patch("popper.scoop.make_json_client", return_value=client):
            with self.assertRaises(ProtocolError):
                VendorRegistry()._execute_llm_subagent(
                    Path(d), {"type": "llm_subagent"}, "https://example.org", "m", "q", {})
            self.assertFalse((Path(d) / 'phase3_revise' / 'final_candidate.json').exists())
            self.assertEqual(2, client.call_count)


class SearchDeadlineTests(unittest.TestCase):
    def test_search_subprocess_receives_bounded_http_policy(self):
        from popper.vendors import VendorRegistry
        registry = VendorRegistry()
        completed = Mock(returncode=0, stdout=json.dumps({"papers": [], "source_counts": {}}), stderr="")
        with tempfile.TemporaryDirectory(dir=registry.project_root / 'integrations' / 'runs') as d:
            with patch("popper.vendors.subprocess.run", return_value=completed) as run:
                registry.paper_search(d, ["q"], 2024, 2026, trusted_local=True)
            env = run.call_args.kwargs["env"]
            self.assertEqual("1", env["PAPER_SEARCH_MAX_ATTEMPTS"])
            self.assertEqual("15", env["PAPER_SEARCH_TIMEOUT_SECONDS"])
            self.assertEqual("8", env["PAPER_SEARCH_CONNECT_TIMEOUT_SECONDS"])

    def test_paper_search_requires_explicit_trusted_local(self):
        from popper.vendors import VendorRegistry
        registry = VendorRegistry()
        with tempfile.TemporaryDirectory(dir=registry.project_root / 'integrations' / 'runs') as d:
            with patch("popper.vendors.subprocess.run") as run:
                with self.assertRaisesRegex(ProtocolError, "trusted-local"):
                    registry.paper_search(d, ["q"], 2024, 2026)
            run.assert_not_called()

    def test_slow_source_is_stopped_and_other_source_is_retained(self):
        vendor = Mock()
        slow, fast = Mock(), Mock()
        slow.wait.side_effect = subprocess.TimeoutExpired("source", 1)
        vendor._start_worker.side_effect = [(slow, "out1", "log1"), (fast, "out2", "log2")]
        vendor._collect_worker.side_effect = [[], [{"title": "retained"}]]
        results = run_source_workers(vendor, ["slow", "fast"], ["q"], 2024, 2026, 5,
                                     True, timeout_seconds=0.1)
        self.assertEqual({"slow": [], "fast": [{"title": "retained"}]}, results)
        vendor._terminate_workers.assert_any_call([slow])

    def test_forwarding_layer_does_not_replace_vendor_functions(self):
        vendor = Mock()
        process = Mock()
        vendor._start_worker.return_value = (process, "out", "log")
        vendor._collect_worker.return_value = [{"title": "kept"}]
        started = vendor._start_worker
        collected = vendor._collect_worker
        results = run_source_workers(vendor, ["arxiv"], ["q"], 2024, 2026, 5, True,
                                     timeout_seconds=5)
        self.assertEqual({"arxiv": [{"title": "kept"}]}, results)
        self.assertIs(started, vendor._start_worker)
        self.assertIs(collected, vendor._collect_worker)
        process.wait.assert_called_once()


class TriageResumeTests(unittest.TestCase):
    def test_completed_batches_survive_later_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            idea = root / 'idea'
            (idea / 'phase3_revise').mkdir(parents=True)
            (idea / 'phase3_revise' / 'final_candidate.json').write_text(json.dumps({
                "title": "t", "core_mechanism": "m", "falsification_prediction": "accuracy"}))
            papers = [{"title": str(i), "doi": str(i)} for i in range(12)]
            batch_calls = []
            fail_once = [True]
            def llm(system, payload):
                if system.startswith("Decompose"):
                    return {"axes": {a: a for a in AXES}, "queries": ["a", "b", "c"]}
                if system.startswith("Triage"):
                    ids = tuple(p["paper_id"] for p in payload["papers"])
                    batch_calls.append(ids)
                    self.assertLessEqual(len(ids), 5)
                    if len(batch_calls) == 2 and fail_once[0]:
                        fail_once[0] = False
                        raise ProtocolError("controlled batch failure")
                    return {"papers": [{"paper_id": pid, "overlap_score": 0,
                                       **{a: "unrelated" for a in AXES}} for pid in ids]}
                if system.startswith("Compare"):
                    return {"comparisons": [{"paper_id": p["paper_id"], "axis_matches": {a: "differ" for a in AXES}}
                                            for p in payload["deep_dive"]],
                            "closest_paper_id": payload["deep_dive"][0]["paper_id"] if payload["deep_dive"] else None}
                return {"delta": "No verified closest paper"}
            def inaccessible(*args):
                raise OSError("offline fixture")
            run = ScoopRun(idea, root / 'scoop', lambda *args: {"papers": papers}, llm, inaccessible)
            with self.assertRaises(ProtocolError):
                run.run(2024, 2026)
            result = run.run(2024, 2026)
            self.assertEqual("provisional", result["phase"])
            self.assertEqual(1, batch_calls.count(batch_calls[0]))
            self.assertEqual(4, len(batch_calls))
            saved = json.loads((root / 'scoop' / 'step3.json').read_text())
            self.assertEqual(12, len(saved["papers"]))


if __name__ == '__main__':
    unittest.main()
