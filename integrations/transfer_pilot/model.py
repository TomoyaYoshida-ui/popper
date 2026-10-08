"""Frozen registered tree regressors; no agent-authored code or target access."""
import argparse
import importlib.metadata
import json
import os
import hashlib
from pathlib import Path
import numpy as np
from feature_map import FEATURES


def encode_figs(node):
    if node.left is None: return {'value':np.asarray(node.value).reshape(-1).tolist()}
    return {'feature':int(node.feature),'threshold':float(node.threshold),
            'left':encode_figs(node.left),'right':encode_figs(node.right)}


def encode_cart(model,i=0):
    t=model.tree_
    if t.children_left[i]==-1: return {'value':t.value[i].reshape(-1).tolist()}
    return {'feature':int(t.feature[i]),'threshold':float(t.threshold[i]),
            'left':encode_cart(model,t.children_left[i]),'right':encode_cart(model,t.children_right[i])}


def main():
    p=argparse.ArgumentParser()
    for key in ('train','input','output','config','seed'): p.add_argument('--'+key,required=True)
    args=p.parse_args(); output=Path(args.output)
    os.environ['MPLCONFIGDIR']=str(output.resolve().parent/'matplotlib-cache')
    config=json.loads(Path(args.config).read_text(encoding='utf-8')); seed=int(args.seed)
    if seed!=17 or set(config)!={'kind','features','max_rules','min_samples_leaf'}:
        raise ValueError('Unregistered model contract')
    selected=config['features']; train=json.loads(Path(args.train).read_text(encoding='utf-8'))
    inputs=json.loads(Path(args.input).read_text(encoding='utf-8'))
    tx=np.asarray([[FEATURES[int(r['x'])][i] for i in selected] for r in train]); ty=np.asarray([r['y'] for r in train])
    ix=np.asarray([[FEATURES[int(r['x'])][i] for i in selected] for r in inputs])
    np.random.seed(seed)
    if config['kind']=='zero':
        predictions=np.zeros(len(inputs)); trees=[{'value':[0.]}]; rules=0
    elif config['kind']=='cart':
        from sklearn.tree import DecisionTreeRegressor
        model=DecisionTreeRegressor(max_leaf_nodes=config['max_rules']+1,
            min_samples_leaf=config['min_samples_leaf'],random_state=seed)
        model.fit(tx,ty); predictions=model.predict(ix); trees=[encode_cart(model)]; rules=int(model.tree_.node_count-model.tree_.n_leaves)
    elif config['kind'] in {'figs_single','figs_multi'}:
        from imodels import FIGSRegressor
        direct=json.loads(importlib.metadata.distribution('imodels').read_text('direct_url.json') or '{}')
        if direct.get('vcs_info',{}).get('commit_id')!='e8efb7e7b599e4d0f3e3399174e6684d125acb6f':
            raise ValueError('FIGS upstream commit mismatch')
        model=FIGSRegressor(max_rules=config['max_rules'],max_trees=1 if config['kind']=='figs_single' else None,
                            random_state=seed,n_jobs=1)
        model.fit(tx,ty); predictions=model.predict(ix); trees=[encode_figs(t) for t in model.trees_]; rules=int(model.complexity_)
    else: raise ValueError('Unknown registered learner')
    output.write_text(json.dumps([{'id':r['id'],'prediction':float(v)} for r,v in zip(inputs,predictions)],allow_nan=False),encoding='utf-8')
    config_hash=hashlib.sha256(json.dumps(config,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()
    audit={'kind':config['kind'],'config_sha256':config_hash,'seed':seed,'selected_features':selected,
           'actual_rules':rules,'tree_count':len(trees),'trees':trees}
    (output.parent/'model-audit-17.json').write_text(json.dumps(audit,allow_nan=False),encoding='utf-8')


if __name__=='__main__': main()
