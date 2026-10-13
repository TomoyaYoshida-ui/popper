"""Prepared regression tasks; oracle mechanisms never enter model contexts."""
from __future__ import annotations
import io
import hashlib
from pathlib import Path
import secrets
import urllib.request
import zipfile
import numpy as np
from ..core import ProtocolError, digest, file_hash, read_json, write_json
from .adaptive_contract import AdaptiveTaskContract

PUBLIC = [('abalone', 1, 'Rings'), ('auto-mpg', 9, 'mpg'), ('airfoil', 291, 'sound_pressure'),
          ('power-plant', 294, 'PE'), ('real-estate', 477, 'price_per_area'), ('hardware', 29, 'PRP')]
BASELINE = {'model': 'hgb', 'max_iter': 100, 'learning_rate': .1, 'max_leaf_nodes': 31}
FIXED_CONFIGS = ([{'model': 'ridge', 'alpha': a} for a in (.1, 1., 10.)] +
    [{'model': 'rf', 'n_estimators': 200, 'min_samples_leaf': n} for n in (1, 3)] +
    [{'model': 'hgb', 'max_iter': 200, 'max_leaf_nodes': n, 'learning_rate': rate}
     for n, rate in ((15, .1), (31, .05), (63, .1))])

MODEL_SOURCE = '''import argparse,json
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor,RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
p=argparse.ArgumentParser()
for key in ('train','input','output','config','seed'):p.add_argument('--'+key,required=True)
a=p.parse_args();train=json.load(open(a.train));rows=json.load(open(a.input));c=json.load(open(a.config));seed=int(a.seed)
X=np.array([r['features'] for r in train]);y=np.array([r['y'] for r in train])
if c['model']=='ridge':m=make_pipeline(StandardScaler(),Ridge(alpha=c['alpha']))
elif c['model']=='rf':m=RandomForestRegressor(n_estimators=c['n_estimators'],min_samples_leaf=c['min_samples_leaf'],n_jobs=2,random_state=seed)
else:m=HistGradientBoostingRegressor(max_iter=c['max_iter'],learning_rate=c['learning_rate'],max_leaf_nodes=c['max_leaf_nodes'],early_stopping=False,random_state=seed)
m.fit(X,y);pred=m.predict(np.array([r['features'] for r in rows]))
json.dump([{'id':r['id'],'prediction':float(v)} for r,v in zip(rows,pred)],open(a.output,'w'))
'''


def _public_rows(name, number):
    slug = {'abalone':'abalone','auto-mpg':'auto+mpg','airfoil':'airfoil+self+noise',
            'power-plant':'combined+cycle+power+plant','real-estate':'real+estate+valuation+data+set',
            'hardware':'computer+hardware'}[name]
    url = f'https://archive.ics.uci.edu/static/public/{number}/{slug}.zip'
    request = urllib.request.Request(url, headers={'User-Agent': 'PopperResearch/0.1'})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read(20_000_001)
    if len(raw) > 20_000_000:
        raise ProtocolError('Dataset download exceeds preparation limit')
    archive = zipfile.ZipFile(io.BytesIO(raw))
    files = archive.namelist()
    if name in {'power-plant', 'real-estate'}:
        import openpyxl
        chosen = next(p for p in files if p.endswith('.xlsx'))
        book = openpyxl.load_workbook(io.BytesIO(archive.read(chosen)), read_only=True, data_only=True)
        values = list(book.worksheets[0].values)[1:]
        if name == 'power-plant':
            X = [list(r[:4]) for r in values if r[0] is not None]; y = [r[4] for r in values if r[0] is not None]
        else:
            X = [list(r[1:7]) for r in values if r[0] is not None]; y = [r[7] for r in values if r[0] is not None]
    else:
        suffix = {'abalone':'abalone.data','auto-mpg':'auto-mpg.data','airfoil':'airfoil_self_noise.dat',
                  'hardware':'machine.data'}[name]
        lines = archive.read(next(p for p in files if p.endswith(suffix))).decode().strip().splitlines()
        X, y = [], []
        for line in lines:
            row = line.split(',') if name in {'abalone','hardware'} else line.split()
            if name == 'abalone':
                X.append([float(row[0] == c) for c in ('M','F','I')] + list(map(float,row[1:8]))); y.append(float(row[8]))
            elif name == 'hardware':
                X.append(list(map(float,row[2:8]))); y.append(float(row[8]))
            elif name == 'auto-mpg':
                X.append([float(v) if v != '?' else float('nan') for v in row[1:8]]); y.append(float(row[0]))
            else:
                X.append(list(map(float,row[:5]))); y.append(float(row[5]))
    return np.asarray(X,dtype=float), np.asarray(y,dtype=float), {'url':url,'raw_sha256':hashlib.sha256(raw).hexdigest(),
        'dataset_id':number,'pretraining_exposure':'unknown'}


def _generated(kind, rng, n):
    X = rng.uniform(-1, 1, (n,10)); noise = rng.normal(0,.3,n)
    if kind == 0:y=3*X[:,0]-2*X[:,1]+noise
    elif kind == 1:y=4*X[:,0]*X[:,1]+noise
    elif kind == 2:y=3*np.sin(np.pi*X[:,0])+2*X[:,1]**2+noise
    elif kind == 3:y=2*X[:,0]+noise
    elif kind == 4:y=2*X[:,0]+(.2+abs(X[:,1]))*rng.normal(size=n)+rng.choice([0.,10.],n,p=[.95,.05])*rng.normal(size=n)
    else:y=rng.normal(size=n)
    return X,y


def prepare_task(directory, task_id, *, public=None, kind=0, n=5000):
    root=Path(directory).resolve(); root.mkdir(parents=True,exist_ok=True)
    if (root/'prepared.json').exists():
        prepared=read_json(root/'prepared.json')
        for split, sha in prepared['data_hashes'].items():
            if file_hash(root/(split+'.json')) != sha:raise ProtocolError('Prepared data changed')
        return prepared
    seed=secrets.randbits(63);rng=np.random.default_rng(seed)
    source={'kind':'generated','oracle_hidden':True,'generator_version':'adaptive-tasks/v1'}
    if public:
        X,y,source=_public_rows(public[0],public[1]);target=public[2]
    else:
        X,y=_generated(kind,rng,n);target='response'
    groups={}
    for i,x in enumerate(X):
        # Airfoil frequencies at the same physical flow condition stay together.
        unit=x[1:] if public and public[0]=='airfoil' else x
        normalized=[None if not np.isfinite(v) else float(v) for v in unit]
        groups[str(i)]=digest(normalized)
    identities=np.array(sorted(set(groups.values())));order=rng.permutation(identities)
    a,b=int(.6*len(order)),int(.8*len(order))
    owners={g:split for split,values in [('train',order[:a]),('dev',order[a:b]),('confirmation',order[b:])] for g in values}
    indices={split:[i for i in range(len(X)) if owners[groups[str(i)]]==split] for split in ('train','dev','confirmation')}
    medians=np.nanmedian(X[indices['train']],axis=0)
    X=np.where(np.isfinite(X),X,medians)
    if not np.all(np.isfinite(X)) or not np.all(np.isfinite(y)):
        raise ProtocolError('Dataset contains an unfillable column or invalid target')
    if public is None and kind==3:
        X[indices['train'],9]=y[indices['train']]+rng.normal(0,.1,len(indices['train']))
        dev=indices['dev'];middle=len(dev)//2
        X[dev[:middle],9]=y[dev[:middle]]+rng.normal(0,.1,middle)
        X[dev[middle:],9]=rng.normal(size=len(dev)-middle)
        X[indices['confirmation'],9]=rng.normal(size=len(indices['confirmation']))
    for split, ids in indices.items():
        write_json(root/(split+'.json'),[{'id':str(i),'features':X[i].tolist(),'y':float(y[i])} for i in ids])
    train=X[indices['train']]
    card={'rows_train':len(train),'feature_count':X.shape[1],
          'feature_means_train':train.mean(axis=0).tolist(),'feature_std_train':train.std(axis=0).tolist(),
          'target_column':target,'source':source,'scope':'predictive performance; mechanisms remain unverified'}
    correlations=[]
    for column in train.T:
        correlations.append(float(np.corrcoef(column,y[indices['train']])[0,1]) if column.std()>0 else 0.)
    card['correlations_train']=correlations
    names=([f'x{i+1}' for i in range(X.shape[1])] if public is None else
           {'abalone':['sex_M','sex_F','sex_I','length','diameter','height','whole_weight','shucked_weight','viscera_weight','shell_weight'],
            'auto-mpg':['cylinders','displacement','horsepower','weight','acceleration','model_year','origin'],
            'airfoil':['frequency','attack_angle','chord_length','velocity','displacement_thickness'],
            'power-plant':['temperature','vacuum','pressure','humidity'],
            'real-estate':['transaction_date','house_age','MRT_distance','stores','latitude','longitude'],
            'hardware':['MYCT','MMIN','MMAX','CACH','CHMIN','CHMAX']}[public[0]])
    data={'task_id':task_id,'target_column':target,'feature_names':names,'source':source,
          'groups':groups,'data_hashes':{s:file_hash(root/(s+'.json')) for s in indices},
          'independence_verified':public is None,
          'sampling':({'design':'iid_row','generator_version':'adaptive-tasks/v1'} if public is None
                      else {'design':'feature_row_hash'}),
          'card':card,
          'materials':[{'id':'dataset-card','text':str(card)}], 'bootstrap_seed':int(rng.integers(0,2**32))}
    write_json(root/'oracle-private.json',{'seed':seed,'kind':kind if public is None else None})
    write_json(root/'prepared.json',data)
    return data


def contract_for(prepared, seed):
    return AdaptiveTaskContract(task_id=prepared['task_id'],objective='Propose and test a scientifically motivated predictive intervention using the frozen target.',
        target_column=prepared['target_column'],feature_names=prepared['feature_names'],
        data_hashes=prepared['data_hashes'],baseline=BASELINE,analysis_unit='registered observation groups',
        independence_verified=prepared['independence_verified'],sampling=prepared.get('sampling'),
        groups=prepared['groups'],
        bootstrap_seed=prepared['bootstrap_seed'],model_seed=seed,materials=prepared['materials']).model_dump()
