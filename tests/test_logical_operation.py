"""Real local HTTP fault tests for one shared logical retry boundary."""
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from popper.core import ProtocolError
from popper.model_budget import ModelRequestBudget
from popper.request_lifecycle import RequestFailure, RequestLimits
from popper.scoop import make_json_client
from popper.transport.logical_operation import LogicalOperationManager


class ScriptedHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.bodies.append(body)
        index = len(self.server.bodies) - 1
        item = self.server.script[min(index, len(self.server.script) - 1)]
        try:
            if isinstance(item, int):
                self.send_response(item)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            frames = [
                {"choices": [{"delta": {"content": item}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"total_tokens": 11}},
            ]
            for frame in frames:
                self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


@pytest.fixture
def server():
    instance = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
    instance.daemon_threads = True
    instance.bodies = []
    instance.script = ['{"ok":true}']
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance
    instance.shutdown()
    instance.server_close()


def client(server, **kwargs):
    return make_json_client(f"http://127.0.0.1:{server.server_port}/v1", "local-test",
                            api_key="synthetic-placeholder", **kwargs)


@pytest.mark.parametrize("script", [["{", 503], [503, "{"]])
def test_invalid_json_and_network_retries_share_two_dispatch_cap(server, script):
    server.script = script
    call = client(server)
    with patch("popper.request_lifecycle.cancellable_wait", return_value=None):
        with call.logical_operation("mixed-failure") as operation:
            with pytest.raises(RequestFailure):
                call("Return JSON", {})
            assert operation.attempts == 2
            with pytest.raises(RequestFailure) as exhausted:
                call("Correct the prior JSON", {"correction": True})
            assert exhausted.value.category == "attempt_budget_exhausted"
    assert len(server.bodies) == 2
    assert all(body["stream"] for body in server.bodies)


def test_caller_correction_and_internal_retry_cannot_multiply_attempts(server):
    server.script = ['{"semantically_invalid":true}', 503, '{"ok":true}']
    call = client(server)
    with call.logical_operation("semantic-correction") as operation:
        assert call("Return JSON", {})["semantically_invalid"]
        with pytest.raises(RequestFailure) as error:
            call("Correct field semantics", {"validation_error": "wrong shape"})
        assert error.value.category == "temporary_service"
        assert operation.attempts == 2
        with pytest.raises(RequestFailure):
            call("Correct once more", {})
    assert len(server.bodies) == 2


def test_correction_uses_same_identity_deadline_and_exactly_once_usage_settlement(server):
    server.script = ['{"semantically_invalid":true}', '{"ok":true}']
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        budget = ModelRequestBudget(root / "calls.db", global_tokens=200000,
            global_calls=4, scope="cell", scope_tokens=200000, scope_calls=4)
        try:
            call = client(server, request_budget=budget, diagnostics_dir=root / "diagnostics")
            with call.logical_operation("stable-id") as operation:
                assert call("JSON", {})["semantically_invalid"]
                started, deadline = operation.started, operation.deadline
                assert call("JSON correction", {"rejected": "shape"})["ok"]
                assert operation.started == started
                assert operation.deadline == deadline
            records = budget.snapshot()["attempts"]
            assert [row["logical_request_id"] for row in records] == ["stable-id", "stable-id"]
            assert [row["attempt_index"] for row in records] == [1, 2]
            assert [row["status"] for row in records] == ["settled", "settled"]
            assert budget.tokens_used == 22
            diagnostics = [json.loads(path.read_text(encoding="utf-8"))
                           for path in (root / "diagnostics").glob("*.json")]
            assert len(diagnostics) == 2
            assert {row["logical_started_monotonic"] for row in diagnostics} == {started}
            assert {row["logical_deadline_monotonic"] for row in diagnostics} == {deadline}
        finally:
            budget.close()


def test_mixed_failures_keep_known_usage_and_unknown_reservation_under_same_identity(server):
    server.script = ["{", 503]
    with tempfile.TemporaryDirectory() as tmp:
        budget = ModelRequestBudget(Path(tmp) / "calls.db", global_tokens=200000,
            global_calls=4, scope="cell", scope_tokens=200000, scope_calls=4,
            unknown_usage_policy="reserve")
        try:
            call = client(server, request_budget=budget)
            with patch("popper.request_lifecycle.cancellable_wait", return_value=None):
                with call.logical_operation("settled-and-unknown"):
                    with pytest.raises(RequestFailure):
                        call("JSON", {})
                    with pytest.raises(RequestFailure):
                        call("JSON correction", {})
            rows = budget.snapshot()["attempts"]
            assert len(rows) == 2
            assert [row["logical_request_id"] for row in rows] == ["settled-and-unknown"] * 2
            assert [row["attempt_index"] for row in rows] == [1, 2]
            assert rows[0]["used_tokens"] == 11
            assert rows[0]["status"] == "settled"
            assert rows[1]["status"] == "unknown"
            assert rows[1]["used_tokens"] is None
            assert rows[1]["reserved_tokens"] > 0
            assert budget.tokens_used > 11
        finally:
            budget.close()
    assert len(server.bodies) == 2


def test_time_between_correction_calls_counts_toward_original_total_deadline(server):
    call = client(server, request_limits=RequestLimits(connect=.5, idle=.5, total=.1))
    with call.logical_operation("deadline") as operation:
        assert call("JSON", {})["ok"]
        time.sleep(.12)
        with pytest.raises(RequestFailure) as error:
            call("JSON correction", {})
        assert error.value.category == "total_timeout"
        assert error.value.sent is False
        assert operation.attempts == 1
    assert len(server.bodies) == 1


def test_context_cancellation_prevents_correction_without_another_send(server):
    event = threading.Event()
    call = client(server)
    with call.logical_operation("cancel-between", cancel_event=event) as operation:
        assert call("JSON", {})["ok"]
        event.set()
        start = time.monotonic()
        with pytest.raises(RequestFailure) as error:
            call("JSON correction", {})
        assert error.value.category == "cancelled"
        assert time.monotonic() - start < .5
        assert operation.attempts == 1
    assert len(server.bodies) == 1


def test_call_cancellation_signal_stays_bound_to_operation(server):
    event = threading.Event()
    call = client(server)
    with call.logical_operation("call-cancel"):
        assert call("JSON", {}, cancel_event=event)["ok"]
        event.set()
        with pytest.raises(RequestFailure) as error:
            call("JSON correction", {})
        assert error.value.category == "cancelled"
    assert len(server.bodies) == 1


def test_cancellation_during_shared_retry_wait_stops_first_attempt(server):
    server.script = [503, '{"ok":true}']
    call = client(server)
    event = threading.Event()
    timer = threading.Timer(.1, event.set)
    timer.start()
    try:
        with call.logical_operation("retry-cancel", cancel_event=event) as operation:
            start = time.monotonic()
            with pytest.raises(RequestFailure) as error:
                call("JSON", {})
            assert error.value.category == "cancelled"
            assert time.monotonic() - start < 1
            assert operation.attempts == 1
    finally:
        timer.cancel()
    assert len(server.bodies) == 1


def test_authentication_failure_cannot_be_retried_by_outer_caller(server):
    server.script = [401, '{"ok":true}']
    call = client(server)
    with call.logical_operation("auth"):
        for _ in range(2):
            with pytest.raises(RequestFailure) as error:
                call("JSON", {})
            assert error.value.category == "authentication_or_permission"
    assert len(server.bodies) == 1


def test_existing_noncontext_interface_still_has_two_attempts_per_call(server):
    server.script = ["{", '{"ok":true}']
    call = client(server)
    with patch("popper.request_lifecycle.cancellable_wait", return_value=None):
        assert call("JSON", {})["ok"]
    assert len(server.bodies) == 2
    assert call("JSON", {})["ok"]
    assert len(server.bodies) == 3


def test_changing_identity_or_nesting_cannot_reset_attempt_budget(server):
    call = client(server)
    with call.logical_operation("stable") as operation:
        with pytest.raises(ProtocolError, match="ID"):
            call("JSON", {}, logical_request_id="other")
        with pytest.raises(ProtocolError, match="嵌套"):
            with call.logical_operation("new"):
                pass
        assert operation.attempts == 0
    assert not server.bodies
    with call.logical_operation("subsequent"):
        assert call("JSON", {}, logical_request_id="subsequent")["ok"]


@pytest.mark.parametrize("max_attempts", [0, 3, True, "2"])
def test_invalid_attempt_configuration_cannot_expand_hard_limit(max_attempts):
    manager = LogicalOperationManager()
    with pytest.raises(ProtocolError):
        with manager.operation(max_attempts=max_attempts):
            pass


def invoke_legacy(method, call):
    """Exercise actual old adapters, not a reconstructed correction loop."""
    from popper.harness.json_agent import JSONAgentHarness
    from popper.research.models import DeepSeekResearchPolicy
    from popper.scoop import _verify_fulltext
    policy = object.__new__(DeepSeekResearchPolicy)
    policy._call = call
    policy.model = "local-test"
    policy.name = "test-policy"
    if method == "generate_hypotheses":
        return policy.generate_hypotheses("Compare candidates", [{"kind": "cart"}])
    if method == "choose":
        return policy.choose({"study": {"question": "Compare candidates"},
            "metric": {"name": "mae", "direction": "min"}, "min_meaningful_effect": .05,
            "budget": {"available": 1}, "observations": [],
            "candidates": [{"hypothesis_id": "H-1", "status": "untested", "config": {}}]})
    if method == "reflect":
        return policy.reflect({"effect": 0, "threshold": .05, "budget_available": 0,
            "remaining_candidates": [], "boundary_detected": False,
            "allowed_actions": ["stop"], "evidence_refs": ["O-base", "O-candidate"]})
    if method == "propose_revision":
        return JSONAgentHarness(call).propose_revision("Compare candidates", {}, {}, [])
    return _verify_fulltext(call, {}, {"paper_id": "P-1"},
                            "This exact passage is independently verifiable.")


LEGACY_METHODS = ["generate_hypotheses", "choose", "reflect", "propose_revision", "fulltext"]


@pytest.mark.parametrize("method", LEGACY_METHODS)
def test_old_adapters_share_inner_json_repair_and_outer_semantic_correction_cap(server, method):
    server.script = ["{", "{}", '{"unexpected_third_send":true}']
    call = client(server)
    with patch("popper.request_lifecycle.cancellable_wait", return_value=None):
        with pytest.raises(RequestFailure) as exhausted:
            invoke_legacy(method, call)
    assert exhausted.value.category == "attempt_budget_exhausted"
    assert len(server.bodies) == 2


@pytest.mark.parametrize("method,valid", [
    ("generate_hypotheses", {"hypotheses": [{"candidate_index": 0, "mechanism": "m",
        "applicability": "scope", "predictions": ["p"], "falsification": ["f"], "alternatives": ["a"]}]}),
    ("choose", {"kind": "run_experiment", "hypothesis_id": "H-1", "rationale": "Compare", "alternatives": []}),
    ("reflect", {"action": "stop", "rationale": "No more budget", "alternative_explanation": "Insufficient signal",
        "next_hypothesis_id": None, "revision": None, "evidence_refs": ["O-base", "O-candidate"]}),
    ("propose_revision", {"edits": [], "rationale": "Already implemented"}),
    ("fulltext", {"paper_id": "P-1", "axis_matches": {axis: "match" for axis in
        ("problem_framing", "core_mechanism", "key_insight", "application_domain")},
        "closest_passage": "This exact passage is independently verifiable."}),
])
def test_old_adapters_still_correct_semantics_once_under_stable_ledger_identity(server, method, valid):
    server.script = ["{}", json.dumps(valid)]
    with tempfile.TemporaryDirectory() as tmp:
        budget = ModelRequestBudget(Path(tmp) / "calls.db", global_tokens=200000,
            global_calls=4, scope="cell", scope_tokens=200000, scope_calls=4)
        try:
            result = invoke_legacy(method, client(server, request_budget=budget))
            assert result
            rows = budget.snapshot()["attempts"]
            assert len(rows) == 2
            assert rows[0]["logical_request_id"] == rows[1]["logical_request_id"]
            assert [row["attempt_index"] for row in rows] == [1, 2]
            assert budget.tokens_used == 22
        finally:
            budget.close()
    assert len(server.bodies) == 2


def invoke_remaining_adapter(method, call):
    from popper.research.question_proposer import mechanism_after_kill, solicit_card_problem
    if method == "card_problem":
        card = {"target_column": "label", "columns": [{"name": "label"}, {"name": "feature1"},
            {"name": "feature2"}], "label_stats": {"scope": "train_dev",
            "distribution": [{"value": 0, "rate": .5}, {"value": 1, "rate": .5}],
            "associations": [{"feature": "feature1", "value": .3}, {"feature": "feature2", "value": .1}]}}
        return solicit_card_problem(call, card, "Compare candidates", "ab" * 32)
    if method == "next_mechanism":
        return mechanism_after_kill(call, second_feature="feature2", target="label",
            killed_id="old", surviving_id="other", delta=0, threshold=.05,
            explanations=[{"statement": "old mechanism"}, {"statement": "other mechanism"}],
            parent_question="previous question", parent_holdout_sha256="ab" * 32)
    from popper.vendors import VendorRegistry
    registry = object.__new__(VendorRegistry)
    with tempfile.TemporaryDirectory() as tmp, patch("popper.scoop.make_json_client", return_value=call):
        return registry._execute_llm_subagent(Path(tmp), {"type": "llm_subagent"},
            "https://unused.example/v1", "local-test", "Compare candidates",
            {"metric": {"name": "accuracy", "direction": "max"}})


@pytest.mark.parametrize("method", ["card_problem", "next_mechanism", "vendor_candidate"])
def test_remaining_adapters_cannot_multiply_inner_and_outer_attempts(server, method):
    server.script = ["{", "{}", '{"unexpected_third_send":true}']
    with patch("popper.request_lifecycle.cancellable_wait", return_value=None):
        if method == "next_mechanism":
            text, author, error = invoke_remaining_adapter(method, client(server))
            assert text
            assert author == "kernel_after_model_rejection"
            assert "attempt_budget_exhausted" in error
        else:
            with pytest.raises(ProtocolError):
                invoke_remaining_adapter(method, client(server))
    assert len(server.bodies) == 2


@pytest.mark.parametrize("method,valid", [
    ("card_problem", {"question": "Can feature1 predict label?", "explanations": [
        {"id": "signal", "statement": "Measured feature1 signal", "contrast": {"direction": "above_threshold"}},
        {"id": "prior", "statement": "Class prior explains the score", "contrast": {"direction": "not_above_threshold"}}],
        "execution": {"baseline": {"kind": "constant"}, "candidates": [{"kind": "linear"}], "seeds": [17], "budget": 4}}),
    ("next_mechanism", {"revised_mechanism": "feature2 improves label by 0.05 over the constant baseline"}),
    ("vendor_candidate", {"title": "Candidate", "core_mechanism": "Feature signal",
        "falsification_prediction": "Accuracy improves by 0.05",
        "evaluation_contract": {"metric": {"name": "accuracy", "direction": "max"}}}),
])
def test_remaining_adapters_keep_successful_one_correction_and_shared_identity(server, method, valid):
    server.script = ["{}", json.dumps(valid)]
    with tempfile.TemporaryDirectory() as tmp:
        budget = ModelRequestBudget(Path(tmp) / "calls.db", global_tokens=200000,
            global_calls=4, scope="cell", scope_tokens=200000, scope_calls=4)
        try:
            result = invoke_remaining_adapter(method, client(server, request_budget=budget))
            assert result
            if method == "next_mechanism":
                assert result[1] == "model"
            if method == "vendor_candidate":
                assert result["process"] == "automated"
            rows = budget.snapshot()["attempts"]
            assert len(rows) == 2
            assert rows[0]["logical_request_id"] == rows[1]["logical_request_id"]
            assert [row["attempt_index"] for row in rows] == [1, 2]
            assert budget.tokens_used == 22
        finally:
            budget.close()
    assert len(server.bodies) == 2
