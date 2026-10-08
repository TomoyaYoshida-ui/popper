"""Separate-process paired recipe-group FIGS evaluation and reproducible verification."""
from __future__ import annotations
import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from ..core import ProtocolError, digest, file_hash, read_json, write_json


def _observation_identity(observation):
    return {k:v for k,v in observation.items() if k != 'status'}


def _tree_predict(tree, features):
    while 'value' not in tree:
        tree = tree['left'] if features[tree['feature']] <= tree['threshold'] else tree['right']
    return tree['value'][0]


def evaluate(packet):
    import numpy as np
    protocol_path = Path(packet['protocol_path'])
    if file_hash(protocol_path) != packet['protocol_sha256']:
        raise ProtocolError('分组比较预注册文件已改变')
    protocol = read_json(protocol_path)
    if (protocol['schema'] != 'figs-grouped-ci/v1' or protocol['bootstrap_seed'] != 20261008
            or protocol['bootstrap_samples'] != 10000 or protocol['minimum_groups'] != 30
            or protocol['prediction'] != {'operator': 'gt', 'threshold': .05}):
        raise ProtocolError('FIGS 预注册统计规则不合法')
    grouped = []
    audit_hashes = []
    valid_complexity = True
    dataset_identity = None
    for index, obs in enumerate(packet['observations']):
        artifact = Path(obs['artifact_id'])
        if file_hash(artifact) != obs['artifact_sha256']:
            raise ProtocolError('独立评估制品摘要变化')
        request = read_json(artifact.parent/'request.json')
        receipt = read_json(artifact)
        if receipt['request_sha256'] != digest(request) or receipt['service_sha256'] != obs['evaluator_service_hash']:
            raise ProtocolError('独立评估请求身份不一致')
        rows = read_json(request['dataset'])
        if file_hash(Path(request['dataset'])) != request['dataset_sha256']:
            raise ProtocolError('冻结评估数据摘要变化')
        if dataset_identity is not None and dataset_identity != request['dataset_sha256']:
            raise ProtocolError('比较必须使用同一配对样本')
        dataset_identity = request['dataset_sha256']
        if request['expected_seeds'] != [17]:
            raise ProtocolError('FIGS 固定模型种子必须为 17')
        prediction = request['predictions'][0]
        path = Path(prediction['path'])
        if file_hash(path) != prediction['sha256']:
            raise ProtocolError('预测制品摘要变化')
        predicted = {row['id']:row['prediction'] for row in read_json(path)}
        if set(predicted) != {row['id'] for row in rows}:
            raise ProtocolError('比较预测覆盖不一致')
        audit = read_json(path.parent/'model-audit-17.json')
        audit_hashes.append(file_hash(path.parent/'model-audit-17.json'))
        if audit['upstream_commit'] != protocol['upstream_commit'] or audit['seed'] != 17:
            raise ProtocolError('FIGS 依赖或模型种子不一致')
        trees = audit['trees']
        def rules(tree):
            return 0 if 'value' in tree else 1+rules(tree['left'])+rules(tree['right'])
        count = sum(rules(tree) for tree in trees)
        if count != audit['actual_rules'] or len(trees) != audit['tree_count']:
            raise ProtocolError('模型复杂度制品无法重算')
        if index == 0 and len(trees) != 1:
            raise ProtocolError('冻结基线必须为单树')
        valid_complexity &= count == audit['budget'] == 12
        errors = {}
        for row in rows:
            identity = str(int(row['x']))
            features = protocol['features'][identity]
            recovered = sum(_tree_predict(tree, features) for tree in trees)
            if not math.isclose(recovered, predicted[row['id']], abs_tol=1e-6, rel_tol=1e-7):
                raise ProtocolError('树序列化与实际预测不一致')
            group = protocol['groups'][identity]
            errors.setdefault(group, []).append(abs(predicted[row['id']]-row['y']))
        grouped.append({group:float(np.mean(values)) for group,values in errors.items()})
    if set(grouped[0]) != set(grouped[1]):
        raise ProtocolError('分组比较不配对')
    groups=sorted(grouped[0]); a=np.array([grouped[0][g] for g in groups]); b=np.array([grouped[1][g] for g in groups])
    if a.mean() <= 1e-12:
        raise ProtocolError('相对误差分母为零')
    ids=np.random.default_rng(20261008).integers(0,len(a),size=(10000,len(a)))
    aa,bb=a[ids].mean(axis=1),b[ids].mean(axis=1)
    if np.any(aa <= 1e-12):
        raise ProtocolError('重采样分母为零')
    quality='valid' if valid_complexity and len(a)>=30 else 'insufficient'
    return {'schema':'grouped-comparison/v1','request_sha256':digest(packet),
            'service_sha256':file_hash(Path(__file__)), 'metric':'relative_mae_improvement','unit':'ratio',
            'prediction':protocol['prediction'], 'hypothesis_predictions':protocol['hypothesis_predictions'],
            'mean_effect':float((a.mean()-b.mean())/a.mean()),
            'ci95':np.quantile((aa-bb)/aa,[.025,.975]).tolist(), 'quality':quality,
            'n_groups':len(a),'baseline_group_mae':float(a.mean()),'candidate_group_mae':float(b.mean()),
            'actual_rule_budgets_match':bool(valid_complexity), 'mechanism_proven':False,
            'model_audit_sha256':audit_hashes,
            'trust':'separate_process_same_account'}


def verify_artifact(descriptor, observations):
    request=Path(descriptor['request']); response=Path(descriptor['response'])
    if file_hash(request) != descriptor['request_sha256'] or file_hash(response) != descriptor['response_sha256']:
        raise ProtocolError('判断比较制品摘要变化')
    packet=read_json(request)
    if packet['observations'] != [_observation_identity(o) for o in observations]:
        raise ProtocolError('判断比较未绑定引用观察')
    recorded=read_json(response)
    if evaluate(packet) != recorded:
        raise ProtocolError('判断比较无法重算')
    return recorded


def compare_observations(project, run_dir, baseline, candidate):
    protocol=Path(project)/'judgment-protocol.json'
    packet={'protocol_path':str(protocol),'protocol_sha256':file_hash(protocol),
            'observations':[_observation_identity(o) for o in (baseline,candidate)]}
    target=Path(run_dir)/'comparisons'/digest(packet)[:24]; target.mkdir(parents=True,exist_ok=True)
    request,response=target/'request.json',target/'response.json'
    if request.exists() and read_json(request) != packet:
        raise ProtocolError('不能覆盖不同的比较请求')
    if not request.exists(): write_json(request,packet)
    if not response.exists():
        result=subprocess.run([sys.executable,'-m','popper.research.grouped_comparison','--request',str(request),'--response',str(response)],
                              cwd=Path(__file__).resolve().parents[2],capture_output=True,timeout=120,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise ProtocolError('分组独立比较失败；详情留在本地评估请求')
    descriptor={'request':str(request),'response':str(response),
                'request_sha256':file_hash(request),'response_sha256':file_hash(response)}
    verify_artifact(descriptor,(baseline,candidate))
    return descriptor


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--request',type=Path,required=True); parser.add_argument('--response',type=Path,required=True)
    args=parser.parse_args(); write_json(args.response,evaluate(read_json(args.request)))


if __name__ == '__main__': main()
