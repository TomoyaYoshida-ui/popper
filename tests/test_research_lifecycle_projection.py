from pathlib import Path
from unittest.mock import patch

import pytest

from popper.core import file_hash
from popper.research.controller import ResearchController, research_lifecycle_phase
from popper.research.readout import read_research_state
from tests.test_study_lifecycle import make_project


@pytest.mark.parametrize("study,core,actions,frozen,submitted,expected", [
    ("active", "searching", [], False, False, "researching"),
    ("active", "searching", ["request_confirmation"], False, False, "ready_for_confirmation"),
    ("active", "frozen", ["request_scope_boundary_confirmation"], False, False, "ready_for_confirmation"),
    ("active", "completed", [], False, False, "ready_for_confirmation"),
    ("active", "confirming", ["request_confirmation"], False, False, "confirming"),
    ("active", "confirmation_failed", ["request_confirmation"], False, False, "confirmation_failed"),
    ("active", "searching", [], True, False, "external_preparation_incomplete"),
    ("active", "searching", [], True, True, "external_confirmation_pending"),
    ("concluded", "completed", [], True, True, "concluded"),
    ("budget_exhausted", "searching", [], False, False, "budget_exhausted"),
    ("active", None, ["request_confirmation"], False, False, "unavailable"),
])
def test_controller_lifecycle_ordering(study, core, actions, frozen, submitted, expected):
    assert research_lifecycle_phase(study, core, [{"action": value} for value in actions],
        external_frozen=frozen, external_submitted=submitted) == expected


def fingerprints(*roots):
    return {str(file): (file_hash(file), file.stat().st_mtime_ns) for root in roots
            for file in Path(root).rglob("*") if file.is_file()}


def test_lifecycle_projection_is_readonly_and_matches_controller(tmp_path):
    project, run_dir = make_project(tmp_path, "ode-convergence")
    controller = ResearchController(run_dir)
    try:
        phase = controller.status()["phase"]
    finally:
        controller.close()
    before = fingerprints(project, run_dir)
    with patch.object(ResearchController, "__init__", side_effect=AssertionError("reader opened Controller")):
        projected = read_research_state(run_dir)
    assert projected["ok"]
    assert projected["lifecycle_phase"] == phase == "researching"
    assert projected["confirmation_mode"] == "local"
    assert projected["confirmation_resource_consumed"] is False
    assert fingerprints(project, run_dir) == before


def test_unreadable_core_does_not_invent_confirmation_eligibility(tmp_path):
    project, run_dir = make_project(tmp_path, "ode-convergence")
    core_path = project / ".popper" / "state.db"
    preserved = core_path.with_name("state.db.acceptance-preserved")
    core_path.rename(preserved)
    before = fingerprints(project, run_dir)
    result = read_research_state(run_dir)
    assert result["ok"]
    assert result["lifecycle_phase"] == "unavailable"
    assert "不会推测" in result["lifecycle_reason"]
    assert "confirmation_resource_consumed" not in result
    assert not core_path.exists()
    assert fingerprints(project, run_dir) == before


def test_failed_confirmation_is_reported_without_recovery_or_retry(tmp_path):
    import json
    import sqlite3

    project, run_dir = make_project(tmp_path, "ode-convergence")
    # A synthetic failure snapshot exercises presentation without consuming a
    # real confirmation or starting any worker/model.
    with sqlite3.connect(project / ".popper" / "state.db") as core:
        snapshot = json.loads(core.execute("SELECT data FROM state WHERE id=1").fetchone()[0])
        snapshot["phase"] = "confirmation_failed"
        core.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(snapshot),))
    before = fingerprints(project, run_dir)
    result = read_research_state(run_dir)
    assert result["lifecycle_phase"] == "confirmation_failed"
    assert result["confirmation_resource_consumed"] is True
    assert "不得重新消费" in result["lifecycle_reason"]
    assert fingerprints(project, run_dir) == before
