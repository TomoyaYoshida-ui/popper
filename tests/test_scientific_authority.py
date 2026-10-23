"""Confirmation claims require scientific_authority authorization."""
from __future__ import annotations

import unittest

from popper.core import ProtocolError
from popper.research.scientific_authority import (
    REFUSAL,
    authorize_confirmation,
    confirmation_authorized,
    require_confirmation_authorized,
)


class ScientificAuthorityTests(unittest.TestCase):
    def test_default_not_authorized(self):
        self.assertFalse(confirmation_authorized())
        with self.assertRaisesRegex(ProtocolError, "authorize_confirmation"):
            require_confirmation_authorized()

    def test_context_grants_and_releases(self):
        with authorize_confirmation(actor="confirmation_controller"):
            self.assertTrue(confirmation_authorized())
            require_confirmation_authorized()
        self.assertFalse(confirmation_authorized())

    def test_empty_actor_rejected(self):
        with self.assertRaises(ProtocolError):
            with authorize_confirmation(actor=""):
                pass

    def test_refusal_message_is_stable(self):
        self.assertIn("authorize_confirmation", REFUSAL)
        self.assertIn("确认集", REFUSAL)


if __name__ == "__main__":
    unittest.main()
