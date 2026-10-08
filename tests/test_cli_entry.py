import contextlib
import io
import unittest
from popper.cli import main


class CliEntryTests(unittest.TestCase):
    def test_all_command_parsers_can_be_constructed(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit:
                main(['--help'])
        self.assertEqual(0,exit.exception.code)
        self.assertIn('research',output.getvalue())

    def test_existing_metrics_list_dispatches_without_register_arguments(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0,main(['research','metrics','list']))
        self.assertIn('metrics',output.getvalue())
