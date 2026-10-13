import json
import subprocess
from pathlib import Path
import pytest
from popper.core import ProtocolError, file_hash, write_json, digest
from popper.research.adaptive_contract import AdaptiveTaskContract
from popper.research.adaptive_runtime import AdaptiveResearchRuntime
from popper.research.adaptive_protocol import parse_action
from tests.test_pydantic_planner import server as planner_server, budget as planner_budget, response

SOURCE = '''import argparse,json
p=argparse.ArgumentParser()
for name in ('train','input','output','config','seed'):p.add_argument('--'+name,required=True)
a=p.parse_args();rows=json.load(open(a.input));json.dump([{'id':r['id'],'prediction':PRED} for r in rows],open(a.output,'w'))
'''
BASELINE = SOURCE.replace('PRED', '0.0')
CANDIDATE = SOURCE.replace('PRED', "r['features'][0]")


def executor(command, workspace, env, stdout, stderr, spec):
    subprocess.run(command, cwd=workspace, env=env, stdout=stdout, stderr=stderr, check=True)


def make_runtime(tmp_path):
    paths, groups = {}, {}
    for split, offset in [('train', 0), ('dev', 100), ('confirmation', 200)]:
        rows = [{'id': str(i), 'features': [float(i + 1)], 'y': float(i + 1)} for i in range(offset, offset + 40)]
        for row in rows:
            groups[row['id']] = digest([float(row['features'][0])])
        paths[split] = tmp_path / f'{split}.json'
        write_json(paths[split], rows)
    contract = AdaptiveTaskContract(task_id='test', objective='Predict a continuous target',
        target_column='y', feature_names=['x'], data_hashes={k: file_hash(p) for k,p in paths.items()},
        baseline={'model':'constant'}, analysis_unit='independent rows', independence_verified=True,
        sampling={'design': 'iid_row', 'generator_version': 'adaptive-tasks/v1'},
        groups=groups, bootstrap_seed=123, model_seed=17,
        materials=[{'id':'dataset-card','text':'Independent continuous predictor and target'}]).model_dump()
    return AdaptiveResearchRuntime(tmp_path / 'run', contract, paths, BASELINE, executor=executor)


def problem():
    return parse_action({'kind':'propose_problem','question':'Does using the measured predictor help?',
        'motivation':'Compare prediction errors with the constant control',
        'explanations':['The predictor contains signal','The predictor is irrelevant'],
        'discriminating_intervention':'Fit using the predictor versus omit it', 'rationale':'Test the explanations',
        'evidence_refs':['dataset-card']})


def experiment(source=CANDIDATE):
    return parse_action({'kind':'run_experiment','mechanism':'The measured predictor carries signal',
        'intervention':'Use the measured predictor', 'config':{}, 'source_code':source,
        'rationale':'Test a new executable intervention'})


def test_v2_problem_execution_confirmation_and_replay(tmp_path):
    r = make_runtime(tmp_path)
    try:
        with pytest.raises(ProtocolError):r.apply(experiment(), 'before-problem')
        r.apply(problem(), '0')
        r.apply(experiment(), '1')
        selected = r.freeze_selection('test')
        assert selected['finalist']['judgment'] == 'supported'
        count = len(r.store.list('run'))
        result = r.confirm({r.study_id: digest(selected)})
        assert result['judgment'] == 'supported'
        assert r.store.verify()['ok']
        assert len(r.store.list('run')) == count + 2
        assert r.confirm({r.study_id: digest(selected)}) == result
        assert len(r.store.list('run')) == count + 2
        with pytest.raises(ProtocolError):r.apply(experiment(), 'after-confirmation')
    finally:r.close()


def test_v2_failure_consumes_budget_and_retains_receipt(tmp_path):
    r = make_runtime(tmp_path)
    try:
        r.apply(problem(), '0')
        r.apply(experiment('raise RuntimeError("retained failure")'), '1')
        assert r.records('attempt_completed')[0]['data']['status'] == 'failed'
        assert r.store.budget(r.study_id).balance(r.study_id)['spent'] == 2.
        assert any(run['status']=='implementation_failed' for run in r.store.list('run'))
        assert r.store.verify()['ok']
        assert r.freeze_selection('failure')['finalist'] is None
    finally:r.close()


def test_v2_unknown_evidence_and_frozen_problem_are_rejected(tmp_path):
    r = make_runtime(tmp_path)
    try:
        r.apply(problem(), '0')
        with pytest.raises(ProtocolError):r.apply(problem(), 'again')
        action=experiment().model_copy(update={'evidence_refs':['hidden-confirmation']})
        with pytest.raises(ProtocolError):r.validate(action)
    finally:r.close()


def _revise(evidence, hid, *, predictions=None):
    return parse_action({'kind':'revise_hypothesis','hypothesis_id':hid,
        'mechanism':'A revised explanation requires a new intervention',
        'predictions':predictions or ['A new version must be tested'],
        'rationale':'Use the observed evidence',
        'evidence_refs':evidence['observation_refs']})


def test_revised_version_does_not_inherit_prior_support(tmp_path):
    r = make_runtime(tmp_path)
    try:
        r.apply(problem(), '0');r.apply(experiment(), '1')
        evidence=r.records('attempt_completed')[0]['data']
        hid=evidence['candidate']['hypothesis_id']
        without_observation=parse_action({'kind':'revise_hypothesis','hypothesis_id':hid,
            'mechanism':'Unsupported revision','predictions':['Untested'],
            'rationale':'Only a public card','evidence_refs':['dataset-card']})
        with pytest.raises(ProtocolError,match='development evidence'):r.validate(without_observation)
        r.apply(_revise(evidence, hid), '2')
        assert r.store.get('hypothesis',hid)['version']==2
        assert not any(j['hypothesis_version']==2 for j in r.store.list('judgment'))
        assert r.freeze_selection('revision')['finalist'] is None
        assert r.store.verify()['ok']
    finally:r.close()


def test_revision_rejected_when_experiment_cap_is_full(tmp_path):
    r = make_runtime(tmp_path)
    try:
        r.apply(problem(), '0')
        r.apply(experiment(), '1')
        evidence = r.records('attempt_completed')[0]['data']
        hid = evidence['candidate']['hypothesis_id']
        for index in range(2, 9):
            r.apply(experiment().model_copy(update={'parent_candidate_id': '1'}), str(index))
        assert len(r.records('attempt_started')) == 8
        with pytest.raises(ProtocolError, match='remaining experiment slot'):
            r.validate(_revise(evidence, hid))
    finally:
        r.close()


def test_revision_rejected_on_last_planning_step(tmp_path):
    r = make_runtime(tmp_path)
    try:
        r.apply(problem(), '0')
        r.apply(experiment(), '1')
        evidence = r.records('attempt_completed')[0]['data']
        hid = evidence['candidate']['hypothesis_id']
        for index in range(11):
            r.record('decision_completed', str(index), {'status': 'synthetic'})
        assert len(r.records('decision_completed')) == 11
        with pytest.raises(ProtocolError, match='remaining planning step'):
            r.validate(_revise(evidence, hid))
    finally:
        r.close()


def test_revision_rejected_without_success_on_current_version(tmp_path):
    r = make_runtime(tmp_path)
    try:
        r.apply(problem(), '0')
        r.apply(experiment(), '1')
        evidence = r.records('attempt_completed')[0]['data']
        hid = evidence['candidate']['hypothesis_id']
        r.apply(_revise(evidence, hid), '2')
        assert r.store.get('hypothesis', hid)['version'] == 2
        with pytest.raises(ProtocolError, match='successful test of the current explanation version'):
            r.validate(_revise(evidence, hid, predictions=['Another untested revision']))
    finally:
        r.close()


def test_workbench_projection_binds_v2_scientific_judgment(tmp_path):
    from popper.research.readout import read_research_state
    r=make_runtime(tmp_path)
    try:
        r.apply(problem(),'0');r.apply(experiment(),'1')
        selected=r.freeze_selection('test');r.confirm({r.study_id:digest(selected)})
        projection=read_research_state(r.root)
        assert projection['ok']
        assert projection['claims'][0]['scientific_status']=='supported'
        assert projection['experiment_graph']['integrity']['ok']
    finally:r.close()


def test_persisted_response_recovers_without_resending(tmp_path):
    r=make_runtime(tmp_path)
    r.record('request','0',{'context_sha256':digest(r.context())})
    r.record('response','0',{'payload':{'action':problem().model_dump()}})
    # Simulate interruption after applying a response, before marking its decision.
    r.apply(problem(),'0');r.close()
    r=make_runtime(tmp_path)
    class Client:
        def decide(self,*args,**kwargs):
            return {'action':{'kind':'stop_planning','rationale':'No further experiments'}}
    try:
        r.run(Client())
        assert len(r.records('problem'))==1
        assert [x['key'] for x in r.records('request')]==['0','1']
    finally:r.close()


def test_ambiguous_request_and_cancellation_never_resend(tmp_path):
    r=make_runtime(tmp_path)
    class Client:
        def decide(self,*args,**kwargs):raise AssertionError('must not send')
    try:
        r.record('request','0',{'context_sha256':digest(r.context())})
        with pytest.raises(ProtocolError,match='Ambiguous'):r.run(Client())
        r.cancel();r.run(Client())
        assert r.records('selection')[0]['data']['reason']=='cancelled'
    finally:r.close()


def test_completed_worker_recovers_accounting_without_reexecution(tmp_path,monkeypatch):
    r=make_runtime(tmp_path);r.apply(problem(),'0')
    original=r.record
    def interrupt(topic,key,data):
        if topic=='run_budget_settled':raise SystemExit('interrupted after worker receipt')
        return original(topic,key,data)
    monkeypatch.setattr(r,'record',interrupt)
    with pytest.raises(SystemExit):r.apply(experiment(),'1')
    receipt=r.worker.list_receipts()[0]
    r.close();r=make_runtime(tmp_path)
    try:
        r.apply(experiment(),'1')
        assert r.records('attempt_completed')[0]['data']['status']=='succeeded'
        assert next(x for x in r.worker.list_receipts() if x['job_id']==receipt['job_id'])==receipt
        assert r.store.budget(r.study_id).balance(r.study_id)['spent']==2.
        runs=len(r.store.list('run'));r.apply(experiment(),'1')
        assert len(r.store.list('run'))==runs
        with pytest.raises(ProtocolError):r.apply(experiment(BASELINE),'1')
        assert r.store.verify()['ok']
    finally:r.close()


def test_interrupted_confirmation_publishes_once(tmp_path,monkeypatch):
    r=make_runtime(tmp_path);r.apply(problem(),'0');r.apply(experiment(),'1')
    selection=r.freeze_selection('test');roster={r.study_id:digest(selection)}
    original=r.record
    def interrupt(topic,key,data):
        if topic=='confirmation_completed':raise SystemExit('publication interrupted')
        return original(topic,key,data)
    monkeypatch.setattr(r,'record',interrupt)
    with pytest.raises(SystemExit):r.confirm(roster)
    receipts=r.worker.list_receipts();assert not r.store.list('claim')
    r.close();r=make_runtime(tmp_path)
    try:
        assert r.confirm(roster)['status']=='confirmation_completed'
        assert r.worker.list_receipts()==receipts
        assert len(r.store.list('claim'))==1
        assert r.store.verify()['ok']
    finally:r.close()


def test_profiles_share_raw_information_without_leaking_structured_verdicts(tmp_path):
    r=make_runtime(tmp_path)
    try:
        r.apply(problem(),'0');r.apply(experiment(),'1')
        evidence=r.context();r.profile='generic_iteration';ordinary=r.context()
        assert evidence['raw_evidence']==ordinary['raw_evidence']
        assert evidence['attempts']==ordinary['attempts']
        assert 'structured_judgments' not in ordinary
        assert all('judgment' not in x['data'] for x in ordinary['attempts'])
        assert 'structured_judgments' in evidence
    finally:r.close()


def test_invalid_predictions_retain_independent_evaluator_diagnostic(tmp_path):
    r=make_runtime(tmp_path)
    try:
        r.apply(problem(),'0')
        r.apply(experiment(SOURCE.replace('PRED',"float('nan')")),'1')
        attempt=r.records('attempt_completed')[0]['data']
        assert attempt['status']=='failed'
        assert attempt['evaluation_diagnostic']['source']=='independent_evaluator'
        assert 'Prediction must be finite' in attempt['evaluation_diagnostic']['stderr']
        assert r.context()['attempts'][0]['data']['evaluation_diagnostic']==attempt['evaluation_diagnostic']
        assert r.store.budget(r.study_id).balance(r.study_id)['spent']==2.
    finally:r.close()


@pytest.mark.parametrize('target',['hypothesis_id','alternative_id'])
def test_revised_explanation_pair_requires_and_accepts_followup_experiment(tmp_path,target):
    r=make_runtime(tmp_path)
    try:
        r.apply(problem(),'0');r.apply(experiment(),'1')
        prior=r.records('attempt_completed')[0]['data']
        r.apply(parse_action({'kind':'revise_hypothesis','hypothesis_id':prior['candidate'][target],
            'mechanism':'Refined explanation after observing the intervention',
            'predictions':['Retest the registered improvement contrast on the new version'],
            'rationale':'Evidence triggers a new test','evidence_refs':prior['observation_refs']}),'2')
        assert all(r.store.get('hypothesis',prior['candidate'][k])['version']==2
                   for k in ('hypothesis_id','alternative_id'))
        r.apply(experiment().model_copy(update={'parent_candidate_id':'1'}),'3')
        followup=r.records('attempt_completed')[-1]['data']
        assert followup['status']=='succeeded'
        assert followup['candidate']['hypothesis_version']==2
        selection=r.freeze_selection('test')
        assert selection['finalist']['candidate']['hypothesis_version']==2
        assert r.confirm({r.study_id:digest(selection)})['judgment']=='supported'
        assert r.store.verify()['ok']
    finally:r.close()


def test_real_sdk_validates_native_store_on_its_owner_thread(tmp_path,planner_server,planner_budget):
    from popper.research.adaptive_runtime import AdaptivePlannerClient
    r=make_runtime(tmp_path)
    planner_server.scripts=[response(kind,{'action':action.model_dump(),'assessment':None}) for kind,action in [
        ('propose_problem',problem()),('run_experiment',experiment()),
        ('stop_planning',parse_action({'kind':'stop_planning','rationale':'Stop after the independent development evidence'}))]]
    client=AdaptivePlannerClient(planner_server.url,'local-model',api_key='synthetic-placeholder',
                                 request_budget=planner_budget)
    try:
        r.run(client)
        assert len(planner_server.bodies)==3
        assert r.records('attempt_completed')[0]['data']['status']=='succeeded'
        assert r.store.verify()['ok']
    finally:r.close()


def test_soft_contract_rejection_continues_and_freezes_finalist(tmp_path):
    r=make_runtime(tmp_path)
    class Client:
        def __init__(self):
            self.n=0
        def decide(self,*args,**kwargs):
            self.n+=1
            if self.n==1:
                return {'action':problem().model_dump()}
            if self.n==2:
                error=ProtocolError('模型输出缺少可执行源码')
                error.category='action_contract_rejected'
                error.details=[{'path':'action.source_code','rule':'missing'}]
                raise error
            if self.n==3:
                return {'action':experiment().model_dump()}
            return {'action':{'kind':'stop_planning','rationale':'Stop with a registered finalist'}}
    try:
        r.run(Client())
        rejected=[x for x in r.records('response') if x['data'].get('rejected')]
        assert len(rejected)==1
        assert rejected[0]['data']['details'][0]['rule']=='missing'
        assert rejected[0]['data']['audit']['consumed_send'] is True
        assert 'logical_request_id' in rejected[0]['data']['audit']
        assert r.records('attempt_completed')[0]['data']['status']=='succeeded'
        selection=r.records('selection')[0]['data']
        assert selection['finalist'] is not None
        assert selection.get('confirmation_preflight', {}).get('ok') is True
        assert r.store.verify()['ok']
    finally:r.close()


def test_empty_finalist_records_superseded_reason(tmp_path):
    r=make_runtime(tmp_path)
    r.apply(problem(), '0')
    r.apply(experiment(), '1')
    succeeded=r.records('attempt_completed')[0]['data']
    candidate=succeeded['candidate']
    # Bump the hypothesis version so freeze_selection drops the prior succeeded attempt.
    from popper.research.contracts import Hypothesis
    hyp=r.store.get('hypothesis', candidate['hypothesis_id'])
    r.store.revise_hypothesis(Hypothesis(
        hyp['hypothesis_id'], r.study_id, hyp['version'] + 1, hyp['version'],
        hyp['mechanism'], hyp['applicability'], tuple(hyp['predictions']),
        tuple(hyp['falsification']), tuple(hyp['alternatives'])))
    try:
        selection=r.freeze_selection('decision_limit')
        assert selection['finalist'] is None
        assert selection['empty_finalist_reason']=='successful_attempts_superseded_by_revision'
        assert selection.get('confirmation_preflight') is None
    finally:
        r.close()


def test_exhausted_logical_attempt_is_skipped_and_research_continues(tmp_path):
    from popper.request_lifecycle import RequestFailure
    r=make_runtime(tmp_path)
    class Client:
        def __init__(self):
            self.n=0
        def decide(self,*args,**kwargs):
            self.n+=1
            if self.n==1:
                return {'action':problem().model_dump()}
            if self.n==2:
                raise RequestFailure('attempt_budget_exhausted', usage={'total_tokens': 10}, sent=True,
                    transport_phase='generating', output_started=True, stream_completed=True)
            if self.n==3:
                return {'action':experiment().model_dump()}
            return {'action':{'kind':'stop_planning','rationale':'Stop after the skipped request'}}
    try:
        r.run(Client())
        assert any(x['data'].get('category')=='attempt_budget_exhausted' for x in r.records('response'))
        assert r.records('attempt_completed')[0]['data']['status']=='succeeded'
        assert r.records('selection')[0]['data']['finalist'] is not None
    finally:r.close()


def test_transport_stop_still_aborts_after_soft_progress(tmp_path):
    r=make_runtime(tmp_path)
    class Client:
        def __init__(self):
            self.n=0
        def decide(self,*args,**kwargs):
            self.n+=1
            if self.n==1:
                return {'action':problem().model_dump()}
            error=ProtocolError('stream ended early')
            error.category='stream_interrupted'
            raise error
    try:
        with pytest.raises(ProtocolError) as failure:
            r.run(Client())
        assert failure.value.category=='stream_interrupted'
        assert len(r.records('problem'))==1
        assert not r.records('selection')
    finally:r.close()
