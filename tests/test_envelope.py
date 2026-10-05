import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from popper.core import ProtocolError
from popper.research.envelope import (
    assert_active, build_envelope, covering_envelope, covers_tool, save_envelope, write_usage)
from popper.research.store import ResearchStore
from popper.sessions import AgentSessionStore


def _envelope(**overrides):
    fields = dict(study_id="S-demo", max_dev_runs=2, max_model_tokens=1000,
                  max_wall_clock_minutes=30, code_files=["src/model.py"], state_version=1,
                  approved_at="2026-10-05T00:00:00+00:00")
    fields.update(overrides)
    return build_envelope(**fields)


class EnvelopeTests(unittest.TestCase):
    def test_digest_is_stable_and_rejects_confirmation(self):
        first = _envelope()
        second = _envelope(code_files=["src/model.py", "src/model.py"])
        self.assertEqual(first["envelope_sha256"], second["envelope_sha256"])
        with self.assertRaises(ProtocolError):
            _envelope(allowed_actions=("confirm",))
        with self.assertRaises(ProtocolError):
            _envelope(code_files=["../secret.py"])

    def test_limits_and_hash_are_enforced(self):
        envelope = _envelope()
        assert_active(envelope, dev_runs_used=1, state_version=1,
                      now=datetime(2026, 10, 5, 0, 10, tzinfo=timezone.utc))
        with self.assertRaises(ProtocolError):
            assert_active(envelope, dev_runs_used=2, state_version=1)
        with self.assertRaises(ProtocolError):
            assert_active(envelope, dev_runs_used=0, state_version=2)
        later = datetime.fromisoformat(envelope["approved_at"]) + timedelta(minutes=31)
        with self.assertRaises(ProtocolError):
            assert_active(envelope, dev_runs_used=0, state_version=1, now=later)
        broken = {**envelope, "max_dev_runs": 3}
        with self.assertRaises(ProtocolError):
            assert_active(broken, dev_runs_used=0, state_version=1)

    def test_cover_stops_at_confirmation_and_unlisted_files(self):
        envelope = _envelope()
        self.assertTrue(covers_tool(envelope, "workspace.file_replace", {"path": "src/model.py"}))
        self.assertFalse(covers_tool(envelope, "workspace.file_replace", {"path": "src/other.py"}))
        self.assertFalse(covers_tool(envelope, "research.confirm_claim", {"claim_id": "C-1"}))
        self.assertFalse(covers_tool(
            envelope, "research.study_action", {"action": "confirm", "study_id": "S-demo"}))
        self.assertTrue(covers_tool(
            envelope, "research.study_action", {"action": "run", "study_id": "S-demo"}))

    def test_saved_usage_stops_later_auto_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".popper" / "research-runs" / "demo"
            save_envelope(run_dir, _envelope(approved_at=datetime.now(timezone.utc).isoformat()))
            self.assertIsNotNone(covering_envelope(
                root, "workspace.file_replace", {"path": "src/model.py"}))
            write_usage(run_dir, dev_runs_used=2)
            self.assertIsNone(covering_envelope(
                root, "workspace.file_replace", {"path": "src/model.py"}))

    def test_approval_is_one_research_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp) / "research.sqlite")
            try:
                envelope = _envelope(approved_at=datetime.now(timezone.utc).isoformat())
                first = store.record_envelope_approval(envelope)
                second = store.record_envelope_approval(envelope)
                self.assertEqual(first, second)
                integrity = store.verify()
                self.assertTrue(integrity["ok"], integrity)
            finally:
                store.close()


class EnvelopeApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AgentSessionStore(self.root)
        self.session = self.store.default_session(create=True, model="test-model")
        self.turn, _ = self.store.start_turn(
            self.session["session_id"], "改模型", "request-envelope",
            actor="test-user", trace_id="trc_envelope_turn")
        self.store.complete_turn(self.turn["turn_id"], "可以提议工具")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _propose(self, name, arguments, risk, key):
        call, replayed = self.store.propose_tool_call(
            self.session["session_id"], self.turn["turn_id"], name, arguments, risk, key,
            actor="agent", trace_id="trc_envelope_call")
        self.assertFalse(replayed)
        return call

    def test_covered_write_is_approved_and_confirmation_stays_pending(self):
        save_envelope(self.root / ".popper" / "research-runs" / "demo",
                      _envelope(approved_at=datetime.now(timezone.utc).isoformat()))
        covered = self._propose(
            "workspace.file_replace", {"path": "src/model.py", "search": "a", "replace": "b"},
            "write", "envelope-covered-01")
        self.assertEqual("approved", covered["status"])
        outside = self._propose(
            "workspace.file_replace", {"path": "src/other.py", "search": "a", "replace": "b"},
            "write", "envelope-outside-01")
        self.assertEqual("pending_approval", outside["status"])
        confirm = self._propose(
            "research.study_action", {"action": "confirm", "study_id": "S-demo", "max_steps": 1},
            "execute", "envelope-confirm-01")
        self.assertEqual("pending_approval", confirm["status"])
