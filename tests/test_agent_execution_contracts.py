from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from popper.core import ProtocolError, file_hash
from popper.research.agent_contracts import execution_contracts
from popper.research import agent_contracts
from popper.sessions import AgentSessionStore
from popper.server import Workstation
from popper import task_context


@pytest.fixture
def bound_run(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    manifest = {"project": str(project), "study_id": "S-0000000000000001", "scorer_id": "oscillator-trajectory-v1"}
    path = run / "research.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    module_dir = Path(agent_contracts.__file__).parent
    binding = {"schema_version": "1.0", "kind": "windows_metered_rhs_v1", "research_sha256": file_hash(path),
               "adapter_sha256": file_hash(module_dir / "oscillator_worker.py"),
               "execution_sha256": file_hash(module_dir / "oscillator_execution.py"),
               "implementation_files": {str(module_dir / name): file_hash(module_dir / name)
                                        for name in ("oscillator_driver.py", "oscillator_client.py", "oscillator_broker.py")},
               "protected_files": {"DO_NOT_READ_PRIVATE_REFERENCE_FILE": "private-hash"}, "api_key": "synthetic-private-key"}
    (run / "oscillator-worker-binding.json").write_text(json.dumps(binding), encoding="utf-8")
    return project, run, binding


def test_public_contract_scoped_deduplicated_and_does_not_export_private_binding(bound_run):
    project, run, _ = bound_run
    data = execution_contracts(project, [run, run])
    assert len(data) == 1
    assert "solve(rhs, y0, times, budget, config)" in data[0]["entrypoint"]
    assert "including stages and implicit iterations" in data[0]["rhs"]["counting"]
    encoded = json.dumps(data)
    assert "DO_NOT_READ" not in encoded and "synthetic-private-key" not in encoded
    assert execution_contracts(project.parent / "other-project", [run]) == []
    assert execution_contracts(project, [run], {"S-0000000000000002"}) == []


@pytest.mark.parametrize("field", ["research_sha256", "adapter_sha256", "execution_sha256"])
def test_changed_binding_refuses_context(bound_run, field):
    project, run, binding = bound_run
    binding[field] = "0" * 64
    (run / "oscillator-worker-binding.json").write_text(json.dumps(binding), encoding="utf-8")
    with pytest.raises(ProtocolError):
        execution_contracts(project, [run])


def test_interface_code_digest_mismatch_refuses_context(bound_run):
    project, run, binding = bound_run
    key = next(k for k in binding["implementation_files"] if k.endswith("oscillator_driver.py"))
    binding["implementation_files"][key] = "0" * 64
    (run / "oscillator-worker-binding.json").write_text(json.dumps(binding), encoding="utf-8")
    with pytest.raises(ProtocolError):
        execution_contracts(project, [run])


def test_unknown_worker_profile_is_not_guessed(bound_run):
    project, run, binding = bound_run
    binding["kind"] = "unregistered_worker"
    (run / "oscillator-worker-binding.json").write_text(json.dumps(binding), encoding="utf-8")
    assert execution_contracts(project, [run]) == []


def test_server_context_pins_contract_without_task_link_and_persists_same_snapshot(bound_run):
    project, run, _ = bound_run
    store = AgentSessionStore(project)
    try:
        session_id = store.create_session("synthetic", "chat", "mock")["session_id"]
        for i in range(6):
            turn, _ = store.start_turn(session_id, "old" * 3000, f"history-{i}", actor="local_user", trace_id="trc_history123")
            store.complete_turn(turn["turn_id"], "old response" * 1000)
        service = SimpleNamespace(chat_harness=SimpleNamespace(context_tools=[]),
                                  _visible_research_runs=lambda _: [run])
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"POPPER_CONTEXT_MAX_BYTES": "4096"}):
            snapshot = Workstation._assemble_session_context(service, store, session_id, "system", "continue")
        assert snapshot["omitted_groups"] > 0
        assert "solve(rhs, y0, times, budget, config)" in snapshot["messages"][0]["content"]
        assert "including stages and implicit iterations" in snapshot["messages"][0]["content"]
        reserve = Workstation._reserve_context(snapshot)
        turn, _ = store.start_turn(session_id, "continue", "pinned-input", actor="local_user", trace_id="trc_pinned123",
                                   token_reservation=reserve, model_input=snapshot)
        assert task_context.load_input(store, turn["turn_id"]) == snapshot
        assert Workstation._reserve_context(task_context.load_input(store, turn["turn_id"])) == reserve
    finally:
        store.close()
