"""Confirmation claims require scientific_authority authorization."""
from __future__ import annotations

import unittest

from popper.core import ProtocolError
from popper.research.scientific_authority import (
    EVIDENCE_REFUSAL,
    REFUSAL,
    authorize_confirmation,
    authorize_evidence_write,
    confirmation_authorized,
    evidence_write_authorized,
    require_confirmation_authorized,
    require_evidence_write_authorized,
)
from popper.research.runtime_facade import (
    ScientificRuntime, campaign_is_not_scientific_runtime)


class ScientificAuthorityTests(unittest.TestCase):
    def test_default_not_authorized(self):
        self.assertFalse(confirmation_authorized())
        self.assertFalse(evidence_write_authorized())
        with self.assertRaisesRegex(ProtocolError, "authorize_confirmation"):
            require_confirmation_authorized()
        with self.assertRaisesRegex(ProtocolError, "authorize_evidence_write"):
            require_evidence_write_authorized()

    def test_context_grants_and_releases(self):
        with authorize_confirmation(actor="confirmation_controller"):
            self.assertTrue(confirmation_authorized())
            self.assertTrue(evidence_write_authorized())
            require_confirmation_authorized()
            require_evidence_write_authorized()
        self.assertFalse(confirmation_authorized())
        self.assertFalse(evidence_write_authorized())

    def test_evidence_write_alone(self):
        with authorize_evidence_write(actor="research_controller"):
            self.assertTrue(evidence_write_authorized())
            self.assertFalse(confirmation_authorized())
        self.assertFalse(evidence_write_authorized())

    def test_empty_actor_rejected(self):
        with self.assertRaises(ProtocolError):
            with authorize_confirmation(actor=""):
                pass
        with self.assertRaises(ProtocolError):
            with authorize_evidence_write(actor=""):
                pass

    def test_refusal_message_is_stable(self):
        self.assertIn("authorize_confirmation", REFUSAL)
        self.assertIn("确认集", REFUSAL)
        self.assertIn("authorize_evidence_write", EVIDENCE_REFUSAL)

    def test_campaign_is_not_scientific_runtime(self):
        meta = campaign_is_not_scientific_runtime()
        self.assertFalse(meta["scientific_runtime"])
        self.assertTrue(meta["orchestration_only"])
        self.assertEqual("research-runtime/v1", meta["confirm_requires"])

    def test_facade_rejects_unknown_kind(self):
        with self.assertRaises(ProtocolError):
            ScientificRuntime("campaign")


if __name__ == "__main__":
    unittest.main()
