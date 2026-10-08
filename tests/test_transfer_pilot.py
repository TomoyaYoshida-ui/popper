import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from popper.capabilities import extra_available
from popper.core import ProtocolError, digest, initialize, read_json,file_hash,write_json
from popper.research.controller import ResearchController
from popper.research.paired_comparison import validate_protocol,paired_statistics
from popper.research.judgment import interval_verdict
from evaluation.transfer_tasks import prepare,task_rows,configurations
from evaluation.run_transfer_pilot import revise_unexecuted,agent_context,run_cell,confirm_cell,require_environment_gates


@unittest.skipUnless(extra_available('figs'),'figs extra required')
class TransferTests(unittest.TestCase):
    def test_paid_requests_are_blocked_by_missing_environment_gates(self):
        from evaluation.run_transfer_pilot import run
        with tempfile.TemporaryDirectory() as tmp, patch('evaluation.run_transfer_pilot.prepare_cohort',return_value={}),patch('evaluation.run_transfer_pilot.preflight') as model_preflight:
            with self.assertRaises(ProtocolError): run(Path(tmp),offline=False)
            model_preflight.assert_not_called()

    def test_preflight_stops_auth_immediately_and_infrastructure_after_three_probes(self):
        from evaluation.run_transfer_pilot import preflight
        from popper.request_lifecycle import RequestFailure
        for category,count in (('authentication_or_permission',1),('connect_timeout',3)):
            with tempfile.TemporaryDirectory() as tmp,patch('evaluation.run_transfer_pilot.make_json_client') as factory:
                factory.return_value.side_effect=RequestFailure(category,sent=False)
                with self.assertRaises(RequestFailure): preflight(Path(tmp),{'protocol_sha256':'fixture'})
                self.assertEqual(count,factory.return_value.call_count)
                self.assertEqual('failed',read_json(Path(tmp)/'preflight.json')['status'])

    def test_confirmation_replays_without_reconsumption_and_ambiguous_step_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); taskroot=root/'tasks/T03'
            info=prepare('T03',taskroot,root/'cache')
            files={p.name:file_hash(p) for p in taskroot.iterdir() if p.is_file() and p.name!='oracle-private.json'}
            protocol={'protocol_sha256':'fixture','tasks':[{**info,'files':files}]}
            cell={'id':'T03-fixture','task':'T03','strategy':'fixed_queue','repeat':0,'candidate_order':[2,3,6,7,0,1,4,5]}
            result=run_cell(root,protocol,cell,offline=True)
            self.assertEqual('development_completed',result['status'])
            roster={'finalists':{cell['id']:result['finalist']},'protocol_sha256':'fixture'}
            receipt=confirm_cell(root,protocol,cell,roster)
            self.assertEqual('completed',receipt['status'])
            self.assertEqual(receipt,confirm_cell(root,protocol,cell,roster))
            c=ResearchController(root/'cells'/cell['id']/'research')
            try:
                self.assertTrue(c.store.verify()['ok'])
                self.assertFalse(c.store.budget(c.manifest['study_id']).outstanding(c.manifest['family_id']))
            finally: c.close()
            other={**cell,'id':'T03-interrupted'}
            interrupted=root/'cells'/other['id']; interrupted.mkdir()
            write_json(interrupted/'step-0.json',{'status':'started'})
            stopped=run_cell(root,protocol,other,offline=True)
            self.assertEqual('failed',stopped['status'])
            self.assertEqual([],stopped['executions'])
            with self.assertRaises(ProtocolError): require_environment_gates(root,protocol)

    def test_shift_confirmation_does_not_score_inapplicable_source_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); taskroot=root/'tasks/T02'; info=prepare('T02',taskroot,root/'cache')
            files={p.name:file_hash(p) for p in taskroot.iterdir() if p.is_file() and p.name!='oracle-private.json'}
            protocol={'protocol_sha256':'fixture','tasks':[{**info,'files':files}]}
            cell={'id':'T02-fixture','task':'T02','strategy':'fixed_queue','repeat':0,'candidate_order':[2,3,6,7,0,1,4,5]}
            result=run_cell(root,protocol,cell,offline=True)
            self.assertEqual('development_completed',result['status'])
            roster={'finalists':{cell['id']:result['finalist']},'protocol_sha256':'fixture'}
            receipt=confirm_cell(root,protocol,cell,roster)
            self.assertEqual('completed',receipt['status'])
            self.assertTrue(all(j['evidence']['n_units']==400 for j in receipt['judgments']))
            self.assertTrue(receipt['integrity']['ok'])

    def test_logical_decision_limit_does_not_promote_partial_model_unit_to_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); taskroot=root/'tasks/T03'; info=prepare('T03',taskroot,root/'cache')
            files={p.name:file_hash(p) for p in taskroot.iterdir() if p.is_file() and p.name!='oracle-private.json'}
            protocol={'protocol_sha256':'fixture','tasks':[{**info,'files':files}]}
            cell={'id':'T03-model-limit','task':'T03','strategy':'generic_iteration','repeat':0,'candidate_order':list(range(8))}
            calls=[]
            def decision(system,context,**kwargs):
                index=len(calls); calls.append(index)
                action='experiment' if index<2 else 'revise'
                hid=context['candidates'][2+index if index<2 else 0]['hypothesis_id']
                refs=context['raw_evidence'][0]['evidence_refs'] if context['raw_evidence'] else []
                proposed={'kind':'execute_candidate' if action=='experiment' else 'revise_hypothesis',
                    'candidate_id' if action=='experiment' else 'hypothesis_id':hid,
                    'rationale':'Bounded local model-limit fixture','evidence_refs':refs}
                if action=='revise': proposed.update(mechanism='Fixed predictive intervention',
                    applicability='Independent units',predictions=['Relative MAE improvement exceeds 5%.'])
                return {'action':proposed,'assessment':None}
            with patch('evaluation.run_transfer_pilot.make_json_client',return_value=decision):
                result=run_cell(root,protocol,cell)
            self.assertEqual(9,len(calls))
            self.assertEqual('incomplete',result['status'])
            self.assertEqual('request_budget_exhausted',result['failure_category'])
            self.assertIsNotNone(result['finalist'])
            self.assertEqual(2,len(result['executions']))

    def test_sufficiency_gate_and_distinct_scientific_verdicts(self):
        for n,improvement,expected in ((30,.15,'supported'),(30,-.15,'refuted'),(16,.15,'inconclusive')):
            result=paired_statistics([1.]*n,[1.-improvement]*n,20261009)
            self.assertEqual(expected,interval_verdict('gt',.05,result['ci95'],quality=result['quality']))
        result=paired_statistics([1.]*40,[.5]*20+[1.5]*20,20261009)
        self.assertEqual('inconclusive',interval_verdict('gt',.05,result['ci95']))

    def test_revision_uses_native_design_identity_and_ablation_raw_facts_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); project=root/'project'; run=root/'research'
            prepare('T03',project,root/'cache'); initialize(project)
            ResearchController.initialize(project,run,budget_cap=7)
            c=ResearchController(run)
            try:
                first,second=c.manifest['candidate_hypothesis_ids'][2:4]
                c.execute_registered_candidate(first,expected_version=c.store.get('study',c.manifest['study_id'])['state_version'],trusted_local=True)
                before=c.store.event_cursor()
                plain=agent_context(c,{'task':'T03','strategy':'generic_iteration'},{first})
                rich=agent_context(c,{'task':'T03','strategy':'evidence_driven'},{first})
                self.assertEqual(before,c.store.event_cursor())
                self.assertNotIn('structured_judgments',plain)
                self.assertEqual(plain,{k:v for k,v in rich.items() if k!='structured_judgments'})
                revise_unexecuted(c,second,{'mechanism':'Predictive intervention','applicability':'Independent held-out units','predictions':['Relative MAE improvement exceeds 5%.']})
                self.assertEqual(2,c.store.get('hypothesis',second)['version'])
                self.assertEqual(2,c.store.get('design',c._current_design_id(second))['hypothesis_version'])
                c.execute_registered_candidate(second,expected_version=c.store.get('study',c.manifest['study_id'])['state_version'],trusted_local=True)
                self.assertTrue(c.store.verify()['ok'])
            finally: c.close()

    def test_shift_task_sizes_scope_and_no_oracle_in_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); info=prepare('T02',root/'project',root/'cache')
            self.assertEqual({'train':600,'dev':400,'test':400},info['split_rows'])
            protocol=validate_protocol(read_json(root/'project/judgment-protocol.json'))
            self.assertEqual('independent-',protocol['primary_dev_prefix'])
            self.assertEqual(8,len(read_json(root/'project/experiment.json')['candidates']))
            self.assertNotIn('sin(f0)',json.dumps(protocol))
            labels={s['scope_id']:s['label'] for s in protocol['scope_schema']}
            self.assertEqual('source development environment',labels['dev:slice:s1'])
            self.assertEqual('independent development environment',labels['dev:slice:s2'])
            self.assertEqual(['f0','f1','f2'],[f['name'] for f in protocol['feature_schema']])
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

    def test_four_experiments_stop_without_requiring_paid_final_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); taskroot=root/'tasks/T03'; info=prepare('T03',taskroot,root/'cache')
            files={p.name:file_hash(p) for p in taskroot.iterdir() if p.is_file() and p.name!='oracle-private.json'}
            protocol={'protocol_sha256':'fixture','tasks':[{**info,'files':files}]}
            cell={'id':'T03-runtime-cap','task':'T03','strategy':'generic_iteration','repeat':0,
                  'candidate_order':list(range(8))}
            calls=[]
            def choose(system,context,**kwargs):
                hid=context['candidates'][[2,3,6,7][len(calls)]]['hypothesis_id']
                calls.append(hid)
                return {'action':{'kind':'execute_candidate','candidate_id':hid,
                    'rationale':'Four frozen candidates','evidence_refs':[]},'assessment':None}
            with patch('evaluation.run_transfer_pilot.make_json_client',return_value=choose):
                result=run_cell(root,protocol,cell)
                self.assertEqual('development_completed',result['status'])
                replay=run_cell(root,protocol,cell)
            self.assertEqual(4,len(calls))
            self.assertEqual(result['finalist'],replay['finalist'])
            self.assertEqual('experiment_limit',result['runtime']['planner']['reason'])

    def test_confirmation_report_failure_recovers_kernel_saved_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); taskroot=root/'tasks/T03'; info=prepare('T03',taskroot,root/'cache')
            files={p.name:file_hash(p) for p in taskroot.iterdir() if p.is_file() and p.name!='oracle-private.json'}
            protocol={'protocol_sha256':'fixture','tasks':[{**info,'files':files}]}
            cell={'id':'T03-report-recovery','task':'T03','strategy':'fixed_queue','repeat':0,
                  'candidate_order':[2,3,6,7,0,1,4,5]}
            result=run_cell(root,protocol,cell,offline=True)
            roster={'finalists':{cell['id']:result['finalist']},'protocol_sha256':'fixture'}
            def fail_confirmation_report(path,value):
                if Path(path).name=='confirmation.json': raise RuntimeError('report interrupted')
                return write_json(path,value)
            with patch('evaluation.run_transfer_pilot.write_json',side_effect=fail_confirmation_report):
                with self.assertRaisesRegex(RuntimeError,'report interrupted'):
                    confirm_cell(root,protocol,cell,roster)
            # A partially written marker/report never authorizes a second look.
            receipt=confirm_cell(root,protocol,cell,roster)
            self.assertEqual('completed',receipt['status'])
            c=ResearchController(root/'cells'/cell['id']/'research')
            try:
                count=c.exp.db.execute("SELECT COUNT(*) FROM events WHERE kind='test_consumed'").fetchone()[0]
                self.assertEqual(1,count)
            finally: c.close()

    def test_v1_cohort_is_not_silently_reused(self):
        from evaluation.run_transfer_pilot import validate_cohort
        with self.assertRaises(ProtocolError):
            validate_cohort({'schema':'transfer-pilot/v1'},Path('.'))

    def test_planner_protocol_failure_preserves_independent_facts_and_frozen_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); taskroot=root/'tasks/T03'; info=prepare('T03',taskroot,root/'cache')
            files={p.name:file_hash(p) for p in taskroot.iterdir() if p.is_file() and p.name!='oracle-private.json'}
            protocol={'protocol_sha256':'fixture','tasks':[{**info,'files':files}]}
            cell={'id':'T03-protocol-failed','task':'T03','strategy':'generic_iteration','repeat':0,
                  'candidate_order':list(range(8))}
            calls=[]
            def choose(system,context,**kwargs):
                calls.append(1)
                if len(calls)==1:
                    return {'action':{'kind':'execute_candidate','candidate_id':context['candidates'][2]['hypothesis_id'],
                        'rationale':'Valid local candidate','evidence_refs':[]},'assessment':None}
                return {'action':{'kind':'stop_planning','candidate_id':'invalid overloaded stop target',
                    'rationale':'Invalid action fixture','evidence_refs':[]},'assessment':None}
            with patch('evaluation.run_transfer_pilot.make_json_client',return_value=choose):
                result=run_cell(root,protocol,cell)
            self.assertEqual('failed',result['status'])
            self.assertEqual(3,len(calls))
            self.assertEqual(1,len(result['executions']))
            self.assertIsNotNone(result['finalist'])
            roster={'finalists':{cell['id']:result['finalist']},'protocol_sha256':'fixture'}
            self.assertEqual('completed',confirm_cell(root,protocol,cell,roster)['status'])

    def test_durable_unanswered_model_request_blocks_repeat_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); taskroot=root/'tasks/T03'; info=prepare('T03',taskroot,root/'cache')
            files={p.name:file_hash(p) for p in taskroot.iterdir() if p.is_file() and p.name!='oracle-private.json'}
            protocol={'protocol_sha256':'fixture','tasks':[{**info,'files':files}]}
            cell={'id':'T03-request-interrupted','task':'T03','strategy':'generic_iteration','repeat':0,
                  'candidate_order':list(range(8))}
            project=root/'cells'/cell['id']/'project'
            shutil.copytree(taskroot,project,ignore=shutil.ignore_patterns('oracle-private.json'))
            initialize(project); research=project.parent/'research'
            ResearchController.initialize(project,research,budget_cap=7)
            c=ResearchController(research)
            try:
                runtime=c.planning_runtime(session_id='transfer:'+cell['id'],profile=cell['strategy'],
                                           max_experiments=4,max_decisions=9)
                c.prepare_registered_baseline(trusted_local=True)
                runtime._write('request_started',0,{'step':0,'context_sha256':digest(runtime.context()),
                    'logical_request_id':runtime._id('request_started',0)})
            finally: c.close()
            with patch('evaluation.run_transfer_pilot.make_json_client') as factory:
                result=run_cell(root,protocol,cell)
            factory.return_value.assert_not_called()
            self.assertEqual('incomplete',result['status'])
            self.assertEqual('request_recovery_blocked',result['failure_category'])

    def test_confirmation_requires_complete_frozen_cohort_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cell={'id':'first'}
            protocol={'protocol_sha256':'fixture','cells':[cell,{'id':'second'}]}
            roster={'protocol_sha256':'fixture','finalists':{'first':None}}
            with self.assertRaisesRegex(ProtocolError,'Every cohort selection'):
                confirm_cell(root,protocol,cell,roster)
