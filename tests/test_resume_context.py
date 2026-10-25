"""Resume checkpoint + projection. Offline; no network."""
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.research.memory_context import (
    MEMORY_ENTRY_LIMIT, project_memory_context, record_memory_entry,
    record_resume_checkpoint, read_resume_checkpoint,
)
from popper.research.resume_context import project_resume_context


class ResumeContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_checkpoint_overwrites_single_slot(self):
        first = record_resume_checkpoint(
            self.run_dir, 'resume after literature', refs=['LIT-1'], study_id='S')
        second = record_resume_checkpoint(
            self.run_dir, 'next: run confirm', refs=['OBS-1'], study_id='S')
        self.assertNotEqual(first, second)
        cp = read_resume_checkpoint(self.run_dir)
        self.assertEqual(second, cp['id'])
        self.assertEqual('next: run confirm', cp['text'])
        self.assertEqual(['OBS-1'], cp['refs'])
        # Checkpoint is separate from the FIFO entry list.
        self.assertEqual([], project_memory_context(self.run_dir)['entries'])
        self.assertEqual(second, project_memory_context(self.run_dir)['checkpoint']['id'])

    def test_checkpoint_rejects_mem_refs_and_scores(self):
        with self.assertRaisesRegex(ProtocolError, 'OBS-|LIT-|SCOOP-|FT-'):
            record_resume_checkpoint(self.run_dir, 'bad', refs=['MEM-1'])
        with self.assertRaisesRegex(ProtocolError, 'evaluation fields'):
            record_resume_checkpoint(
                self.run_dir, {'text': 'x', 'mean_effect': 0.1})

    def test_checkpoint_does_not_consume_fifo_slots(self):
        record_resume_checkpoint(self.run_dir, 'checkpoint intent', study_id='S')
        for i in range(MEMORY_ENTRY_LIMIT):
            record_memory_entry(self.run_dir, 'action_note', f'note-{i}', study_id='S')
        ctx = project_memory_context(self.run_dir)
        self.assertEqual(MEMORY_ENTRY_LIMIT, len(ctx['entries']))
        self.assertIsNotNone(ctx['checkpoint'])

    def test_resume_projection_includes_checkpoint_without_metrics(self):
        record_resume_checkpoint(self.run_dir, 'continue confirm', study_id='S')
        record_memory_entry(self.run_dir, 'action_note', 'did something', study_id='S')
        resume = project_resume_context(
            self.run_dir, phase='ready_for_confirmation', integrity_ok=True,
            last_runtime_step=2, last_action_kind='execute_candidate',
            remaining_experiments=1)
        self.assertEqual('research-resume/v1', resume['schema'])
        self.assertEqual(2, resume['last_runtime_step'])
        self.assertEqual('execute_candidate', resume['last_action_kind'])
        self.assertEqual('continue confirm', resume['checkpoint']['text'])
        blob = str(resume)
        self.assertNotIn('mean_effect', blob)
        self.assertNotIn('ci95', blob)


if __name__ == '__main__':
    unittest.main()
