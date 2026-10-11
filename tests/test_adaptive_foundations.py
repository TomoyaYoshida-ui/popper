import pytest
from popper.core import ProtocolError, dataset, score, write_json, file_hash, model_inputs
from popper.research.exposure_ledger import ExposureLedger
from popper.research.prediction_comparison import evaluate, verify_artifact


def test_vector_regression_has_independent_identity(tmp_path):
    rows = [{'id': 'a', 'features': [1., 2.], 'y': 3.}]
    write_json(tmp_path / 'rows.json', rows)
    assert dataset(tmp_path / 'rows.json', 'vector-mae-v1') == rows
    assert score(rows, [{'id': 'a', 'prediction': 2.}], 'vector-mae-v1') == 1.
    with pytest.raises(ProtocolError):
        dataset(tmp_path / 'rows.json', 'mae-v1')


def test_exposure_detects_renamed_samples_and_cohort_changes(tmp_path):
    ledger = ExposureLedger(tmp_path / 'exposure.sqlite')
    rows = [{'id': 'a', 'features': [1., 2.]}]
    try:
        ledger.reserve('batch', 'task', {'confirmation': rows})
        assert ledger.reserve('batch', 'task', {'confirmation': rows})
        with pytest.raises(ProtocolError):
            ledger.reserve('other', 'other', {'train': [{**rows[0], 'id': 'renamed'}]})
        first = ledger.open_confirmation('batch', 'task', {'candidate': 'frozen'})
        assert ledger.open_confirmation('batch', 'task', {'candidate': 'frozen'}) == first
        with pytest.raises(ProtocolError):
            ledger.open_confirmation('batch', 'task', {'candidate': 'changed'})
    finally:
        ledger.close()


def packet(tmp_path, n=40):
    rows = [{'id': str(i), 'features': [float(i)], 'y': float(i)} for i in range(n)]
    p = {}
    for name, value in [('data', rows), ('b', [{'id': r['id'], 'prediction': r['y'] + 2.} for r in rows]),
                        ('c', [{'id': r['id'], 'prediction': r['y'] + 1.} for r in rows])]:
        path = tmp_path / f'{name}.json'
        write_json(path, value)
        p[name] = {'path': str(path), 'sha256': file_hash(path)}
    bindings=[]
    for name in ('b','c'):
        root=tmp_path/name; (root/'code').mkdir(parents=True)
        (root/'code/model.py').write_text('pass')
        revision={'revision_id':name,'files':{'model.py':file_hash(root/'code/model.py')}}
        write_json(root/'revision.json',revision)
        inputs=[]
        for role,value in [('training_data',rows),('features_without_labels',model_inputs(rows,'vector-mae-v1')),('config',{})]:
            path=root/(role+'.json');write_json(path,value)
            inputs.append({'role':role,'source':str(path),'sha256':file_hash(path)})
        receipt={'status':'succeeded','job_id':name,'revision_id':name,
                 'revision_manifest_sha256':file_hash(root/'revision.json'),
                 'artifacts':{'outputs/predictions.json':p[name]['sha256']}}
        write_json(root/'receipt.json',receipt)
        write_json(root/'job.json',{'status':'succeeded','job_id':name,
            'receipt_sha256':file_hash(root/'receipt.json'),
            'spec':{'inputs':inputs,'revision_id':name,'revision_manifest_sha256':file_hash(root/'revision.json')}})
        p[name]['worker']={role:{'path':str(root/file),'sha256':file_hash(root/file)} for role,file in
                           [('receipt','receipt.json'),('manifest','job.json'),('revision','revision.json')]}
        bindings.extend(p[name]['worker'].values())
    return {'schema': 'prediction-packet-ci/v1', 'dataset': p['data']['path'],
            'dataset_sha256': p['data']['sha256'], 'baseline': p['b'], 'candidate': p['c'],
            'groups': {r['id']: r['id'] for r in rows}, 'independence_verified': True,
            'bootstrap_seed': 123, 'hypothesis_predictions': {'H-test': {'operator': 'gt', 'threshold': .05}},
            'bindings': bindings}


def test_grouped_comparison_recomputes_predictions_and_detects_tampering(tmp_path):
    p = packet(tmp_path)
    result = evaluate(p)
    assert result['mean_effect'] == .5 and result['ci95'] == [.5, .5]
    write_json(tmp_path / 'packet.json', p)
    write_json(tmp_path / 'response.json', result)
    d = {'packet': str(tmp_path / 'packet.json'), 'packet_sha256': file_hash(tmp_path / 'packet.json'),
         'response': str(tmp_path / 'response.json'), 'response_sha256': file_hash(tmp_path / 'response.json')}
    obs = [{'scorer_id': 'vector-mae-v1', 'value': 2.}, {'scorer_id': 'vector-mae-v1', 'value': 1.}]
    assert verify_artifact(d, obs) == result
    write_json(tmp_path / 'c.json', [{'id': str(i), 'prediction': float(i)} for i in range(40)])
    with pytest.raises(ProtocolError):
        verify_artifact(d, obs)


def test_training_seeds_do_not_inflate_analysis_units(tmp_path):
    p = packet(tmp_path)
    p['groups'] = {k: str(int(k) // 2) for k in p['groups']}
    result = evaluate(p)
    assert result['n_units'] == 20
    assert result['quality'] == 'insufficient_independent_units'


@pytest.mark.parametrize('target',['code/model.py','features_without_labels.json','training_data.json'])
def test_prediction_provenance_cannot_be_swapped(tmp_path,target):
    p=packet(tmp_path)
    evaluate(p)
    (tmp_path/'c'/target).write_text('[]')
    with pytest.raises(ProtocolError):evaluate(p)


def test_exposure_detects_shared_analysis_unit_with_different_inputs(tmp_path):
    ledger=ExposureLedger(tmp_path/'exposure.sqlite')
    try:
        ledger.reserve('one','task',{'confirmation':[{'id':'a','features':[1.,20.]}]},groups={'a':'same-tunnel-condition'})
        with pytest.raises(ProtocolError,match='Analysis unit overlaps'):
            ledger.reserve('two','task',{'train':[{'id':'renamed','features':[2.,20.]}]},groups={'renamed':'same-tunnel-condition'})
    finally:ledger.close()
