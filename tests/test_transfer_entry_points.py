import ast
import unittest
from pathlib import Path
from unittest.mock import patch
from popper.capabilities import extra_available
from popper.core import ProtocolError


class TransferEntryPoints(unittest.TestCase):
    def test_pilot_entry_points_and_model_compile(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('evaluation/run_transfer_pilot.py','evaluation/transfer_tasks.py',
                     'evaluation/verify_transfer_pilot.py','evaluation/validate_transfer_environment.py',
                     'integrations/transfer_pilot/model.py'):
            with self.subTest(name=name): ast.parse((root/name).read_text(encoding='utf-8'),filename=name)

    @unittest.skipUnless(extra_available('figs'),'figs extra required')
    def test_readonly_verifier_upgrade_cannot_override_experimental_sources(self):
        from evaluation.verify_transfer_pilot import verify_frozen_sources
        actual={'evaluation/verify_transfer_pilot.py':'new','popper/core.py':'fixed'}
        with patch('evaluation.verify_transfer_pilot.snapshot',return_value=actual):
            self.assertTrue(verify_frozen_sources({'implementation':{**actual,'evaluation/verify_transfer_pilot.py':'old'}},True))
            with self.assertRaises(ProtocolError):
                verify_frozen_sources({'implementation':{**actual,'popper/core.py':'other'}},True)
