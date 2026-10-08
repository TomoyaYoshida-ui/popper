"""Recompute pilot integrity and descriptive outcomes; never infer superiority from repeats."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from popper.core import ProtocolError,digest,read_json,write_json
from popper.research.controller import ResearchController
from evaluation.run_transfer_pilot import ARMS,validate_cohort


def audit(out):
    out=Path(out).resolve(); protocol=read_json(out/'protocol.json'); validate_cohort(protocol,out)
    roster=read_json(out/'finalists.json') if (out/'finalists.json').exists() else None
    groups=[]; cells=[]
    for cell in protocol['cells']:
        root=out/'cells'/cell['id']; r=read_json(root/'result.json') if (root/'result.json').exists() else None
        record={**cell,'status':r['status'] if r else 'not_started','confirmation_status':'not_started',
                'scientific_judgment':'not_evaluated','mean_effect':None,'ci95':None,'false_support':False,
                'agent_assessment':'not_evaluated','agent_evidence_agreement':None,'experiments':0,'seconds':0.}
        if r:
            if r['protocol_sha256']!=protocol['protocol_sha256']: raise ProtocolError('单元协议不一致')
            if len(r['executions'])>4: raise ProtocolError('开发实验超过冻结额度')
            record.update(experiments=len(r['executions']),seconds=r['elapsed_seconds'],
                agent_assessment=r.get('final_agent_assessment','not_evaluated'))
            c=ResearchController(root/'research')
            try:
                integrity=c.store.verify()
                if not integrity['ok']: raise ProtocolError('研究存储证据不能重算: '+cell['id'])
                if r['finalist']:
                    best=min(r['executions'],key=lambda x:(x['dev_value'],x['hypothesis_id']))
                    if best['hypothesis_id']!=r['finalist']['hypothesis_id']: raise ProtocolError('最终候选不是冻结开发目标的最优值')
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
                'development_completed':sum(r['status']=='development_completed' for r in rows),
                'confirmation_completed':len(valid),'failed_or_unfinished':sum(r['status'] not in {'development_completed','abstained'} or r['confirmation_status'] not in {'completed','abstained'} for r in rows),
                'unverified_false_support':sum(r['false_support'] for r in rows),
                'scientific_supported':sum(r['scientific_judgment']=='supported' for r in valid),
                'scientific_refuted':sum(r['scientific_judgment']=='refuted' for r in valid),
                'scientific_inconclusive':sum(r['scientific_judgment']=='inconclusive' for r in valid),
                'agent_evidence_agreement_count':sum(r['agent_evidence_agreement'] is True for r in assessed),
                'agent_evidence_assessed_count':len(assessed),
                'development_experiments':sum(r['experiments'] for r in rows),
                'elapsed_seconds':round(sum(r['seconds'] for r in rows),3),
                'confirmation_effects':[r['mean_effect'] for r in valid]})
    usage={'calls':0,'known_tokens':0,'unresolved_reserved_tokens':0,'accounted_tokens':0}
    if (out/'model-calls.sqlite').exists():
        from popper.model_budget import ModelRequestBudget
        budget=ModelRequestBudget(out/'model-calls.sqlite',global_tokens=5000000,global_calls=180,
            scope='preflight',scope_tokens=100000,scope_calls=6)
        try: usage=budget.snapshot()['global']
        finally: budget.close()
        if usage['calls']>180 or usage['accounted_tokens']>5000000: raise ProtocolError('真实请求超过冻结全局预算')
    result={'schema':'transfer-audit/v1','protocol_sha256':protocol['protocol_sha256'],'planned':27,
        'cells':cells,'descriptive_groups':groups,'usage':usage,
        'general_superiority_claimed':False,'repeats_are_independent_datasets':False,
        'offline_fixture':not (out/'model-calls.sqlite').exists(),
        'limitations':['One frozen dataset per task; repeated runs are not independent datasets.',
                      'Scientific support concerns preregistered prediction regions, not causal mechanisms.',
                      'T01 is public and may have appeared in model training.']}
    write_json(out/'audit.json',result); return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--out',type=Path,required=True); args=parser.parse_args()
    result=audit(args.out)
    print(json.dumps({'planned':result['planned'],'usage':result['usage'],'offline_fixture':result['offline_fixture']}))
