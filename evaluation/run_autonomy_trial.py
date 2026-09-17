"""Observe an unmodified Popper campaign. Credentials are environment-only."""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from popper.core import Experiment, file_hash, initialize
from popper.orchestrator import Orchestrator
from popper.campaign_nodes import BUILTIN_NODES, DEFAULT_CAMPAIGN_STEPS
from popper.vendors import VendorRegistry
import popper.scoop as scoop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-trial", type=Path,
                        help="Reuse a prior model-generated candidate and literature; preserve original run")
    args = parser.parse_args()
    prior = args.source_trial.resolve() if args.source_trial else None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    trial = ROOT / 'integrations' / 'runs' / ('autonomy-' + stamp)
    trial.mkdir(parents=True)
    print('TRIAL=' + str(trial), flush=True)

    def save(name, data):
        (trial / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    hashes = {str(p.relative_to(ROOT)): file_hash(p) for p in (ROOT / 'popper').glob('*.py')}
    objective = (
        'Using the provided runnable WDBC binary-classification project, independently propose '
        'a falsifiable methodological hypothesis grounded in literature, implement it as a code '
        'proposal, compare the actual modified code with its original baseline under the frozen '
        'evaluation contract, and draw an evidence-bounded conclusion. Do not claim clinical '
        'validity or novelty without evidence. Report limitations and whether results justify '
        'a revised hypothesis, an additional control, or stopping. No human-written candidate '
        'or experimental patch is supplied.'
    )
    protocol = {
        'schema': 'autonomy-trial-1', 'created_utc': stamp,
        'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com',
        'objective': objective, 'source_hashes': hashes,
        'max_project_model_calls': 24, 'network_timeout_seconds': 120,
        'scope': 'Single familiar public-data task; success cannot establish general scientific novelty.',
        'criteria': [
            'Agent generates its own falsifiable candidate; no human replacement.',
            'Agent gathers literature and accurately marks incomplete full-text verification.',
            'Agent produces a substantive code proposal tied to its hypothesis.',
            'The proposed code is actually evaluated against original code with independent scoring.',
            'Agent uses observed results to revise, request a control, or justify stopping.',
            'Final evidence replays; required skipped stages cannot count as autonomous success.'
        ],
        'interventions': [
            'Evaluator selects existing public benchmark and freezes objective and budgets.',
            'Evaluator copies registered inputs into a new experiment; no history copied.',
            'Evaluator instruments model calls without changing prompts or model parameters.',
            'Generated code remains at the existing review gate pending inspection.'
        ],
        'no_manual_candidate_or_patch': True,
        'allow_provisional': False,
    }
    if prior:
        protocol['upstream_trial'] = str(prior)
        protocol['reuse_scope'] = 'Existing model-generated candidate and literature; new execution and archived fulltext revalidation'
        protocol['upstream_hashes'] = {str(p.relative_to(prior)): file_hash(p)
            for sub in ('idea', 'scoop') for p in (prior / sub).rglob('*.json')}
        for sub in ('idea', 'scoop'):
            shutil.copytree(prior / sub, trial / sub, ignore=shutil.ignore_patterns('model-diagnostics'))
    save('acceptance-protocol.json', protocol)
    save('protocol-fingerprint.json', {'sha256': file_hash(trial / 'acceptance-protocol.json')})
    source = prior / 'experiment' if prior else ROOT / 'examples' / 'breast-cancer-wisconsin'
    spec = json.loads((source / 'experiment.json').read_text(encoding='utf-8'))
    project = trial / 'experiment'
    project.mkdir()
    for name in set(['experiment.json', *spec['code_files'], *spec.get('provenance_files', []),
                     spec['train'], spec['dev'], spec['test']]):
        dest = project / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, dest)
    save('initialization.json', initialize(project))
    save('vendors.json', VendorRegistry().inspect())
    key = os.environ.get('POPPER_API_KEY')
    if not key:
        save('outcome.json', {'status': 'blocked', 'reason': 'missing credential'})
        return
    try:
        req = urllib.request.Request(protocol['base_url'] + '/models',
                                     headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(req, timeout=30) as response:
            models = json.load(response)
        ids = [m.get('id') for m in models.get('data', [])]
        save('provider-check.json', {'authenticated': True, 'available_models': ids,
                                     'requested_model_available': protocol['model'] in ids})
        print('PROVIDER authenticated; model available=' + str(protocol['model'] in ids), flush=True)
    except Exception as error:
        save('outcome.json', {'status': 'blocked', 'stage': 'provider_check',
                             'error_type': type(error).__name__, 'http_status': getattr(error, 'code', None)})
        print('PROVIDER failed: ' + type(error).__name__, flush=True)
        return

    original_client = scoop.make_json_client
    calls = []
    def observed_client(base_url, model, **kwargs):
        client = original_client(base_url, model, **kwargs)
        def call(system, payload):
            if len(calls) >= protocol['max_project_model_calls']:
                raise RuntimeError('Evaluator model-call budget exhausted')
            record = {'index': len(calls) + 1, 'system': system, 'payload': payload}
            calls.append(record)
            save('model-calls.json', calls)
            print('MODEL_CALL ' + str(record['index']), flush=True)
            started = time.monotonic()
            try:
                result = client(system, payload)
                record['response'] = result
                return result
            except Exception as error:
                record['error_type'] = type(error).__name__
                raise
            finally:
                record['elapsed_seconds'] = round(time.monotonic() - started, 3)
                save('model-calls.json', calls)
        return call
    scoop.make_json_client = observed_client
    run = Orchestrator(trial)
    run.init(objective, DEFAULT_CAMPAIGN_STEPS)
    started = time.monotonic()
    try:
        result = run.run(nodes=BUILTIN_NODES, config={
            'project': str(project), 'mode': 'trusted_local',
            'base_url': protocol['base_url'], 'model': protocol['model'],
            'start_year': 2015, 'end_year': 2026,
            'proposal_approved': False, 'allow_provisional': False,
        })
        outcome = {'status': 'observed', 'campaign_result': result}
    except Exception as error:
        outcome = {'status': 'execution_failed', 'error_type': type(error).__name__,
                   'message': str(error).replace(key, '[REDACTED]')}
    finally:
        scoop.make_json_client = original_client
    outcome['elapsed_seconds'] = round(time.monotonic() - started, 3)
    outcome['model_calls'] = len(calls)
    outcome['physical_model_attempts'] = len(list(trial.glob('**/model-diagnostics/*.json')))
    outcome['model_call_count_note'] = 'model_calls counts logical calls; physical attempts include bounded retries'
    outcome['source_unchanged'] = all(file_hash(ROOT / p) == h for p, h in hashes.items())
    outcome['human_research_content_supplied'] = False
    exp = Experiment(project)
    try:
        outcome['experiment_phase'] = exp.state()['phase']
        outcome['experiment_runs'] = len(exp.results())
    finally:
        exp.close()
    save('outcome.json', outcome)
    print(json.dumps(outcome, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
