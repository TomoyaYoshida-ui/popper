"""Study memory projection: bounded notes, never evaluation authority."""
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.research.memory_context import (
    MEMORY_ENTRY_LIMIT, MEMORY_TEXT_CHARS, project_memory_context,
    record_memory_entry, study_memory,
)


class MemoryContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_projection(self):
        ctx = project_memory_context(self.run_dir)
        self.assertEqual('research-memory-context/v1', ctx['schema'])
        self.assertEqual('unverified_session_notes', ctx['trust'])
        self.assertEqual([], ctx['entries'])

    def test_record_and_project_roundtrip(self):
        eid = record_memory_entry(
            self.run_dir, 'hypothesis_revision', 'H-1: shorter mechanism',
            refs=['OBS-1'], study_id='S-test')
        self.assertTrue(eid.startswith('MEM-'))
        ctx = project_memory_context(self.run_dir)
        self.assertEqual(1, len(ctx['entries']))
        self.assertEqual(eid, ctx['entries'][0]['id'])
        self.assertEqual('hypothesis_revision', ctx['entries'][0]['kind'])
        # Idempotent
        again = record_memory_entry(
            self.run_dir, 'hypothesis_revision', 'H-1: shorter mechanism',
            refs=['OBS-1'], study_id='S-test')
        self.assertEqual(eid, again)
        self.assertEqual(1, len(project_memory_context(self.run_dir)['entries']))

    def test_entry_limit_and_text_clip(self):
        for i in range(MEMORY_ENTRY_LIMIT + 5):
            record_memory_entry(
                self.run_dir, 'action_note', f'note-{i}-' + ('x' * 500),
                study_id='S-test')
        entries = project_memory_context(self.run_dir)['entries']
        self.assertEqual(MEMORY_ENTRY_LIMIT, len(entries))
        self.assertLessEqual(len(entries[-1]['text']), MEMORY_TEXT_CHARS)

    def test_rejects_score_payload(self):
        with self.assertRaisesRegex(ProtocolError, 'evaluation fields'):
            record_memory_entry(
                self.run_dir, 'judgment_outcome',
                {'text': 'bad', 'mean_effect': 1.0}, study_id='S')

    def test_cli_memory_dir_still_usable(self):
        mem = study_memory(self.run_dir)
        mem.set_working('step scratch')
        mem.set_summary({'schema': 'research-memory/v1', 'entries': []})
        self.assertGreater(mem.working_size(), 0)
        ctx = project_memory_context(self.run_dir)
        self.assertIn('step scratch', ctx['working'] or '')


if __name__ == '__main__':
    unittest.main()
