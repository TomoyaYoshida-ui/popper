import tempfile
import unittest
from pathlib import Path
from popper.core import ProtocolError, digest, file_hash, initialize, read_json, write_json
from popper.research.grouped_comparison import evaluate, verify_artifact
from popper.capabilities import extra_available


class NativeFigsTests(unittest.TestCase):
    def test_confirmation_refit_cannot_include_test_or_altered_labels(self):
        import shutil
        example=Path(__file__).resolve().parents[1]/'examples/quadratic'
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)/'project'; shutil.copytree(example,project,ignore=shutil.ignore_patterns('.popper','__pycache__'))
            spec=read_json(project/'experiment.json'); spec['confirmation_train']='refit.json'; write_json(project/'experiment.json',spec)
            rows=read_json(project/spec['train'])+read_json(project/spec['dev'])
            write_json(project/'refit.json',rows+read_json(project/spec['test']))
            with self.assertRaisesRegex(ProtocolError,'train'): initialize(project)
            write_json(project/'refit.json',rows)
            initialize(project)
            from popper.core import Experiment
            exp=Experiment(project)
            self.assertIn('refit.json',exp.state()['input_hashes']); exp.close()

    def packet(self,root,candidate_value):
        from scripts.prepare_figs_concrete import UPSTREAM
        protocol={'schema':'figs-grouped-ci/v1','bootstrap_seed':20261008,'bootstrap_samples':10000,
                  'minimum_groups':30,'prediction':{'operator':'gt','threshold':.05},
                  'hypothesis_predictions':{'H1':{'operator':'gt','threshold':.05}},'upstream_commit':UPSTREAM,
                  'features':{str(i):[0.] for i in range(32)},'groups':{str(i):f'group-{i}' for i in range(32)}}
        write_json(root/'protocol.json',protocol)
        rows=[{'id':str(i),'x':i,'y':0.} for i in range(32)]; write_json(root/'data.json',rows)
        observations=[]
        for index,value in enumerate((10.,candidate_value)):
            run=root/str(index); run.mkdir()
            write_json(run/'predictions.json',[{'id':str(i),'prediction':value} for i in range(32)])
            t={'value':[value]}
            for _ in range(12): t={'feature':0,'threshold':1.,'left':t,'right':{'value':[value]}}
            write_json(run/'model-audit-17.json',{'upstream_commit':UPSTREAM,'seed':17,'budget':12,'actual_rules':12,'tree_count':1,'trees':[t]})
            req={'dataset':str(root/'data.json'),'dataset_sha256':file_hash(root/'data.json'),'expected_seeds':[17],
                 'predictions':[{'path':str(run/'predictions.json'),'sha256':file_hash(run/'predictions.json')}]}
            write_json(run/'request.json',req)
            write_json(run/'response.json',{'request_sha256':digest(req),'service_sha256':'fixture'})
            observations.append({'artifact_id':str(run/'response.json'),'artifact_sha256':file_hash(run/'response.json'),
                                 'evaluator_service_hash':'fixture', 'observation_id':f'OBS-{index}',
                                 'scope':'dev','scorer_id':'mae-v1','hypothesis_id':'H1' if index else 'control',
                                 'hypothesis_version':1})
        return {'protocol_path':str(root/'protocol.json'),'protocol_sha256':file_hash(root/'protocol.json'),
                'observations':observations}

    @unittest.skipUnless(extra_available('ml'), 'ml extra required for grouped bootstrap')
    def test_paired_group_interval_recomputes_and_rejects_artifact_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); packet=self.packet(root,9.)
            result=evaluate(packet)
            self.assertAlmostEqual(.1,result['mean_effect'])
            self.assertEqual(32,result['n_groups'])
            self.assertEqual('valid',result['quality'])
            write_json(root/'request.json',packet); write_json(root/'response.json',result)
            descriptor={'request':str(root/'request.json'),'response':str(root/'response.json'),
                        'request_sha256':file_hash(root/'request.json'),'response_sha256':file_hash(root/'response.json')}
            self.assertEqual(result,verify_artifact(descriptor,packet['observations']))
            write_json(root/'1/predictions.json',[{'id':'0','prediction':1.}])
            with self.assertRaises(ProtocolError): verify_artifact(descriptor,packet['observations'])

    @unittest.skipUnless(extra_available('ml'), 'ml extra required for grouped bootstrap')
    def test_same_frozen_prediction_changes_judgment_and_next_action_with_evidence(self):
        from popper.research.judgment import derive
        for value,verdict,action in ((9.,'supported','continue_sampling'),(11.,'refuted','revise_hypothesis')):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); packet=self.packet(root,value)
                write_json(root/'request.json',packet); write_json(root/'response.json',evaluate(packet))
                descriptor={'request':str(root/'request.json'),'response':str(root/'response.json'),
                    'request_sha256':file_hash(root/'request.json'),'response_sha256':file_hash(root/'response.json')}
                result=derive({'hypothesis_id':'H1','version':1,'study_id':'S1'}, {}, packet['observations'],
                              comparison_artifact=descriptor)
                self.assertEqual(verdict,result['new_judgment']); self.assertEqual(action,result['next_action'])
                self.assertFalse(result['mechanism_proven'])
