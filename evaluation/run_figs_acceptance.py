"""Native FIGS lifecycle acceptance; optional model calls share a durable 30-call cap."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from popper.core import initialize, write_json
from popper.model_budget import ModelRequestBudget
from popper.research.controller import ResearchController
from popper.research.models import DeepSeekResearchPolicy
from scripts.prepare_figs_concrete import prepare

BASE='https://maas.qianwenaiapi.com/compatible-mode/v1'
MODEL='qwen3.8-flash'


def run(output, *, csv=None, live=False):
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    project, research=output/'project',output/'research'
    budget=None; controller=None
    try:
        if live:
            if not os.environ.get('POPPER_API_KEY'):
                raise RuntimeError('POPPER_API_KEY is unavailable')
            budget=ModelRequestBudget(output.parent/'figs-model-budget.sqlite',global_tokens=2_000_000,global_calls=30,
                                      scope='figs-acceptance:'+output.name,scope_tokens=2_000_000,scope_calls=30)
            policy=DeepSeekResearchPolicy(BASE,MODEL,output/'diagnostics',request_budget=budget)
        else:
            policy=None
        if not project.exists(): prepare(project,csv)
        if not (project/'.popper/state.db').exists(): initialize(project)
        ResearchController.initialize(project,research,policy=policy)
        controller=ResearchController(research,policy=policy)
        result=controller.run(trusted_local=True,auto_confirm=True)
        judgments=result['judgments']
        final=[j for j in judgments if j['scope']=='confirmation']
        summary={'schema':'native-figs-acceptance/v1','status':'completed',
                 'live':live,'model':MODEL if live else None,'phase':result['phase'],
                 'integrity':result['integrity'],'judgments':[
                     {k:j.get(k) for k in ('judgment_id','hypothesis_id','hypothesis_version','old_judgment',
                       'new_judgment','scope','reason','next_action','observation_refs','evidence','mechanism_proven')}
                     for j in judgments],
                 'final_scientific_judgments':[j['new_judgment'] for j in final],
                 'decision_actions':[d['action'] for d in result['decisions']],
                 'legacy_threshold_statuses':[c['status'] for c in result['claims']],
                 'legacy_threshold_is_scientific_support':False,
                 'fresh_confirmation_evidence':False,
                 'scope':'Migration reproduction of a previously evaluated public benchmark; not a new independent scientific confirmation',
                 'confirmation_observations':len([o for o in result['observations'] if o['scope']=='confirmation']),
                 'request_budget':budget.snapshot() if budget else None}
    except Exception as error:
        summary={'schema':'native-figs-acceptance/v1','status':'failed','live':live,
                 'error_type':type(error).__name__,'failure_category':getattr(error,'category','execution_or_contract_failure'),
                 'error':str(error).replace(os.environ.get('POPPER_API_KEY') or '\x00','[REDACTED]')[:500],
                 'request_budget':budget.snapshot() if budget else None}
    finally:
        if controller is not None: controller.close()
        if budget is not None: budget.close()
    write_json(output/'summary.json',summary)
    print(json.dumps({k:summary.get(k) for k in ('status','phase','final_scientific_judgments','failure_category','error')},ensure_ascii=True))
    return summary


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--csv',type=Path); parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    result=run(args.output,csv=args.csv,live=args.live)
    return 0 if result['status']=='completed' else 1


if __name__ == '__main__': raise SystemExit(main())
