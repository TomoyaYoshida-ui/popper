from popper.research.adaptive_tasks import prepare_task, contract_for, FIXED_CONFIGS
from popper.research.adaptive_benchmark import prepare, report
import pytest
from popper.core import ProtocolError,write_json,file_hash,digest,read_json


def test_generated_task_seals_holdout_and_freezes_preprocessing(tmp_path):
    prepared=prepare_task(tmp_path/'task','new',kind=1,n=200)
    contract=contract_for(prepared,17)
    assert contract['planning_protocol']=='research-runtime/v2'
    assert contract['model_seed']==17
    assert prepared['independence_verified'] is True
    assert prepared['sampling'] == {'design': 'iid_row', 'generator_version': 'adaptive-tasks/v1'}
    assert contract['sampling'] == prepared['sampling']
    assert 'seed' not in prepared['card'] and 'kind' not in prepared['card']['source'] or prepared['card']['source']['kind']=='generated'
    assert prepare_task(tmp_path/'task','new',kind=1,n=200)==prepared
    assert len(FIXED_CONFIGS)==8


def test_pilot_has_six_cells_and_report_preserves_all_planned_cells(tmp_path):
    cohort=prepare(tmp_path/'pilot',pilot=True)
    assert len(cohort['cells'])==6
    assert len(cohort['tasks'])==2
    result=report(tmp_path/'pilot',cohort)
    assert result['planned_cells']==6
    assert all(c['development_status']=='not_started' for c in result['cells'])
    assert all(row['planned']==2 for row in result['profiles'].values())


def test_report_cannot_shrink_frozen_denominator(tmp_path):
    root=tmp_path/'pilot';cohort=prepare(root,pilot=True)
    write_json(root/'confirmation-roster.json',{'cohort_sha256':file_hash(root/'cohort.json'),
               'bindings':[{'cell':c['id']} for c in cohort['cells']]})
    changed={**cohort,'cells':cohort['cells'][:-1]}
    write_json(root/'cohort.json',changed)
    with pytest.raises(ProtocolError,match='denominator'):report(root,changed)


def test_changed_kernel_cannot_open_confirmation(tmp_path,monkeypatch):
    from popper.research import adaptive_benchmark as benchmark
    cohort=prepare(tmp_path/'pilot',pilot=True)
    monkeypatch.setattr(benchmark,'source_snapshot',lambda:{})
    with pytest.raises(ProtocolError,match='Frozen implementation changed'):
        benchmark.confirm(tmp_path/'pilot',cohort)
    assert not (tmp_path/'pilot/confirmation-roster.json').exists()


def test_model_calls_are_serialized_across_cells():
    from popper.research.adaptive_benchmark import SerializedModelClient
    import threading
    import time
    state={'inside':0,'max':0}
    class Client:
        def decide(self,*_args,**_kwargs):
            state['inside']+=1
            state['max']=max(state['max'],state['inside'])
            time.sleep(0.05)
            state['inside']-=1
            return {'ok':True}
    client=SerializedModelClient(Client())
    threads=[threading.Thread(target=client.decide) for _ in range(4)]
    for thread in threads:thread.start()
    for thread in threads:thread.join()
    assert state['max']==1


def test_develop_marks_completed_when_finalist_survives_transport_stop(tmp_path,monkeypatch):
    from popper.research import adaptive_benchmark as benchmark
    from tests.test_adaptive_runtime_v2 import make_runtime, problem, experiment

    root=tmp_path/'cohort'
    cell={'id':'P01-generic_iteration-r0','profile':'generic_iteration','task_id':'P01','repeat':0}
    cohort={'source_snapshot':{'x':1},'base_url':'http://127.0.0.1','model':'local'}
    runtime=make_runtime(root/'cell-home')
    runtime.apply(problem(),'0')
    runtime.apply(experiment(),'1')

    class Client:
        def decide(self,*args,**kwargs):
            error=ProtocolError('provider stream ended')
            error.category='stream_interrupted'
            raise error

    monkeypatch.setattr(benchmark,'runtime',lambda *_args,**_kwargs:runtime)
    monkeypatch.setattr(benchmark,'require_gates',lambda *_args,**_kwargs:None)
    monkeypatch.setattr(benchmark,'FrozenClient',lambda client,_snapshot:client)
    monkeypatch.setattr(benchmark,'AdaptivePlannerClient',lambda *a,**k:Client())
    monkeypatch.setattr(benchmark,'ModelRequestBudget',lambda *a,**k:type('B',(),{'close':lambda self:None})())
    result=benchmark._develop_cell(root,cohort,cell)
    assert result['status']=='development_completed'
    assert result['error']['category']=='stream_interrupted'
    assert result['runtime']['selection'][0]['data']['finalist'] is not None
    runtime.close()


def test_campaign_caps_read_from_cohort():
    from popper.research.adaptive_benchmark import _campaign_caps, DEFAULT_GLOBAL_CALL_CAP, DEFAULT_GLOBAL_TOKEN_CAP
    assert _campaign_caps({}) == (DEFAULT_GLOBAL_TOKEN_CAP, DEFAULT_GLOBAL_CALL_CAP)
    assert _campaign_caps({'global_token_cap': 36_000_000, 'global_call_cap': 3200}) == (36_000_000, 3200)
    with pytest.raises(ProtocolError):
        _campaign_caps({'global_call_cap': 0, 'global_token_cap': 100})


def test_confirm_failure_preserves_implementation_category(tmp_path,monkeypatch):
    from popper.research import adaptive_benchmark as benchmark
    from tests.test_adaptive_runtime_v2 import make_runtime, problem, experiment

    root=tmp_path/'cohort'
    root.mkdir()
    cell={'id':'P01-evidence_driven-r0','profile':'evidence_driven','task_id':'P01','repeat':0}
    cohort={'source_snapshot':{'x':1},'tasks':[],'cells':[cell],'base_url':'http://127.0.0.1','model':'local'}
    write_json(root/'cohort.json',cohort)
    runtime=make_runtime(root/'cells'/cell['id'])
    runtime.apply(problem(),'0')
    runtime.apply(experiment(),'1')
    selection=runtime.freeze_selection('test')
    write_json(root/'cells'/cell['id']/'development-result.json',{
        'cell':cell['id'],'status':'development_completed','runtime':runtime.result()})
    write_json(root/'confirmation-roster.json',{
        'cohort_sha256':file_hash(root/'cohort.json'),
        'selections':{runtime.study_id:digest(selection)},
        'bindings':[{'cell':cell['id'],'result_sha256':file_hash(root/'cells'/cell['id']/'development-result.json'),
                     'study_id':runtime.study_id}]})

    class ExplodingRuntime:
        def __init__(self, inner):
            self.inner=inner
        def confirm(self, roster):
            error=ProtocolError('Candidate execution failed; retained worker receipt available')
            error.category='implementation_failed'
            error.details=[{'path':'worker','rule':'implementation_failed','stderr_tail':'JSONDecodeError'}]
            raise error
        def records(self, topic):
            return self.inner.records(topic)
        def close(self):
            self.inner.close()

    monkeypatch.setattr(benchmark,'source_snapshot',lambda: {'x':1})
    monkeypatch.setattr(benchmark,'runtime',lambda *_a,**_k: ExplodingRuntime(runtime))
    monkeypatch.setattr(benchmark.ExposureLedger,'reserve',lambda *a,**k: None)
    monkeypatch.setattr(benchmark.ExposureLedger,'open_confirmation',lambda *a,**k: None)
    monkeypatch.setattr(benchmark.ExposureLedger,'close',lambda *a,**k: None)
    benchmark.confirm(root,cohort)
    result=read_json(root/'cells'/cell['id']/'confirmation-result.json')
    assert result['status']=='failed'
    assert result['category']=='implementation_failed'
    assert result['details'][0]['rule']=='implementation_failed'
    assert 'JSONDecodeError' in result['details'][0]['stderr_tail']
