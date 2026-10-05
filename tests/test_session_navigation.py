import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError
from popper.sessions import AgentSessionStore, SessionMetadataConflict, read_session_summaries


class SessionNavigationTests(unittest.TestCase):
    def test_summary_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual([], read_session_summaries(directory))
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_versioned_rename_archive_restore_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentSessionStore(directory)
            try:
                session = store.create_session('原标题', 'chat', 'mock')
                sid = session['session_id']
                store.set_default_session(sid)
                renamed = store.update_session_metadata(sid, session['updated'], title='新标题')
                with self.assertRaises(SessionMetadataConflict):
                    store.update_session_metadata(sid, session['updated'], title='过期标题')
                turn, _ = store.start_turn(sid, '合成测试', 'navigation-001', actor='test', trace_id='trc_navigation')
                snapshot = store.path.read_bytes()
                summaries = read_session_summaries(directory)
                self.assertTrue(summaries[0]['running'])
                self.assertEqual(snapshot, store.path.read_bytes())
                with self.assertRaisesRegex(ProtocolError, '先停止'):
                    store.update_session_metadata(sid, store.get_session(sid)['updated'], status='archived')
                store.complete_turn(turn['turn_id'], '保留的回复')
                archived = store.update_session_metadata(sid, store.get_session(sid)['updated'], status='archived')
                self.assertIsNone(store.default_session(create=False))
                with self.assertRaises(ProtocolError):
                    store.set_default_session(sid)
                restored = store.update_session_metadata(sid, archived['updated'], status='active')
                store.set_default_session(sid)
                self.assertEqual('新标题', restored['title'])
                self.assertEqual('保留的回复', store.turns(sid)[0]['assistant_message'])
            finally:
                store.close()
