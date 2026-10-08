"""Frozen 27-cell migration pilot. Paid calls require verified platform gates."""
from __future__ import annotations
import argparse
import random
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from popper.core import ProtocolError,canonical,digest,file_hash,initialize,read_json,write_json
from popper.model_budget import ModelRequestBudget
from popper.scoop import make_json_client
from popper.research.controller import ResearchController
from popper.research.runtime import ResearchRuntime
from popper.research.runtime_protocol import decision_instructions
from evaluation.transfer_tasks import prepare,ROOT,DATA_SEED

ARMS=('fixed_queue','generic_iteration','evidence_driven')
IMPLEMENTATION=('popper/core.py','popper/cli.py','popper/research/controller.py','popper/research/store.py',
 'popper/research/judgment.py','popper/research/paired_comparison.py','popper/request_lifecycle.py',
 'popper/model_budget.py','popper/scoop.py','evaluation/transfer_tasks.py',
 'evaluation/run_transfer_pilot.py','integrations/transfer_pilot/model.py',
 'evaluation/validate_transfer_environment.py','evaluation/verify_transfer_pilot.py','scripts/Dockerfile.transfer')
BASE='https://maas.qianwenaiapi.com/compatible-mode/v1'
MODEL='qwen3.8-flash'
SYSTEM=decision_instructions()


def snapshot():
    # Freeze the complete production package, including imported evaluators and services.
    files=set(IMPLEMENTATION)|{str(p.relative_to(ROOT)).replace('\\','/') for p in (ROOT/'popper').rglob('*.py')}
    return {p:file_hash(ROOT/p) for p in sorted(files)}


def prepare_cohort(out):
    out=Path(out).resolve(); out.mkdir(parents=True,exist_ok=True)
    target=out/'protocol.json'
    if target.exists():
        p=read_json(target); validate_cohort(p,out); return p
    tasks=[]
    for task in ('T01','T02','T03'):
        path=out/'tasks'/task
        info=prepare(task,path,out/'public-cache')
        files={p.name:file_hash(p) for p in path.iterdir() if p.is_file() and p.name!='oracle-private.json'}
        tasks.append({**info,'files':files})
    cells=[]
    for task in tasks:
        for repeat in range(3):
            order=list(range(8)); random.Random(DATA_SEED+repeat).shuffle(order)
            for arm in ARMS: cells.append({'id':f"{task['task']}-{arm}-r{repeat}",'task':task['task'],
                                          'strategy':arm,'repeat':repeat,'candidate_order':order})
    random.Random(DATA_SEED).shuffle(cells)
    p={'schema':'transfer-pilot/v2','tasks':tasks,'cells':cells,'implementation':snapshot(),
       'model':MODEL,'global_calls':180,'cell_calls':9,'preflight_calls':6,'global_tokens':5000000,
       'cell_tokens':250000,'preflight_tokens':100000,'candidate_attempts':4,
       'exposure':'project-held-out_public_or_fresh_generated','general_superiority_claimed':False,
       'repeats_are_independent_datasets':False,'model_seed':17,'data_seed':DATA_SEED}
    p['protocol_sha256']=digest(p); write_json(target,p); return p


def validate_cohort(p,out):
    if p.get('schema')!='transfer-pilot/v2' or len(p['cells'])!=27 or len({c['id'] for c in p['cells']})!=27:
        raise ProtocolError('冻结单元身份或数量不合法')
    if digest({k:v for k,v in p.items() if k!='protocol_sha256'})!=p['protocol_sha256'] or p['implementation']!=snapshot():
        raise ProtocolError('冻结协议或实现已变，必须重新冻结，不能混合运行')
    if (p['global_calls'],p['cell_calls'],p['preflight_calls'],p['candidate_attempts'])!=(180,9,6,4):
        raise ProtocolError('试点预算不一致')
    for task in p['tasks']:
        for name,sha in task['files'].items():
            if file_hash(Path(out)/'tasks'/task['task']/name)!=sha: raise ProtocolError('冻结任务制品已变')


def agent_context(c,cell,seen=None):
    """Compatibility projection; all facts and scope semantics come from runtime."""
    return ResearchRuntime.preview_context(c,profile=cell['strategy'])


def validate_decision(d,context):
    """Read-only parser for archived v1 responses; never used by v2 execution."""
    required={'action','hypothesis_id','rationale','evidence_refs','agent_assessment'}
    if not isinstance(d,dict) or not required<=d.keys() or set(d)-required-{'revision'}:
        raise ProtocolError('模型决策字段必须恰为冻结模式')
    if d['action'] not in {'experiment','revise','finish'} or d['agent_assessment'] not in {'supported','refuted','inconclusive','not_evaluated'}:
        raise ProtocolError('模型动作或未经验证判断不合法')
    if not isinstance(d['rationale'],str) or not 1<=len(d['rationale'])<=2000:
        raise ProtocolError('模型理由必须为简洁文本')
    known={r for e in context['raw_evidence'] for r in e['evidence_refs']}
    if not isinstance(d['evidence_refs'],list) or any(r not in known for r in d['evidence_refs']):
        raise ProtocolError('模型引用未知证据')
    candidates={r['hypothesis_id']:r for r in context['candidates']}
    if d['action']!='finish':
        if d['hypothesis_id'] not in candidates or candidates[d['hypothesis_id']]['executed']:
            raise ProtocolError('仅允许尚未执行的冻结候选')
    elif d['hypothesis_id'] is not None or d.get('revision') is not None:
        raise ProtocolError('停止不能带新候选或修订')
    if d['action']=='revise':
        revision=d.get('revision')
        if (not isinstance(revision,dict) or set(revision)!={'mechanism','applicability','predictions'}
                or not all(isinstance(revision[k],str) and revision[k].strip() for k in ('mechanism','applicability'))
                or not isinstance(revision['predictions'],list) or not revision['predictions']
                or any(not isinstance(x,str) or not x.strip() for x in revision['predictions']) or not d['evidence_refs']):
            raise ProtocolError('修订必须引用观察并给出完整版本文本')
    elif d.get('revision') is not None:
        raise ProtocolError('实验决策不能隐式修改假设')
    return d


def revise_unexecuted(c,hid,revision):
    """Compatibility call to the controller-owned revision/version transaction."""
    refs=[o['observation_id'] for o in c.store.list('observation')
          if o['scope']=='dev' or o['scope'].startswith('dev:slice:')]
    return c.revise_registered_hypothesis(hid,**revision,evidence_refs=refs,
        expected_version=c.store.get('study',c.manifest['study_id'])['state_version'])



def _report_execution(c,execution):
    """Project verified facts for the existing descriptive report vocabulary."""
    observation=c.store.get('observation',execution['observation_id'])
    judgments=[j for j in c.store.list('judgment')
        if j['hypothesis_id']==execution['hypothesis_id']
        and j['hypothesis_version']==observation['hypothesis_version']
        and j['scope']==observation['scope'] and observation['observation_id'] in j['observation_refs']]
    primary=next(j for j in judgments if j.get('evidence'))
    return {**execution,'dev_value':primary['evidence']['candidate_mae'],
            'native_row_mae':observation['value'],'scope':observation['scope'],
            'selection_metric':'equal_analysis_unit_mean_mae','selection_evidence_key':'candidate_mae',
            'judgment_ids':[j['judgment_id'] for j in judgments]}


def _report_decisions(runtime):
    # These labels are a reporting compatibility projection, never planner input.
    decisions=[]
    for record in runtime.records('action_completed'):
        body=record['body']; action=body['action']; kind=action['kind']
        projected={'action':{'execute_candidate':'experiment','revise_hypothesis':'revise',
                             'stop_planning':'finish'}[kind],
            'hypothesis_id':action.get('candidate_id',action.get('hypothesis_id')),
            'rationale':action['rationale'],'evidence_refs':action['evidence_refs'],
            'agent_assessment':body['assessment']['judgment'] if body.get('assessment') else 'not_evaluated',
            'runtime_action':action}
        if kind=='revise_hypothesis':
            projected['revision']={k:action[k] for k in ('mechanism','applicability','predictions')}
        decisions.append(projected)
    return decisions


def run_cell(out,p,cell,*,offline=False):
    root=Path(out)/'cells'/cell['id']; result_path=root/'result.json'
    root.mkdir(parents=True,exist_ok=True); project=root/'project'; run=root/'research'
    task=next(t for t in p['tasks'] if t['task']==cell['task'])
    c=None; runtime=None; budget=None; started=time.monotonic()
    result={**cell,'schema':'transfer-cell/v2','protocol_sha256':p['protocol_sha256'],
            'status':'running','decisions':[],'executions':[],'finalist':None,
            'model_called':False,'offline_fixture':offline}
    try:
        if result_path.exists():
            previous=read_json(result_path)
            if (previous.get('schema')!='transfer-cell/v2'
                    or previous['protocol_sha256']!=p['protocol_sha256']
                    or previous['offline_fixture']!=offline):
                raise ProtocolError('Legacy or conflicting cell report cannot be resumed with this runtime')
        # A historical JSON marker cannot be interpreted as a new durable request.
        if list(root.glob('step-*.json')):
            raise ProtocolError('Legacy JSON step checkpoint requires audit; it is not a runtime receipt')
        checkpoint=root/'checkpoint.json'
        if checkpoint.exists() and read_json(checkpoint).get('schema')!='transfer-report-checkpoint/v2':
            raise ProtocolError('Legacy JSON checkpoint cannot authorize a new runtime session')
        if not project.exists():
            project.mkdir()
            for name in task['files']: shutil_copy(Path(out)/'tasks'/cell['task']/name,project/name)
            initialize(project)
        ResearchController.initialize(project,run,budget_cap=7)
        c=ResearchController(run)
        runtime=c.planning_runtime(session_id='transfer:'+cell['id'],profile=cell['strategy'],
                                   max_experiments=4,max_decisions=9)
        call=None
        if cell['strategy']!='fixed_queue' and not offline:
            budget=ModelRequestBudget(Path(out)/'model-calls.sqlite',global_tokens=5000000,global_calls=180,
                scope=cell['id'],scope_tokens=250000,scope_calls=9,unknown_usage_policy='reserve')
            call=make_json_client(BASE,MODEL,root/'diagnostics',request_budget=budget)
        def progress(event):
            print(canonical({'cell_id':cell['id'],**event}),flush=True)
        candidate_order=[c.manifest['candidate_hypothesis_ids'][i] for i in cell['candidate_order']]
        outcome=runtime.run(call,candidate_order=candidate_order,trusted_local=True,on_progress=progress)
        result['runtime']=outcome
        result['executions']=[_report_execution(c,e) for e in outcome['development']['executions']]
        result['decisions']=_report_decisions(runtime)
        result['model_called']=bool(runtime.records('request_started'))
        result['finalist']=outcome['finalist']
        if result['finalist']:
            result['finalist']={**result['finalist'],
                'selection_metric':'equal_analysis_unit_mean_mae','selection_evidence_key':'candidate_mae'}
        relevant=[a for a in outcome['agent_assessments']
            if result['finalist'] and a['subject_hypothesis_id']==result['finalist']['hypothesis_id']
            and a['hypothesis_version']==result['finalist']['hypothesis_version']
            and a['scope_id']==result['finalist']['scope']]
        result['final_agent_assessment']=relevant[-1]['judgment'] if relevant else 'not_evaluated'
        result['agent_assessments']=outcome['agent_assessments']
        result['agent_assessment_trust']='unverified'
        planner=outcome['planner']; reason=planner['reason']
        if planner['status']=='stopped':
            result['status']='development_completed' if result['finalist'] else 'abstained'
        else:
            result['status']='incomplete' if planner['status'] in {'exhausted','recovery_blocked','cancelled'} else 'failed'
            result['failure_category']=({'decision_limit':'request_budget_exhausted',
                'request_started_without_durable_response':'request_recovery_blocked'}).get(reason,reason)
        result['integrity']=outcome['integrity']
    except Exception as error:
        result.update(status='failed',error_type=type(error).__name__,
            failure_category=getattr(error,'category',None) or 'execution_or_contract_failure')
    finally:
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        if budget is not None: result['usage']=budget.snapshot(); budget.close()
        if c is not None: c.close()
        # Reports may be regenerated; only the existing ResearchStore receipts
        # authorize resumption, execution and candidate selection.
        write_json(result_path,result)
        write_json(root/'checkpoint.json',{'schema':'transfer-report-checkpoint/v2',
            'protocol_sha256':p['protocol_sha256'],'status':result['status'],
            'recovery_authority':'ResearchStore runtime_record and original execution receipts'})
    return result


def shutil_copy(source,target):
    import shutil
    target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(source,target)


def confirm_cell(out,p,cell,roster):
    root=Path(out)/'cells'/cell['id']; path=root/'confirmation.json'
    selected=roster['finalists'][cell['id']]; roster_sha=digest(roster)
    if roster.get('protocol_sha256')!=p['protocol_sha256']:
        raise ProtocolError('Confirmation roster is not bound to the frozen cohort')
    if 'cells' in p:
        expected={item['id'] for item in p['cells']}
        frozen=Path(out)/'finalists.json'
        if set(roster['finalists'])!=expected or not frozen.is_file() or read_json(frozen)!=roster:
            raise ProtocolError('Every cohort selection must be frozen before confirmation opens')
    if path.exists() and read_json(path).get('roster_sha256')!=roster_sha:
        raise ProtocolError('Confirmation receipt roster changed')
    if selected is None:
        receipt={'schema':'transfer-confirmation/v2','status':'abstained','cell_id':cell['id'],
                 'protocol_sha256':p['protocol_sha256'],'roster_sha256':roster_sha}
        write_json(path,receipt); return receipt
    marker=root/'confirmation-consumed.json'
    marker_body={'schema':'transfer-roster-marker/v2','cell_id':cell['id'],
        'roster_sha256':roster_sha,'selection_sha256':digest(selected),
        'authority':'Roster identity only; confirmation consumption lives in the original kernel'}
    if marker.exists() and read_json(marker)!=marker_body:
        raise ProtocolError('Frozen confirmation roster marker changed')
    if not marker.exists(): write_json(marker,marker_body)
    c=ResearchController(root/'research')
    try:
        selection=selected['selection_receipt']
        status=c.confirm_registered_candidate(selected['hypothesis_id'],
            expected_version=selection['state_version'],selection_receipt=selection,trusted_local=True)
        judgments=[j for j in status['judgments'] if j['scope']=='confirmation'
                   and j['hypothesis_version']==selected['hypothesis_version']]
        receipt={'schema':'transfer-confirmation/v2','status':'completed','cell_id':cell['id'],
            'protocol_sha256':p['protocol_sha256'],'roster_sha256':roster_sha,
            'selection_sha256':digest(selected),'confirmation_consumed_once':True,
            'judgments':judgments,'integrity':status['integrity']}
        write_json(path,receipt); return receipt
    except Exception as error:
        # This receipt is only a report. The controller can recover saved core
        # results after a scoring interruption, and refuses consumed execution.
        state=c.exp.state()
        consumed=bool(state.get('test_consumed'))
        write_json(path,{'schema':'transfer-confirmation/v2','status':'failed','cell_id':cell['id'],
            'protocol_sha256':p['protocol_sha256'],'roster_sha256':roster_sha,
            'error_type':type(error).__name__,'confirmation_consumed_once':consumed,
            'budget_accounted':2. if consumed else 0.,
            'failure_category':'confirmation_execution_or_evaluation'})
        raise
    finally: c.close()


def require_environment_gates(out,p):
    for name in ('windows','linux','linux-sandbox','docker'):
        path=Path(out)/'environment'/(name+'.json')
        if not path.is_file(): raise ProtocolError('付费评测环境门禁未完成: '+name)
        gate=read_json(path)
        if (gate.get('schema')!='transfer-environment-gate/v1' or gate.get('name')!=name
            or gate.get('status')!='passed' or gate.get('implementation')!=p['implementation']
            or gate.get('returncode')!=0 or not gate.get('artifacts') or not gate.get('source_unchanged')):
            raise ProtocolError('环境门禁未通过或不是冻结实现: '+name)
        for artifact in gate.get('artifacts',[]):
            if file_hash(Path(artifact['path']))!=artifact['sha256']: raise ProtocolError('门禁证据制品已变')


def preflight(out,p):
    path=Path(out)/'preflight.json'
    if path.exists():
        receipt=read_json(path)
        if receipt.get('status')!='passed' or receipt.get('protocol_sha256')!=p['protocol_sha256']:
            raise ProtocolError('模型预检未通过，不能重跑以冲淡失败')
        return receipt
    marker=Path(out)/'preflight-started.json'
    if marker.exists(): raise ProtocolError('模型预检曾中断，禁止重复发送')
    write_json(marker,{'protocol_sha256':p['protocol_sha256']})
    budget=ModelRequestBudget(Path(out)/'model-calls.sqlite',global_tokens=5000000,global_calls=180,
        scope='preflight',scope_tokens=100000,scope_calls=6,unknown_usage_policy='reserve')
    try:
        call=make_json_client(BASE,MODEL,Path(out)/'preflight-diagnostics',request_budget=budget)
        infrastructure={'connection_failure','connect_timeout','read_timeout','total_timeout','temporary_service','stream_interrupted'}
        for probe in range(3):
            try:
                response=call('Return exactly one JSON object {"ready":true}.',{'check':'JSON streaming and final usage','probe':probe},
                              logical_request_id=f'transfer-preflight-v2:{probe}')
                if response!={'ready':True}: raise ProtocolError('预检 JSON 模式不符合请求')
                receipt={'status':'passed','protocol_sha256':p['protocol_sha256'],'usage':budget.snapshot()}
                write_json(path,receipt); return receipt
            except Exception as error:
                if getattr(error,'category',None) not in infrastructure or probe==2: raise
    except Exception as error:
        write_json(path,{'status':'failed','protocol_sha256':p['protocol_sha256'],'error_type':type(error).__name__,
                         'failure_category':getattr(error,'category','preflight_failure'),'usage':budget.snapshot()})
        raise
    finally: budget.close()


def run(out,*,offline=False,limit=None):
    out=Path(out).resolve(); p=prepare_cohort(out)
    if not offline:
        require_environment_gates(out,p)
        preflight(out,p)
    cells=p['cells'] if limit is None else p['cells'][:limit]
    results=[]; streak=0
    for cell in cells:
        result=run_cell(out,p,cell,offline=offline); results.append(result)
        streak=streak+1 if result['status']=='failed' and result.get('failure_category') in {'connection_failure','connect_timeout','read_timeout','total_timeout','temporary_service','stream_interrupted'} else 0
        write_json(out/'development-results.json',{'cells':results,'all_selections_frozen':False})
        if result.get('failure_category')=='authentication_or_permission' or streak>=3: break
    if len(results)!=27:
        report={'status':'incomplete','offline_fixture':offline,'planned':27,'finished_development':len(results),'cells':results,
                'confirmations_started':False,'general_superiority_claimed':False}
        write_json(out/'report.json',report); return report
    roster={'schema':'transfer-finalists/v2','protocol_sha256':p['protocol_sha256'],
            'finalists':{r['id']:r['finalist'] for r in results},
            'agent_assessments':{r['id']:r.get('final_agent_assessment','not_evaluated') for r in results}}
    roster_path=out/'finalists.json'
    if roster_path.exists() and read_json(roster_path)!=roster: raise ProtocolError('不能更改已冻结确认名单')
    if not roster_path.exists(): write_json(roster_path,roster)
    confirmations=[]
    for cell in p['cells']:
        try: confirmations.append(confirm_cell(out,p,cell,roster))
        except Exception as error: confirmations.append({'cell_id':cell['id'],'status':'failed','error_type':type(error).__name__})
    report={'schema':'transfer-report/v2','status':'completed_with_failures' if any(c['status']=='failed' for c in confirmations) or any(r['status'] in {'failed','incomplete'} for r in results) else 'completed',
            'planned':27,'cells':results,'confirmations':confirmations,'roster_sha256':digest(roster),'offline_fixture':offline,
            'model_arms_evaluated':not offline,'general_superiority_claimed':False,'repeats_are_independent_datasets':False}
    write_json(out/'report.json',report); return report


def main():
    p=argparse.ArgumentParser(); p.add_argument('command',choices=('prepare','offline','run','status'))
    p.add_argument('--out',type=Path,required=True); p.add_argument('--limit',type=int)
    args=p.parse_args()
    if args.command=='prepare': result=prepare_cohort(args.out)
    elif args.command in {'offline','run'}: result=run(args.out,offline=args.command=='offline',limit=args.limit)
    else: result=read_json(args.out/'report.json') if (args.out/'report.json').exists() else {'status':'not_started'}
    print(canonical({k:result.get(k) for k in ('status','planned','finished_development','model_arms_evaluated')}))


if __name__=='__main__': main()
