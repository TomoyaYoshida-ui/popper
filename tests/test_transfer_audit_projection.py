"""V2 reports cannot erase planner failures or invent assessment/evidence facts."""
import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from popper.core import ProtocolError, initialize, read_json, write_json
from popper.research.controller import ResearchController
from popper.research.runtime import ResearchRuntime
from evaluation.verify_transfer_pilot import (
    AuditReader, audit, failed_or_unfinished, runtime_report_projection,
    verify_runtime_report, verify_runtime_evidence)


def outcome(status='failed'):
    selection={'hypothesis_id':'H-final','hypothesis_version':2,'scope':'dev:slice:s9'}
    return {'planner':{'status':status,'reason':'fixture'},
        'development':{'status':'completed' if status=='stopped' else 'partial',
                       'executions':[{'hypothesis_id':'H-final','observation_id':'OBS-final'}]},
        'finalist':selection,'agent_assessments':[
            {'subject_hypothesis_id':'H-other','hypothesis_version':2,'scope_id':'dev:slice:s9','judgment':'supported'},
            {'subject_hypothesis_id':'H-final','hypothesis_version':1,'scope_id':'dev:slice:s9','judgment':'supported'},
            {'subject_hypothesis_id':'H-final','hypothesis_version':2,'scope_id':'dev:slice:s1','judgment':'supported'},
            {'subject_hypothesis_id':'H-final','hypothesis_version':2,'scope_id':'dev:slice:s9','judgment':'inconclusive'}]}


def report(authoritative):
    projected=runtime_report_projection(authoritative)
    return {'status':projected['status'],'executions':authoritative['development']['executions'],
            'final_agent_assessment':projected['agent_assessment']}


def test_confirmed_partial_evidence_keeps_failed_planner_in_denominator():
    projected=verify_runtime_report(report(outcome()),outcome())
    projected['confirmation_status']='completed'
    assert projected['development_status']=='partial'
    assert projected['confirmation_eligible']
    assert failed_or_unfinished(projected,modern=True)
    assert not failed_or_unfinished({'status':'development_completed','confirmation_status':'completed'})


@pytest.mark.parametrize('tamper', ['status','assessment','execution'])
def test_report_tamper_cannot_replace_durable_outcomes(tamper):
    trusted=outcome();changed=copy.deepcopy(report(trusted))
    if tamper=='status': changed['status']='development_completed'
    elif tamper=='assessment': changed['final_agent_assessment']='supported'
    else: changed['executions'][0]['observation_id']='OBS-unrelated'
    with pytest.raises(ProtocolError): verify_runtime_report(changed,trusted)


def test_final_assessment_is_bound_to_selected_subject_version_and_scope():
    projected=runtime_report_projection(outcome())
    assert projected['agent_assessment']=='inconclusive'
    other=outcome();other['agent_assessments'].pop()
    assert runtime_report_projection(other)['agent_assessment']=='not_evaluated'


def scoped_evidence():
    from types import SimpleNamespace
    trusted=outcome('stopped')
    selected=trusted['finalist']
    selected.update(observation_id='OBS-final',dev_value=.8,judgment_ids=['J-primary'],
        selection_metric='candidate_mae',config={'kind':'cart'},selection_receipt={'receipt_sha256':'unchanged'})
    observation={'hypothesis_id':'H-final','hypothesis_version':2,'scope':'dev:slice:s9','value':1.2}
    primary={'judgment_id':'J-primary','hypothesis_id':'H-final','hypothesis_version':2,
        'scope':'dev:slice:s9','observation_refs':['OBS-control','OBS-final'],
        'comparison_artifact':{'response_sha256':'primary'},'evidence':{'candidate_mae':.8},
        'new_judgment':'inconclusive'}
    source={**primary,'judgment_id':'J-source','scope':'dev:slice:s1',
        'observation_refs':['OBS-source-control','OBS-source'],
        'evidence':{'candidate_mae':.4},'new_judgment':'supported'}
    store=SimpleNamespace(get=lambda kind,key:observation if kind=='observation' else {'version':2},
                          list=lambda kind:[primary,source])
    rendered=report(trusted)
    rendered['executions']=[{'hypothesis_id':'H-final','observation_id':'OBS-final','dev_value':.8,
        'native_row_mae':1.2,'scope':'dev:slice:s9','selection_metric':'equal_analysis_unit_mean_mae',
        'selection_evidence_key':'candidate_mae','judgment_ids':['J-primary']}]
    rendered['finalist']={**selected,'selection_metric':'equal_analysis_unit_mean_mae',
                         'selection_evidence_key':'candidate_mae'}
    return trusted,rendered,store


def test_same_hypothesis_source_judgment_cannot_replace_primary_scope_with_matching_value():
    trusted,rendered,store=scoped_evidence()
    rendered['executions'][0].update(judgment_ids=['J-source'],dev_value=.4)
    rendered['finalist'].update(judgment_ids=['J-source'],dev_value=.4)
    # The signed selection identity and valid H/OBS IDs are left unchanged.
    verify_runtime_report(rendered,trusted)
    with pytest.raises(ProtocolError,match='primary evidence'):
        verify_runtime_evidence(rendered,trusted,store,'dev:slice:s9','candidate_mae')


def test_primary_scope_agreement_uses_verified_judgment_and_native_value():
    trusted,rendered,store=scoped_evidence()
    checked=verify_runtime_evidence(rendered,trusted,store,'dev:slice:s9','candidate_mae')
    assert checked['selected_development_judgment']=='inconclusive'
    rendered['executions'][0]['native_row_mae']=99
    with pytest.raises(ProtocolError,match='native_row_mae'):
        verify_runtime_evidence(rendered,trusted,store,'dev:slice:s9','candidate_mae')


def test_finalist_derived_fields_must_match_frozen_authority():
    trusted,rendered,store=scoped_evidence()
    rendered['finalist']['judgment_ids']=['J-source']
    with pytest.raises(ProtocolError,match='authoritative selection'):
        verify_runtime_evidence(rendered,trusted,store,'dev:slice:s9','candidate_mae')


def test_missing_runtime_facts_cannot_be_reported_as_completed_abstention():
    cells=[{'id':f'{task}-{arm}-{repeat}','task':task,'strategy':arm,'repeat':repeat}
           for task in ('T01','T02','T03') for arm in ('fixed_queue','generic_iteration','evidence_driven')
           for repeat in range(3)]
    root=Path('in-memory-audit-fixture').resolve()
    protocol={'schema':'transfer-pilot/v2','protocol_sha256':'fixture','cells':cells}
    selected=cells[0]['id']
    cell_report={'protocol_sha256':'fixture','status':'abstained','executions':[],
        'elapsed_seconds':0,'decisions':[],'finalist':None,'final_agent_assessment':'not_evaluated'}
    files={str(root/'protocol.json'):protocol,str(root/'cells'/selected/'result.json'):cell_report}
    with patch('evaluation.verify_transfer_pilot.verify_frozen_sources',return_value=False),\
         patch('evaluation.verify_transfer_pilot.validate_cohort'),\
         patch('evaluation.verify_transfer_pilot.read_json',side_effect=lambda p:files[str(p)]),\
         patch.object(Path,'exists',lambda p:str(p) in files),\
         patch.object(Path,'is_file',return_value=False),\
         patch('evaluation.verify_transfer_pilot.write_json'):
        with pytest.raises(ProtocolError,match='without durable research facts'):
            audit(root)
        cell_report['status']='failed'
        result=audit(root)
    first=result['cells'][0]
    assert first['status']=='incomplete'
    assert first['planner_status']=='not_started'
    group=next(g for g in result['descriptive_groups'] if g['task']=='T01' and g['strategy']=='fixed_queue')
    assert group['failed_or_unfinished']==3


def test_real_registered_polynomial_artifacts_are_readonly_on_audit(tmp_path):
    from popper.capabilities import extra_available
    if not extra_available('analysis'): pytest.skip('analysis extra required')
    from test_unit_comparison import polynomial_project
    project=tmp_path/'project';polynomial_project(project)
    spec=read_json(project/'experiment.json')
    spec['metadata']={'planning_protocol':'research-runtime/v1'}
    write_json(project/'experiment.json',spec)
    initialize(project);run=tmp_path/'research'
    ResearchController.initialize(project,run)
    c=ResearchController(run)
    try:
        runtime=c.planning_runtime(session_id='audit-fixture',max_experiments=1)
        trusted=runtime.run(trusted_local=True)
        rendered={'status':'development_completed','executions':trusted['development']['executions'],
                  'final_agent_assessment':'not_evaluated'}
        assert verify_runtime_report(rendered,trusted)['confirmation_eligible']
        key=runtime.adapter.describe()['selection_metric'];scope=runtime.adapter.primary_scope_id()
        rendered['executions']=[{**e,'dev_value':next(j['evidence'][key] for j in c.store.list('judgment')
            if j['hypothesis_id']==e['hypothesis_id'] and j['scope']==scope and e['observation_id'] in j['observation_refs']),
            'native_row_mae':c.store.get('observation',e['observation_id'])['value'],'scope':scope,
            'selection_metric':'equal_analysis_unit_mean_mae','selection_evidence_key':key,
            'judgment_ids':[j['judgment_id'] for j in c.store.list('judgment') if j['hypothesis_id']==e['hypothesis_id']
                and j['scope']==scope and e['observation_id'] in j['observation_refs']]}
            for e in trusted['development']['executions']]
        rendered['finalist']={**trusted['finalist'],'selection_metric':'equal_analysis_unit_mean_mae',
                             'selection_evidence_key':key}
        verify_runtime_evidence(rendered,trusted,c.store,scope,key)
    finally: c.close()
    before=(run/'research.sqlite').read_bytes()
    reader=AuditReader(run)
    try:
        restored=ResearchRuntime(reader,session_id='audit-fixture',max_experiments=1).result()
        assert restored['finalist']==trusted['finalist']
        assert verify_runtime_report(rendered,restored)['planner_status']=='stopped'
        verify_runtime_evidence(rendered,restored,reader.store,scope,key)
    finally: reader.close()
    assert before==(run/'research.sqlite').read_bytes()
