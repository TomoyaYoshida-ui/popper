"""Prepare a native FIGS project from a pinned public CSV, keeping data local."""
import argparse
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from popper.core import digest, write_json

UPSTREAM='e8efb7e7b599e4d0f3e3399174e6684d125acb6f'
RAW_SHA='8d4b15b6fc68cd932d745cbd663d5ceae66dd54422e99c1e4865f2936ab7e2af'
URL='https://archive.ics.uci.edu/static/public/165/data.csv'


def prepare(output, csv=None):
    import numpy as np
    import pandas as pd
    output=Path(output).resolve()
    if output.exists(): raise ValueError('Refuse to overwrite an existing project or confirmation data')
    raw=Path(csv).read_bytes() if csv else urllib.request.urlopen(URL,timeout=30).read(2_000_001)
    if hashlib.sha256(raw).hexdigest()!=RAW_SHA: raise ValueError('public dataset hash mismatch')
    import io
    frame=pd.read_csv(io.BytesIO(raw)); values=frame.to_numpy(dtype=float)
    if values.shape!=(1030,9) or not np.isfinite(values).all(): raise ValueError('unexpected dataset')
    features=values[:,:8]; labels=values[:,8]
    groups=[digest(row[:7].tolist()) for row in features]
    unique=np.array(sorted(set(groups))); shuffled=np.random.default_rng(20261008).permutation(unique)
    a,b=int(.6*len(unique)),int(.8*len(unique))
    partition={g:split for split,gs in (('train',shuffled[:a]),('dev',shuffled[a:b]),('test',shuffled[b:])) for g in gs}
    splits={s:[i for i,g in enumerate(groups) if partition[g]==s] for s in ('train','dev','test')}
    output.mkdir(parents=True)
    for split,ids in splits.items():
        write_json(output/(split+'.json'),[{'id':f'concrete-{i}','x':i,'y':float(labels[i])} for i in ids])
    write_json(output/'confirmation-train.json',[
        {'id':f'concrete-{i}','x':i,'y':float(labels[i])} for i in splits['train']+splits['dev']])
    (output/'feature_map.py').write_text('FEATURES = '+repr(features.tolist())+'\n',encoding='utf-8')
    shutil.copy2(Path(__file__).resolve().parents[1]/'integrations/figs_concrete/model.py',output/'model.py')
    config={'model':'figs_multi_12'}
    hid='H-'+digest({'index':0,'config':config})[:16]
    protocol={'schema':'figs-grouped-ci/v1','bootstrap_seed':20261008,'bootstrap_samples':10000,
              'minimum_groups':30,'prediction':{'operator':'gt','threshold':.05},
              'hypothesis_predictions':{hid:{'operator':'gt','threshold':.05},
                                       'H-figs-single-sufficient':{'operator':'between','threshold':[-.05,.05]}},
              'upstream_commit':UPSTREAM,'features':{str(i):v for i,v in enumerate(features.tolist())},
              'groups':{str(i):g for i,g in enumerate(groups)}}
    write_json(output/'judgment-protocol.json',protocol)
    write_json(output/'provenance.json',{'dataset':'UCI Concrete Compressive Strength','doi':'10.24432/C5PK67',
        'license':'CC BY 4.0','url':URL,'raw_sha256':RAW_SHA,'upstream_commit':UPSTREAM,
        'grouping':'first seven recipe features; age excluded','split_seed':20261008,'model_seed':17,
        'splits':splits,'split_rows':{s:len(ids) for s,ids in splits.items()}})
    write_json(output/'experiment.json',{'name':'FIGS Concrete grouped comparison',
        'objective':'Compare additive multi-tree FIGS to single-tree FIGS at 12 rules; retain competing explanations and inconclusive results.',
        'entrypoint':'model.py','code_files':['model.py','feature_map.py'],
        'train':'train.json','dev':'dev.json','test':'test.json',
        'confirmation_train':'confirmation-train.json',
        'baseline':{'model':'figs_single_12'},'candidates':[config],
        'metric':{'name':'mae','direction':'min'},'seeds':[17],'budget':1,
        'timeout_seconds':120,'min_improvement':.05,
        'provenance_files':['provenance.json','judgment-protocol.json']})
    return output


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path,required=True); parser.add_argument('--csv',type=Path)
    args=parser.parse_args(); print(prepare(args.output,args.csv))


if __name__ == '__main__': main()
