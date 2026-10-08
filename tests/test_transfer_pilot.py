import json
import shutil
import tempfile
import unittest
from pathlib import Path
from popper.capabilities import extra_available
from popper.core import ProtocolError, digest, initialize, read_json
from popper.research.controller import ResearchController
from popper.research.paired_comparison import validate_protocol
from evaluation.transfer_tasks import prepare,task_rows,configurations


@unittest.skipUnless(extra_available('figs'),'figs extra required')
class TransferTests(unittest.TestCase):
    def test_shift_task_sizes_scope_and_no_oracle_in_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); info=prepare('T02',root/'project',root/'cache')
            self.assertEqual({'train':600,'dev':400,'test':400},info['split_rows'])
            protocol=validate_protocol(read_json(root/'project/judgment-protocol.json'))
            self.assertEqual('independent-',protocol['primary_dev_prefix'])
            self.assertEqual(8,len(read_json(root/'project/experiment.json')['candidates']))
            self.assertNotIn('sin(f0)',json.dumps(protocol))
            train={r['id'] for r in read_json(root/'project/train.json')}
            test={r['id'] for r in read_json(root/'project/test.json')}
            self.assertFalse(train&test)
            with self.assertRaises(ValueError): prepare('T02',root/'project',root/'cache')

    def test_registered_candidate_independent_execution_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); project=root/'project'; run=root/'research'
            prepare('T03',project,root/'cache'); initialize(project); ResearchController.initialize(project,run,budget_cap=7)
            c=ResearchController(run)
            try:
                hid=c.manifest['candidate_hypothesis_ids'][2]
                version=c.store.get('study',c.manifest['study_id'])['state_version']
                result=c.execute_registered_candidate(hid,expected_version=version,trusted_local=True)
                self.assertEqual(2,len(result['judgments']))
                self.assertTrue(c.store.verify()['ok'])
                again=c.execute_registered_candidate(hid,expected_version=result['state_version'],trusted_local=True)
                self.assertEqual(result['observation']['observation_id'],again['observation']['observation_id'])
                with self.assertRaises(ProtocolError):
                    c.execute_registered_candidate(hid,expected_version=version,trusted_local=True)
            finally: c.close()

    def test_primary_scope_is_independent_development_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); project=root/'project'; run=root/'research'
            prepare('T02',project,root/'cache'); initialize(project); ResearchController.initialize(project,run,budget_cap=7)
            c=ResearchController(run)
            try:
                version=c.store.get('study',c.manifest['study_id'])['state_version']
                result=c.execute_registered_candidate(c.manifest['candidate_hypothesis_ids'][2],expected_version=version,trusted_local=True)
                self.assertEqual('dev:slice:s2',result['observation']['scope'])
                self.assertEqual(200,result['judgments'][0]['evidence']['n_units'])
                self.assertTrue(c.store.verify()['ok'])
            finally: c.close()
