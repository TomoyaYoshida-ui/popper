"""Frozen FIGS Concrete runner implementing the native Popper prediction CLI."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import numpy as np
from feature_map import FEATURES

UPSTREAM = 'e8efb7e7b599e4d0f3e3399174e6684d125acb6f'


def tree(node):
    if node.left is None:
        return {'value':np.asarray(node.value).reshape(-1).tolist()}
    return {'feature':int(node.feature),'threshold':float(node.threshold),
            'left':tree(node.left),'right':tree(node.right)}


def main():
    parser=argparse.ArgumentParser()
    for name in ('train','input','output','config','seed'): parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    # The native worker intentionally removes the user's profile environment.
    # Keep matplotlib's import-time cache inside the allowed output workspace.
    os.environ['MPLCONFIGDIR'] = str(Path(args.output).resolve().parent/'matplotlib-cache')
    from imodels import FIGSRegressor
    direct=json.loads(importlib.metadata.distribution('imodels').read_text('direct_url.json') or '{}')
    if direct.get('vcs_info',{}).get('commit_id') != UPSTREAM:
        raise ValueError('imodels must be installed from the frozen upstream commit')
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    if config not in ({'model':'figs_single_12'},{'model':'figs_multi_12'}):
        raise ValueError('unregistered FIGS configuration')
    seed=int(args.seed)
    if seed != 17: raise ValueError('model seed must be frozen at 17')
    train=json.loads(Path(args.train).read_text(encoding='utf-8')); inputs=json.loads(Path(args.input).read_text(encoding='utf-8'))
    np.random.seed(seed)
    model=FIGSRegressor(max_rules=12,max_trees=1 if config['model']=='figs_single_12' else None,
                        random_state=seed,n_jobs=1)
    model.fit(np.asarray([FEATURES[int(r['x'])] for r in train]),np.asarray([r['y'] for r in train]))
    predictions=np.asarray(model.predict(np.asarray([FEATURES[int(r['x'])] for r in inputs]))).reshape(-1)
    output=Path(args.output)
    output.write_text(json.dumps([{'id':r['id'],'prediction':float(v)} for r,v in zip(inputs,predictions)],allow_nan=False),encoding='utf-8')
    audit={'upstream_commit':UPSTREAM,'seed':seed,'budget':12,'actual_rules':int(model.complexity_),
           'tree_count':len(model.trees_),'trees':[tree(t) for t in model.trees_]}
    (output.parent/'model-audit-17.json').write_text(json.dumps(audit,allow_nan=False),encoding='utf-8')


if __name__ == '__main__': main()
