"""Frozen, independently reproducible paired-unit comparisons beyond FIGS Concrete."""
from __future__ import annotations
import argparse
import math
import subprocess
import sys
from pathlib import Path
from ..core import ProtocolError, digest, file_hash, read_json, write_json
from .grouped_comparison import _observation_identity, _tree_predict

SCHEMA = 'paired-unit-ci/v1'
LIBRARIES={'numpy':'2.2.6','scikit-learn':'1.7.2','pandas':'2.3.3'}


def validate_protocol(p):
    required = {'schema','metric','effect','unit','confidence','bootstrap_samples','bootstrap_seed',
                'minimum_units','model_seed','features','groups','configurations',
                'hypothesis_predictions','hypothesis_contrasts','primary_dev_prefix'}
    if not isinstance(p,dict) or not required <= p.keys() or p['schema'] != SCHEMA:
        raise ProtocolError('配对比较协议字段或版本不合法')
    if (p['metric'] != {'name':'mae','direction':'min'} or p['effect']!='relative_mae_improvement'
            or p['unit']!='ratio' or p['confidence']!=.95 or p['bootstrap_samples']!=10000
            or p['minimum_units']!=30 or p['model_seed']!=17 or type(p['bootstrap_seed']) is not int):
        raise ProtocolError('未实现的配对比较统计规则')
    if not isinstance(p['primary_dev_prefix'],str):
        raise ProtocolError('开发范围必须预注册')
    for key,spec in p['hypothesis_predictions'].items():
        op,threshold=spec.get('operator'),spec.get('threshold')
        if op not in {'gt','gte','lt','between'}:
            raise ProtocolError('预测运算符不合法')
        values=threshold if op=='between' else [threshold]
        if (not isinstance(values,list) or len(values)!=(2 if op=='between' else 1)
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in values)
                or (op=='between' and values[0]>=values[1])):
            raise ProtocolError('预测阈值不合法')
    return p


def _prediction_rows(obs, protocol):
    artifact=Path(obs['artifact_id'])
    if file_hash(artifact)!=obs['artifact_sha256']:
        raise ProtocolError('评估制品已变')
    request=read_json(artifact.parent/'request.json'); receipt=read_json(artifact)
    if (receipt['request_sha256']!=digest(request) or receipt['service_sha256']!=obs['evaluator_service_hash']
            or request['evaluator_id']!='mae-v1' or request['expected_seeds']!=[protocol['model_seed']]):
        raise ProtocolError('观察与独立评估请求身份不一致')
    if file_hash(Path(request['dataset']))!=request['dataset_sha256']:
        raise ProtocolError('配对数据已变')
    entry=request['predictions'][0]; path=Path(entry['path'])
    if file_hash(path)!=entry['sha256']:
        raise ProtocolError('预测制品已变')
    rows=read_json(request['dataset']); predictions=read_json(path)
    values={r['id']:r['prediction'] for r in predictions}
    if len(values)!=len(predictions) or set(values)!={r['id'] for r in rows}:
        raise ProtocolError('预测覆盖不一致')
    audit_path=path.parent/'model-audit-17.json'; audit=read_json(audit_path)
    config=protocol['configurations'].get(audit.get('config_sha256'))
    if config is None or audit['seed']!=protocol['model_seed'] or audit['selected_features']!=config['features']:
        raise ProtocolError('模型制品配置未注册')
    if audit['kind']!=config['kind']:
        raise ProtocolError('模型类型与冻结配置不一致')
    raw_config={k:config[k] for k in ('kind','features','max_rules','min_samples_leaf')}
    if digest(raw_config)!=audit['config_sha256'] or audit.get('libraries')!=LIBRARIES:
        raise ProtocolError('模型配置摘要或依赖版本与冻结规则不一致')
    if config['kind'].startswith('figs') and audit.get('figs_commit')!='e8efb7e7b599e4d0f3e3399174e6684d125acb6f':
        raise ProtocolError('FIGS 依赖提交不一致')
    trees=audit['trees']
    def count(t):
        return 0 if 'value' in t else 1+count(t['left'])+count(t['right'])
    rules=sum(count(t) for t in trees)
    if rules!=audit['actual_rules'] or rules>config['max_rules'] or len(trees)!=audit['tree_count']:
        raise ProtocolError('模型规则预算无法重算')
    if config['kind'] in {'cart','figs_single'} and len(trees)!=1:
        raise ProtocolError('注册的单树模型不能产生多树')
    if config['kind']=='zero' and (rules!=0 or trees!=[{'value':[0.]}]):
        raise ProtocolError('阴性基线必须为冻结零预测')
    for row in rows:
        x=protocol['features'][str(int(row['x']))]
        selected=[x[i] for i in audit['selected_features']]
        recovered=sum(_tree_predict(t,selected) for t in trees)
        if not math.isclose(values[row['id']],recovered,abs_tol=1e-6,rel_tol=1e-7):
            raise ProtocolError('模型序列化与实际预测不一致')
    return rows,values,request['dataset_sha256'],file_hash(audit_path),audit['config_sha256']


def evaluate(packet):
    import numpy as np
    source=Path(packet['protocol_path'])
    if file_hash(source)!=packet['protocol_sha256']:
        raise ProtocolError('冻结比较协议已变')
    p=validate_protocol(read_json(source)); observations=packet['observations']
    if len(observations)!=2 or observations[0]['scope']!=observations[1]['scope']:
        raise ProtocolError('比较必须为同范围的两项观察')
    decoded=[_prediction_rows(o,p) for o in observations]
    if decoded[0][2]!=decoded[1][2]:
        raise ProtocolError('必须使用同一配对数据')
    candidate=observations[1]['hypothesis_id']
    config=p['configurations'][decoded[1][4]]
    if config['hypothesis_id']!=candidate:
        raise ProtocolError('候选制品未绑定假设')
    baseline_config=p['configurations'][decoded[0][4]]
    if baseline_config.get('is_baseline') is not True:
        raise ProtocolError('比较对照不是冻结基线')
    prefix=(p.get('analysis_scopes',{}).get(observations[1]['scope'],p['primary_dev_prefix'])
            if observations[1]['scope'].startswith('dev') else '')
    grouped=[]
    for rows,values,*_ in decoded:
        errors={}
        for row in rows:
            if prefix and not row['id'].startswith(prefix): continue
            unit=p['groups'][str(int(row['x']))]
            errors.setdefault(unit,[]).append(abs(values[row['id']]-row['y']))
        grouped.append({u:float(np.mean(v)) for u,v in errors.items()})
    if not grouped[0] or set(grouped[0])!=set(grouped[1]):
        raise ProtocolError('配对分析单位缺失或不一致')
    units=sorted(grouped[0]); a=np.array([grouped[0][u] for u in units]); b=np.array([grouped[1][u] for u in units])
    stats=paired_statistics(a,b,p['bootstrap_seed'])
    alternate=p['hypothesis_contrasts'][candidate]['alternative_id']
    return {'schema':SCHEMA,'request_sha256':digest(packet),'service_sha256':file_hash(Path(__file__)),
        'metric':p['effect'],'unit':p['unit'],**stats,
        'analysis_prefix':prefix,'model_audit_sha256':[d[3] for d in decoded],
        'hypothesis_predictions':{h:p['hypothesis_predictions'][h] for h in (candidate,alternate)},
        'mechanism_proven':False,'trust':'separate_process_same_account'}


def paired_statistics(baseline,candidate,seed):
    import numpy as np
    a,b=np.asarray(baseline,dtype=float),np.asarray(candidate,dtype=float)
    if a.ndim!=1 or a.shape!=b.shape or len(a)==0 or not np.isfinite(a).all() or not np.isfinite(b).all() or np.any(a<0) or np.any(b<0):
        raise ProtocolError('配对单位误差不合法')
    if a.mean()<=1e-12: raise ProtocolError('相对 MAE 分母为零')
    idx=np.random.default_rng(seed).integers(0,len(a),size=(10000,len(a)))
    aa,bb=a[idx].mean(axis=1),b[idx].mean(axis=1)
    if np.any(aa<=1e-12): raise ProtocolError('重采样分母为零')
    return {'mean_effect':float((a.mean()-b.mean())/a.mean()),'ci95':np.quantile((aa-bb)/aa,[.025,.975]).tolist(),
            'quality':'valid' if len(a)>=30 else 'insufficient','n_units':len(a),
            'baseline_mae':float(a.mean()),'candidate_mae':float(b.mean())}


def verify_artifact(descriptor, observations):
    request,response=Path(descriptor['request']),Path(descriptor['response'])
    if file_hash(request)!=descriptor['request_sha256'] or file_hash(response)!=descriptor['response_sha256']:
        raise ProtocolError('配对制品摘要变化')
    packet=read_json(request)
    if packet['observations']!=[_observation_identity(o) for o in observations]:
        raise ProtocolError('配对制品引用了其他观察')
    result=read_json(response)
    if evaluate(packet)!=result: raise ProtocolError('配对比较无法重算')
    return result


def compare_observations(project, run_dir, baseline, candidate):
    source=Path(project)/'judgment-protocol.json'
    packet={'protocol_path':str(source),'protocol_sha256':file_hash(source),
            'observations':[_observation_identity(o) for o in (baseline,candidate)]}
    root=Path(run_dir)/'comparisons'/digest(packet)[:24]; root.mkdir(parents=True,exist_ok=True)
    request,response=root/'request.json',root/'response.json'
    if request.exists() and read_json(request)!=packet: raise ProtocolError('比较请求身份冲突')
    if not request.exists(): write_json(request,packet)
    if not response.exists():
        completed=subprocess.run([sys.executable,'-m','popper.research.paired_comparison','--request',str(request),'--response',str(response)],
            cwd=Path(__file__).resolve().parents[2],capture_output=True,timeout=120,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if completed.returncode:
            write_json(root/'failure.json',{'returncode':completed.returncode,'error_type':'comparison_process_failed'})
            raise ProtocolError('独立配对比较失败')
    descriptor={'comparison_schema':SCHEMA,'request':str(request),'response':str(response),
                'request_sha256':file_hash(request),'response_sha256':file_hash(response)}
    verify_artifact(descriptor,(baseline,candidate)); return descriptor


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--request',required=True,type=Path); parser.add_argument('--response',required=True,type=Path)
    args=parser.parse_args(); write_json(args.response,evaluate(read_json(args.request)))
