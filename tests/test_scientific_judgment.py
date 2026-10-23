import json
import shutil
import tempfile
import unittest
from pathlib import Path
from popper.core import ProtocolError, initialize
from popper.research.controller import ResearchController
from popper.research.judgment import interval_verdict
from popper.research.scientific_authority import authorize_evidence_write
from popper.research.store import ResearchStore


class JudgmentTests(unittest.TestCase):
    def test_evidence_changes_verdict_without_changing_prediction(self):
        self.assertEqual('supported',interval_verdict('gte',.05,[.06,.1]))
        self.assertEqual('refuted',interval_verdict('gte',.05,[-.1,.01]))
        self.assertEqual('inconclusive',interval_verdict('gte',.05,[-.1,.1]))
        self.assertEqual('inconclusive',interval_verdict('gte',.05,[.06,.1],quality='insufficient'))
        self.assertEqual('supported',interval_verdict('lt',.05,[-.1,.01]))
        with self.assertRaises(ProtocolError): interval_verdict('gte',.05,[float('nan'),1])

    def test_old_store_is_not_backfilled_and_new_judgments_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); project=root/'project'; run=root/'run'
            shutil.copytree(Path(__file__).resolve().parents[1]/'examples/quadratic',project,
                            ignore=shutil.ignore_patterns('.popper','__pycache__'))
            initialize(project); ResearchController.initialize(project,run)
            copy=root/'legacy-copy.sqlite'; shutil.copy2(run/'research.sqlite',copy)
            legacy=ResearchStore(copy)
            self.assertEqual([],legacy.list('judgment')); self.assertTrue(legacy.verify()['ok']); legacy.close()
            controller=ResearchController(run)
            try:
                status=controller.run(trusted_local=True,auto_confirm=False,max_steps=1)
                self.assertTrue(status['integrity']['ok'])
                judgment=status['judgments'][0]
                self.assertEqual('not_recorded',judgment['old_judgment'])
                self.assertEqual('inconclusive',judgment['new_judgment'])
                self.assertFalse(judgment['mechanism_proven'])
                with authorize_evidence_write(actor='test'):
                    again=controller.store.record_judgment(judgment['hypothesis_id'],judgment['hypothesis_version'],judgment['observation_refs'])
                self.assertEqual(judgment,again)
                self.assertEqual(1,len(controller.store.list('judgment')))
                self.assertTrue(any(d.get('judgment_refs') for d in status['decisions']))
                artifact=Path(next(o['artifact_id'] for o in status['observations'] if o['observation_id'] in judgment['observation_refs']))
                artifact.write_text('{}',encoding='utf-8')
                self.assertFalse(controller.store.verify()['ok'])
                with self.assertRaises(ProtocolError):
                    with authorize_evidence_write(actor='test'):
                        controller.store.record_judgment(judgment['hypothesis_id'],judgment['hypothesis_version'],())
                with self.assertRaisesRegex(ProtocolError, 'authorize_evidence_write'):
                    controller.store.record_judgment(judgment['hypothesis_id'],judgment['hypothesis_version'],judgment['observation_refs'])
            finally: controller.close()

    def test_execution_failure_is_not_scientific_refutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); project=root/'project'; run=root/'run'
            shutil.copytree(Path(__file__).resolve().parents[1]/'examples/quadratic',project,
                            ignore=shutil.ignore_patterns('.popper','__pycache__'))
            initialize(project); ResearchController.initialize(project,run)
            controller=ResearchController(run)
            try:
                hid=controller.manifest['candidate_hypothesis_ids'][0]
                with authorize_evidence_write(actor='test'):
                    judgment=controller.store.record_judgment(hid,1,())
                self.assertEqual('not_evaluated',judgment['new_judgment'])
                self.assertTrue(controller.store.verify()['ok'])
            finally: controller.close()
