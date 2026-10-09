"""Recompute pilot integrity and descriptive outcomes; never infer superiority from repeats."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from popper.core import Experiment, ProtocolError,digest,read_json,write_json,file_hash
from popper.research.store import ResearchStore
from evaluation.run_transfer_pilot import ARMS,validate_cohort,snapshot


class AuditReader:
    """Read-only facts; never reconcile jobs, budgets or execution on audit."""
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.manifest = read_json(self.run_dir/'research.json')
        self.project = Path(self.manifest['project'])
        self.store = ResearchStore(self.run_dir/'research.sqlite', readonly=True)
        self.exp = Experiment(self.project, readonly=True)

    def _verify_inputs(self):
        if not self.store.verify()['ok']:
            raise ProtocolError('Research audit integrity failed')
        study = self.store.get('study', self.manifest['study_id'])
        if json.loads(study['scope'])['manifest_sha256'] != file_hash(self.run_dir/'research.json'):
            raise ProtocolError('Frozen research manifest changed')
        state = self.exp.verify_inputs()
        if self.manifest['input_identity'] != {'input_hashes': state['input_hashes'],
                                               'evaluator_hash': state['evaluator_hash']}:
            raise ProtocolError('Frozen research inputs changed')
        return state

    def close(self):
        self.exp.close()
        self.store.close()


def verify_frozen_sources(protocol,allow_verifier_upgrade=False):
    actual=snapshot(); expected=protocol['implementation']
    changes={p for p in set(actual)|set(expected) if actual.get(p)!=expected.get(p)}
    if changes and (not allow_verifier_upgrade or changes!={'evaluation/verify_transfer_pilot.py'}):
        raise ProtocolError('冻结的实验、决策或评估实现已变；核验器升级仅允许修改核验器本身')
    return bool(changes)


def runtime_report_projection(authoritative):
    """Derive application outcomes only from durable planning and evidence facts."""
    planner=authoritative['planner']['status']
    executions=authoritative['development']['executions']
    selected=authoritative['finalist']
    development=('completed' if planner=='stopped' else 'partial') if executions else 'not_completed'
    if authoritative['development']['status']!=development:
        raise ProtocolError('Durable development outcome contradicts its evaluated facts')
    if planner=='stopped': status='development_completed' if selected else 'abstained'
    elif planner in {'active','exhausted','recovery_blocked','cancelled'}: status='incomplete'
    elif planner=='failed': status='failed'
    else: raise ProtocolError('Unknown durable planner outcome')
    relevant=[a for a in authoritative['agent_assessments']
        if selected and a['subject_hypothesis_id']==selected['hypothesis_id']
        and a['hypothesis_version']==selected['hypothesis_version']
        and a['scope_id']==selected['scope']]
    return {'status':status,'planner_status':planner,'development_status':development,
            'confirmation_eligible':bool(selected),
            'agent_assessment':relevant[-1]['judgment'] if relevant else 'not_evaluated',
            'execution_ids':[(e['hypothesis_id'],e['observation_id']) for e in executions]}


def verify_runtime_report(report,authoritative):
    projection=runtime_report_projection(authoritative)
    if report['status']!=projection['status']:
        raise ProtocolError('Cell report status differs from durable planner and development outcomes')
    if report.get('final_agent_assessment','not_evaluated')!=projection['agent_assessment']:
        raise ProtocolError('Agent assessment report differs from its durable subject and evidence scope')
    reported=[(e['hypothesis_id'],e['observation_id']) for e in report['executions']]
    if reported!=projection['execution_ids']:
        raise ProtocolError('Execution report identities differ from durable independent development facts')
    return projection


def verify_runtime_evidence(report,authoritative,store,primary_scope,metric_key):
    """Bind report values and judgments to each authoritative primary observation."""
    checked=[]; selected_judgment=None
    for reported,execution in zip(report['executions'],authoritative['development']['executions']):
        hid,oid=execution['hypothesis_id'],execution['observation_id']
        observation=store.get('observation',oid)
        hypothesis=store.get('hypothesis',hid)
        if (not observation or not hypothesis or observation['hypothesis_id']!=hid
                or observation['hypothesis_version']!=hypothesis['version']
                or observation['scope']!=primary_scope):
            raise ProtocolError('Primary development observation identity or scope changed')
        judgments=[j for j in store.list('judgment')
            if j['hypothesis_id']==hid and j['hypothesis_version']==hypothesis['version']
            and j['scope']==primary_scope and oid in j['observation_refs']
            and j.get('comparison_artifact') and j.get('evidence')]
        if not judgments:
            raise ProtocolError('Primary development comparison judgments are missing')
        values=[j['evidence'].get(metric_key) for j in judgments]
        if (any(type(value) not in (int,float) or not math.isfinite(value) for value in values)
                or len(set(values))!=1):
            raise ProtocolError('Primary development selection metric is not independently consistent')
        expected={**execution,'dev_value':values[0],'native_row_mae':observation['value'],
            'scope':primary_scope,'selection_metric':'equal_analysis_unit_mean_mae',
            'selection_evidence_key':metric_key,'judgment_ids':[j['judgment_id'] for j in judgments]}
        for key,value in expected.items():
            actual=reported.get(key)
            valid=(type(actual) in (int,float) and math.isfinite(actual)
                   and math.isclose(actual,value,rel_tol=1e-12,abs_tol=1e-12)) if key in {'dev_value','native_row_mae'} else actual==value
            if not valid:
                raise ProtocolError('Development report field is not bound to primary evidence: '+key)
        checked.append(expected)
        selected=authoritative['finalist']
        if selected and selected['hypothesis_id']==hid and selected['observation_id']==oid:
            selected_judgment=judgments[-1]['new_judgment']
    selected=authoritative['finalist']
    if selected:
        expected={**selected,'selection_metric':'equal_analysis_unit_mean_mae',
                  'selection_evidence_key':metric_key}
        for key,value in expected.items():
            actual=report['finalist'].get(key)
            valid=(type(actual) in (int,float) and math.isfinite(actual)
                   and math.isclose(actual,value,rel_tol=1e-12,abs_tol=1e-12)) if key=='dev_value' else actual==value
            if not valid:
                raise ProtocolError('Finalist report field differs from the frozen authoritative selection: '+key)
        if selected_judgment is None:
            raise ProtocolError('Frozen finalist lacks matching primary development judgments')
    return {'executions':checked,'selected_development_judgment':selected_judgment}


def failed_or_unfinished(record,modern=False):
    planning=(record.get('planner_status')!='stopped' if modern else
              record['status'] not in {'development_completed','abstained'})
    return planning or record['confirmation_status'] not in {'completed','abstained'}


def audit(out,*,allow_verifier_upgrade=False):
    out=Path(out).resolve(); protocol=read_json(out/'protocol.json')
    upgraded=verify_frozen_sources(protocol,allow_verifier_upgrade)
    if not upgraded: validate_cohort(protocol,out)
    else:
        if (protocol.get('schema') not in {'transfer-pilot/v1','transfer-pilot/v2'} or len(protocol['cells'])!=27
            or len({c['id'] for c in protocol['cells']})!=27
            or digest({k:v for k,v in protocol.items() if k!='protocol_sha256'})!=protocol['protocol_sha256']
            or (protocol['global_calls'],protocol['cell_calls'],protocol['preflight_calls'],protocol['candidate_attempts'])!=(180,9,6,4)):
            raise ProtocolError('历史协议身份、数量或预算不合法')
        for task in protocol['tasks']:
            for name,sha in task['files'].items():
                if file_hash(out/'tasks'/task['task']/name)!=sha: raise ProtocolError('冻结数据或模型制品已变')
    roster=read_json(out/'finalists.json') if (out/'finalists.json').exists() else None
    groups=[]; cells=[]
    for cell in protocol['cells']:
        root=out/'cells'/cell['id']; r=read_json(root/'result.json') if (root/'result.json').exists() else None
        record={**cell,'status':r['status'] if r else 'not_started','confirmation_status':'not_started',
                'scientific_judgment':'not_evaluated','mean_effect':None,'ci95':None,'false_support':False,
                'agent_assessment':'not_evaluated','agent_evidence_agreement':None,'agent_development_agreement':None,
                'experiments':0,'seconds':0.,'stop_actions':0,'revision_actions':0,'scope_changes':[]}
        if r:
            if r['protocol_sha256']!=protocol['protocol_sha256']: raise ProtocolError('单元协议不一致')
            if len(r['executions'])>4: raise ProtocolError('开发实验超过冻结额度')
            record.update(experiments=len(r['executions']),seconds=r['elapsed_seconds'],
                agent_assessment=r.get('final_agent_assessment','not_evaluated'))
            record.update(stop_actions=sum(d['action']=='finish' for d in r['decisions']),
                revision_actions=sum(d['action']=='revise' for d in r['decisions']),
                scope_changes=[d['revision']['applicability'] for d in r['decisions'] if d['action']=='revise'])
            if protocol['schema']=='transfer-pilot/v2' and (
                    not (root/'research'/'research.json').is_file()
                    or not (root/'research'/'research.sqlite').is_file()):
                if (r['status'] not in {'failed','incomplete','not_started'} or r['executions']
                        or r['finalist'] is not None
                        or r.get('final_agent_assessment','not_evaluated')!='not_evaluated'):
                    raise ProtocolError('A cell without durable research facts cannot report completed abstention or evidence')
                record.update(status='not_started' if r['status']=='not_started' else 'incomplete',
                    planner_status='not_started',development_status='not_completed',
                    confirmation_eligible=False,agent_assessment='not_evaluated',experiments=0)
                cells.append(record)
                continue
            c=AuditReader(root/'research')
            try:
                integrity=c.store.verify()
                if not integrity['ok']: raise ProtocolError('研究存储证据不能重算: '+cell['id'])
                if protocol['schema']=='transfer-pilot/v2':
                    from popper.research.runtime import ResearchRuntime
                    runtime = ResearchRuntime(c,session_id='transfer:'+cell['id'],profile=cell['strategy'],
                                              max_experiments=4,max_decisions=9)
                    authoritative = runtime.result()
                    projection=verify_runtime_report(r,authoritative)
                    record.update({k:v for k,v in projection.items() if k!='execution_ids'})
                    if r.get('runtime',{}).get('planner') != authoritative['planner']:
                        raise ProtocolError('Planner report differs from durable receipts')
                    selected = authoritative['finalist']
                    if bool(selected) != bool(r['finalist']) or (selected and
                            selected['selection_receipt'] != r['finalist'].get('selection_receipt')):
                        raise ProtocolError('Selection report differs from durable receipts')
                    checked=verify_runtime_evidence(r,authoritative,c.store,runtime.adapter.primary_scope_id(),
                        runtime.adapter.describe()['selection_metric'])
                    if record['agent_assessment']!='not_evaluated' and checked['selected_development_judgment'] is not None:
                        record['agent_development_agreement']=(record['agent_assessment']==checked['selected_development_judgment'])
                else:
                    for execution in r['executions']:
                        primary=next(c.store.get('judgment',jid) for jid in execution['judgment_ids']
                            if c.store.get('judgment',jid)['hypothesis_id']==execution['hypothesis_id'])
                        if (execution.get('selection_metric')!='equal_analysis_unit_mean_mae'
                            or not math.isclose(execution['dev_value'],primary['evidence']['candidate_mae'],rel_tol=1e-12,abs_tol=1e-12)):
                            raise ProtocolError('选择指标不是独立评估的冻结单位 MAE')
                    if r['finalist']:
                        best=min(r['executions'],key=lambda x:(x['dev_value'],x['hypothesis_id']))
                        if best['hypothesis_id']!=r['finalist']['hypothesis_id']: raise ProtocolError('最终候选不是冻结开发目标的最优值')
                        judgments=[j for j in c.store.list('judgment') if j['judgment_id'] in r['finalist']['judgment_ids'] and j['hypothesis_id']==r['finalist']['hypothesis_id']]
                        if record['agent_assessment']!='not_evaluated' and judgments:
                            record['agent_development_agreement']=record['agent_assessment']==judgments[-1]['new_judgment']
                path=root/'confirmation.json'
                if path.exists():
                    receipt=read_json(path); record['confirmation_status']=receipt['status']
                    if roster is None or receipt['roster_sha256']!=digest(roster): raise ProtocolError('确认未绑定冻结名单')
                    if roster['finalists'][cell['id']]!=r['finalist']: raise ProtocolError('确认后修改候选')
                    if receipt['status']=='completed':
                        hid=r['finalist']['hypothesis_id']
                        primary=next(j for j in receipt['judgments'] if j['hypothesis_id']==hid)
                        stored=c.store.get('judgment',primary['judgment_id'])
                        if primary!=stored or primary['scope']!='confirmation': raise ProtocolError('确认判断不是独立存储的事实')
                        evidence=primary['evidence']
                        record.update(scientific_judgment=primary['new_judgment'],mean_effect=evidence['mean_effect'],ci95=evidence['ci95'])
                        if record['agent_assessment']!='not_evaluated':
                            record['agent_evidence_agreement']=record['agent_assessment']==record['scientific_judgment']
            finally: c.close()
            record['false_support']=cell['task']=='T03' and record['agent_assessment']=='supported'
        cells.append(record)
    for task in ('T01','T02','T03'):
        for arm in ARMS:
            rows=[r for r in cells if r['task']==task and r['strategy']==arm]
            valid=[r for r in rows if r['confirmation_status']=='completed']
            assessed=[r for r in valid if r['agent_evidence_agreement'] is not None]
            groups.append({'task':task,'strategy':arm,'denominator':3,
                'planner_completed':sum(r.get('planner_status')=='stopped' for r in rows),
                'development_completed':sum((r.get('development_status')=='completed' if protocol['schema']=='transfer-pilot/v2'
                    else r['status']=='development_completed') for r in rows),
                **({'confirmation_eligible':sum(r.get('confirmation_eligible',False) for r in rows)}
                   if protocol['schema']=='transfer-pilot/v2' else {}),
                'confirmation_completed':len(valid),'failed_or_unfinished':sum(failed_or_unfinished(r,protocol['schema']=='transfer-pilot/v2') for r in rows),
                'unverified_false_support':sum(r['false_support'] for r in rows),
                'scientific_supported':sum(r['scientific_judgment']=='supported' for r in valid),
                'scientific_refuted':sum(r['scientific_judgment']=='refuted' for r in valid),
                'scientific_inconclusive':sum(r['scientific_judgment']=='inconclusive' for r in valid),
                'agent_evidence_agreement_count':sum(r['agent_evidence_agreement'] is True for r in assessed),
                'agent_evidence_assessed_count':len(assessed),
                'agent_development_agreement_count':sum(r['agent_development_agreement'] is True for r in rows),
                'agent_development_assessed_count':sum(r['agent_development_agreement'] is not None for r in rows),
                'stop_actions':sum(r['stop_actions'] for r in rows),'revision_actions':sum(r['revision_actions'] for r in rows),
                'development_experiments':sum(r['experiments'] for r in rows),
                'elapsed_seconds':round(sum(r['seconds'] for r in rows),3),
                'confirmation_effects':[r['mean_effect'] for r in valid]})
    usage={'calls':0,'known_tokens':0,'unresolved_reserved_tokens':0,'accounted_tokens':0}
    if (out/'model-calls.sqlite').exists():
        from popper.model_budget import ModelRequestBudget
        budget=ModelRequestBudget.readonly(out/'model-calls.sqlite',scope='preflight')
        try: usage=budget.snapshot()['global']
        finally: budget.close()
        if usage['calls']>180 or usage['accounted_tokens']>5000000: raise ProtocolError('真实请求超过冻结全局预算')
    result={'schema':'transfer-audit/v1','protocol_sha256':protocol['protocol_sha256'],'planned':27,
        'verifier_upgrade':upgraded,'verifier_sha256':file_hash(Path(__file__)),
        'cells':cells,'descriptive_groups':groups,'usage':usage,
        'general_superiority_claimed':False,'repeats_are_independent_datasets':False,
        'offline_fixture':not (out/'model-calls.sqlite').exists(),
        'limitations':['One frozen dataset per task; repeated runs are not independent datasets.',
                      'Scientific support concerns preregistered prediction regions, not causal mechanisms.',
                      'T01 is public and may have appeared in model training.']}
    write_json(out/'audit.json',result); return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--allow-verifier-upgrade',action='store_true',help='仅允许只读核验器自身的版本变化，实验/决策/计分源码必须完全一致')
    args=parser.parse_args(); result=audit(args.out,allow_verifier_upgrade=args.allow_verifier_upgrade)
    print(json.dumps({'planned':result['planned'],'usage':result['usage'],'offline_fixture':result['offline_fixture']}))
