"""Trusted preparation of three frozen tasks; oracle information never reaches agents."""
from __future__ import annotations
import csv
import hashlib
import io
import shutil
import zipfile
from pathlib import Path
import numpy as np
from popper.core import digest, read_json, write_json

ROOT=Path(__file__).resolve().parents[1]
DATA_SEED=20261009
WINE_SHA='4a402cf041b025d4566d954c3b9ba8635a3a8a01e039005d97d6a710278cf05e'


def wine_csv(cache):
    import urllib.request
    cache=Path(cache); cache.mkdir(parents=True,exist_ok=True); path=cache/'winequality-red.csv'
    if not path.exists():
        from popper.request_lifecycle import RequestLifecycle
        # Public dataset retrieval, never a model request.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_environment()))
        with opener.open('https://archive.ics.uci.edu/static/public/186/wine+quality.zip',timeout=30) as response:
            raw=response.read(2_000_001)
        if len(raw)>2_000_000: raise ValueError('Dataset archive too large')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names=[n for n in archive.namelist() if n.endswith('winequality-red.csv')]
            if len(names)!=1: raise ValueError('Expected one official CSV')
            content=archive.read(names[0])
        if hashlib.sha256(content).hexdigest()!=WINE_SHA: raise ValueError('Wine CSV checksum mismatch')
        path.write_bytes(content)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=WINE_SHA: raise ValueError('Cached CSV changed')
    return path.read_bytes()


def task_rows(task, cache):
    rng=np.random.default_rng(DATA_SEED)
    if task=='T01':
        parsed=list(csv.DictReader(io.StringIO(wine_csv(cache).decode()),delimiter=';'))
        if len(parsed)!=1599: raise ValueError('Wine row count mismatch')
        x=np.array([[float(v) for k,v in r.items() if k!='quality'] for r in parsed]); y=np.array([float(r['quality']) for r in parsed])
        groups=[digest(r.tolist()) for r in x]; unique=np.array(sorted(set(groups))); shuffled=rng.permutation(unique)
        a,b=int(.6*len(unique)),int(.8*len(unique)); mapping={g:s for s,gs in (('train',shuffled[:a]),('dev',shuffled[a:b]),('test',shuffled[b:])) for g in gs}
        splits={s:[i for i,g in enumerate(groups) if mapping[g]==s] for s in ('train','dev','test')}
        domains=['standard']*len(y); exclude=10
        oracle={'public_data_pretraining_exposure_unknown':True,'source_sha256':WINE_SHA}
    elif task=='T02':
        x=rng.normal(size=(1400,3)); signal=np.sin(x[:,0])+.5*x[:,1]**2
        y=signal+rng.normal(scale=.25,size=len(x))
        domains=['source']*800+['independent']*600
        x[:800,2]=signal[:800]+rng.normal(scale=.05,size=800)
        splits={'train':list(range(600)),'dev':list(range(600,1000)),'test':list(range(1000,1400))}
        groups=[str(i) for i in range(len(y))]; exclude=2
        oracle={'generator':'shifted-regression/v1','signal':'sin(f0)+0.5*f1**2','noise_sd':.25,'proxy_changed':2}
    elif task=='T03':
        x=rng.normal(size=(1400,11)); y=rng.normal(size=1400)
        splits={'train':list(range(600)),'dev':list(range(600,1000)),'test':list(range(1000,1400))}
        groups=[str(i) for i in range(len(y))]; domains=['standard']*len(y); exclude=10
        oracle={'generator':'independent-normal-null/v1','bayes_mae_baseline':0.,'true_improvement_at_least_005':False}
    else: raise ValueError('Unknown task')
    return x,y,groups,domains,splits,exclude,oracle


def configurations(dimensions, exclude, zero_baseline):
    all_features=list(range(dimensions)); masked=[i for i in all_features if i!=exclude]
    def config(kind, features, leaf=1): return {'kind':kind,'features':features,'max_rules':12,'min_samples_leaf':leaf}
    baseline=config('zero' if zero_baseline else 'cart',all_features)
    candidates=[config(kind,features,leaf) for features in (all_features,masked)
                for kind,leaf in (('figs_single',1),('figs_multi',1),('cart',5),('cart',10))]
    return baseline,candidates


def prepare(task, project, cache):
    project=Path(project).resolve()
    if project.exists(): raise ValueError('Refuse to overwrite task or confirmation data')
    x,y,groups,domains,splits,exclude,oracle=task_rows(task,cache)
    project.mkdir(parents=True)
    for split,ids in splits.items():
        rows=[{'id':f'{domains[i]}-{i}','x':i,'y':float(y[i])} for i in ids]
        write_json(project/(split+'.json'),rows)
    (project/'feature_map.py').write_text('FEATURES = '+repr(x.tolist())+'\n',encoding='utf-8')
    shutil.copy2(ROOT/'integrations/transfer_pilot/model.py',project/'model.py')
    baseline,candidates=configurations(x.shape[1],exclude,task=='T03')
    predictions,contrasts,registered={},{},{}
    for i,c in enumerate(candidates):
        hid='H-'+digest({'index':i,'config':c})[:16]; alternate='H-alt-'+digest(hid)[:16]
        predictions[hid]={'operator':'gt','threshold':.05}; predictions[alternate]={'operator':'between','threshold':[-.05,.05]}
        contrasts[hid]={'alternative_id':alternate}
        registered[digest(c)]={**c,'hypothesis_id':hid,'is_baseline':False}
    registered[digest(baseline)]={**baseline,'hypothesis_id':'H-control-'+digest(baseline)[:12],'is_baseline':True}
    protocol={'schema':'paired-unit-ci/v1','metric':{'name':'mae','direction':'min'},'effect':'relative_mae_improvement',
        'unit':'ratio','confidence':.95,'bootstrap_samples':10000,'bootstrap_seed':DATA_SEED,'minimum_units':30,'model_seed':17,
        'features':{str(i):row for i,row in enumerate(x.tolist())},'groups':{str(i):g for i,g in enumerate(groups)},
        'configurations':registered,'hypothesis_predictions':predictions,'hypothesis_contrasts':contrasts,
        'analysis_scopes':{'dev:slice:s1':'source-','dev:slice:s2':'independent-'} if task=='T02' else {},
        'primary_dev_prefix':'independent-' if task=='T02' else ''}
    write_json(project/'judgment-protocol.json',protocol)
    spec={'name':task,'objective':'Compare registered predictive interventions; verify the declared practical threshold and applicability on independent confirmation units.',
        'entrypoint':'model.py','code_files':['model.py','feature_map.py'],'train':'train.json','dev':'dev.json','test':'test.json',
        'baseline':baseline,'candidates':candidates,'metric':{'name':'mae','direction':'min'},'seeds':[17],'budget':4,
        'timeout_seconds':120,'min_improvement':.05,'provenance_files':['judgment-protocol.json']}
    if task=='T02': spec['analysis_slices']=[{'slice_id':'s1','rule':{'kind':'id_prefix','value':'source-'}},
                                           {'slice_id':'s2','rule':{'kind':'id_prefix','value':'independent-'}}]
    write_json(project/'experiment.json',spec)
    write_json(project/'oracle-private.json',oracle)
    return {'task':task,'rows':len(y),'features':x.shape[1],'split_rows':{s:len(v) for s,v in splits.items()},
            'split_groups':{s:len({groups[i] for i in ids}) for s,ids in splits.items()},'excluded_feature':exclude,
            'protocol_sha256':digest(protocol),'raw_source_sha256':WINE_SHA if task=='T01' else None}
