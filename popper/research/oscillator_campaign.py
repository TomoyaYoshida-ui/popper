"""Isolated real-conversation campaign; no automatic approvals or confirmations.

prepare is offline. propose makes at most ONE budgeted request, freezes its
answer (including one-shot code), and stops for explicit Study approval. A
failed/ambiguous request is retained, never silently replaced.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import time

from ..api_contracts import new_trace_id
from ..core import ProtocolError, canonical, digest, file_hash, initialize, read_json, write_json
from ..server import Workstation
from ..sessions import AgentSessionStore
from .agent_evaluation_budget import AgentEvaluationBudget
from .oscillator_proposals import preview_proposal, initialize_reviewed_study
from .oscillator_study import verify
from .oscillator_execution import probe_read_isolation
from .oscillator_worker import freeze_binding
from .revision_drafts import preview_initial_revision, submit_initial_revision
from .readout import read_research_state


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = 'campaign.json'
SCIENTIFIC_SOURCES = ('core.py', 'domains/oscillator_trajectory.py',
    'research/oscillator_study.py', 'research/oscillator_baseline.py',
    'research/evaluation_service.py', 'research/controller.py',
    'research/store.py', 'research/schema.py')


def _hashes(paths):
    return {str(Path(path).resolve()): file_hash(path) for path in paths}


def prepare(study_root, destination, model, base_url, *, shared_budget=None):
    study_root, destination = Path(study_root).resolve(), Path(destination).resolve()
    verify(study_root)
    if destination.exists():
        raise ProtocolError('campaign destination already exists; preserve and reconcile it')
    material_receipt = read_json(study_root / 'initial-material-receipt-v1.json')
    for name, expected in material_receipt['study_artifacts_sha256'].items():
        if file_hash(study_root / name) != expected:
            raise ProtocolError('initial material source changed')
    for name, expected in material_receipt['repository_artifacts_sha256'].items():
        if file_hash(ROOT / name) != expected:
            raise ProtocolError('registered control implementation changed')
    material = read_json(study_root / 'agent-visible/initial-material-v1.json')
    relative_threshold = .20
    baseline = read_json(study_root / 'baseline-development.json')['trajectory_error']
    candidates = [{'slot': index} for index in range(1, 7)]
    common = {'material': material['common_instructions'], 'literature': material['literature'],
              'objective': material['objective'], 'candidates': candidates,
              'fixed_control_source': (ROOT / 'popper/research/oscillator_baseline.py').read_text(encoding='utf-8'),
              'public_files': ['public-train.json', 'public-dev.json'],
              'first_reply_rule': 'Return ONLY one complete JSON object, no markdown and no tools. '
                  'Use kind=oscillator_hypotheses_v1, exact objective/candidates, six concise '
                  'hypotheses indexed 0..5 (mechanism, applicability, predictions, falsification, alternatives). '
                  'Also include implementation={candidate_index,path:"model.py",replacement:complete Python source}. '
                  'Choose one candidate yourself. Include your initial prediction before seeing any score. '
                  'Allowed top-level keys are kind, objective, candidates, hypotheses, implementation '
                  'and optional initial_prediction (nonempty text or a nonempty JSON object; '
                  'at most 4096 UTF-8 bytes of text or compact JSON, finite numbers only). '
                  'Each hypothesis has only candidate_index, mechanism, applicability, predictions, '
                  'falsification and alternatives; the last three are nonempty lists of text. '
                  'Keep hypotheses concise enough to fit the same 4096 output-token limit with source code. '
                  'You may use standard-library arithmetic freely; the RHS budget is queries, not all arithmetic. '
                  'The public files are source material, not permission to read private data. '
                  'The initial answer is frozen before approval or feedback; no inferred repairs.'}
    destination.mkdir(parents=True)
    ledger = Path(shared_budget).resolve() if shared_budget else destination / 'budget.sqlite'
    if shared_budget and not ledger.is_file():
        raise ProtocolError('shared campaign budget must already exist; never silently reset it')
    with closing(AgentEvaluationBudget(ledger)) as budget:
        if budget.status()['unresolved_requests']:
            raise ProtocolError('shared campaign budget has unresolved usage')
        prior_requests = [r[0] for r in budget.connection.execute('SELECT id FROM requests ORDER BY id')]
    inherited_private = {}
    if shared_budget:
        parent_manifest = ledger.parent / MANIFEST
        if not parent_manifest.is_file():
            raise ProtocolError('shared budget requires registered parent campaign provenance')
        parent = read_json(parent_manifest)
        if parent['protocol_sha256'] != file_hash(study_root / 'protocol.json'):
            raise ProtocolError('shared campaign uses a different scientific protocol')
        for path, expected in parent['materials_sha256'].items():
            if file_hash(path) != expected:
                raise ProtocolError('parent campaign materials changed')
        inherited_private = _hashes(protected_files(parent))
    runs = []
    for arm in ('single_shot', 'popper_loop'):
        for index in range(1, 4):
            run_id = f'{arm}-{index}'
            directory = destination / run_id
            project = directory / 'project'
            project.mkdir(parents=True)
            for split in ('train', 'dev', 'test'):
                shutil.copyfile(study_root / 'evaluator-private' / (split + '.json'), project / (split + '.json'))
            for split in ('train', 'dev'):
                shutil.copyfile(study_root / 'agent-visible' / (split + '.json'), project / ('public-' + split + '.json'))
            shutil.copyfile(ROOT / 'popper/research/oscillator_baseline.py', project / 'model.py')
            write_json(project / 'evaluation-policy.json', {'version': 1, 'arm': arm,
                'candidate_development_limit': 1 if arm == 'single_shot' else 6,
                'confirmation_requires_user_approval': True})
            write_json(project / 'experiment.json', {'name': 'Frozen oscillator Agent study',
                'objective': material['objective'], 'entrypoint': 'model.py', 'code_files': ['model.py'],
                'train': 'train.json', 'dev': 'dev.json', 'test': 'test.json',
                'provenance_files': ['evaluation-policy.json'],
                'baseline': {'arm': 'fixed_rk4'}, 'candidates': candidates,
                'metric': {'name': 'trajectory_error', 'direction': 'min'},
                'seeds': [0], 'budget': 6, 'timeout_seconds': 600,
                'min_improvement': baseline * relative_threshold})
            initialize(project)
            prompt = canonical({**common, 'arm_rule': material['arm_rules'][arm]})
            write_json(directory / 'initial-prompt.json', {'prompt': prompt, 'sha256': digest(prompt)})
            runs.append({'run_id': run_id, 'arm': arm, 'project': str(project),
                         'research': str(directory / 'research'), 'prompt_sha256': digest(prompt)})
    files = [p for run in runs for p in Path(run['project']).glob('*.json')]
    files += [Path(run['project']) / 'model.py' for run in runs]
    files += [destination / run['run_id'] / 'initial-prompt.json' for run in runs]
    source_names = ['server.py', 'chat.py', 'revision_tools.py', 'sessions.py', 'task_context.py']
    sources = [ROOT / 'popper' / name for name in source_names]
    sources += list(Path(__file__).parent.glob('oscillator_*.py'))
    sources += [Path(__file__).with_name(name) for name in ('agent_revisions.py', 'revision_drafts.py',
                                                          'revision_archive.py', 'agent_evaluation_budget.py')]
    manifest = {'version': 1, 'status': 'prepared_not_armed', 'study_root': str(study_root),
        'protocol_sha256': file_hash(study_root / 'protocol.json'), 'runs': runs,
        'model': model, 'base_url': base_url, 'temperature': .7, 'max_output_tokens': 4096,
        'request_limit': 60, 'token_limit': 400000, 'budget_ledger': str(ledger),
        'preexisting_request_ids': prior_requests,
        'inherited_protected_files_sha256': inherited_private,
        'materials_sha256': _hashes(files), 'implementation_sha256': _hashes(
            set(sources) | set((ROOT / 'popper').rglob('*.py'))),
        'primary_criterion': {'relative_error_reduction': .20, 'successful_loop_runs': 2,
                              'confirmation_failure_rate_not_greater_than_control': True},
        'kernel_absolute_dev_threshold': baseline * relative_threshold,
        'threshold_note': 'Kernel uses frozen absolute development threshold; final report must '
                          'separately test relative held-out improvement. A Claim is not the primary verdict.',
        'network_block': False, 'no_automatic_approvals': True}
    write_json(destination / MANIFEST, manifest)
    return manifest


def _execution_snapshot(root, manifest):
    """Source-only append revisions; original scientific inputs stay authoritative."""
    root = Path(root).resolve()
    previous = file_hash(root / MANIFEST)
    sources, version_path = manifest['implementation_sha256'], root / MANIFEST
    for index, path in enumerate(sorted((root / 'execution-revisions').glob('*.json')), start=1):
        if path.name != f'{index:04d}.json':
            raise ProtocolError('execution revision sequence is incomplete')
        revision = read_json(path)
        if (set(revision) != {'version', 'parent_sha256', 'campaign_sha256', 'reason',
                             'implementation_sha256'} or revision['version'] != index
                or revision['parent_sha256'] != previous
                or revision['campaign_sha256'] != file_hash(root / MANIFEST)):
            raise ProtocolError('execution revision ancestry changed')
        sources = revision['implementation_sha256']
        if not set(manifest['implementation_sha256']).issubset(sources):
            raise ProtocolError('execution revision dropped registered sources')
        if any(not Path(p).resolve().is_relative_to(ROOT / 'popper') for p in sources):
            raise ProtocolError('execution source outside application')
        for name in SCIENTIFIC_SOURCES:
            key = str((ROOT / 'popper' / name).resolve())
            if sources.get(key) != manifest['implementation_sha256'].get(key):
                raise ProtocolError('execution revision cannot change scoring or Research Kernel')
        previous, version_path = file_hash(path), path
    return sources, version_path, previous


def freeze_execution_revision(root, reason, *, offline_only=False):
    """Freeze infrastructure changes only. Does not arm or dispatch anything."""
    root = Path(root).resolve()
    manifest = read_json(root / MANIFEST)
    verify(manifest['study_root'])
    if file_hash(Path(manifest['study_root']) / 'protocol.json') != manifest['protocol_sha256']:
        raise ProtocolError('original study protocol changed')
    for path, expected in manifest['materials_sha256'].items():
        if file_hash(path) != expected:
            raise ProtocolError('campaign materials changed')
    for path, expected in manifest.get('inherited_protected_files_sha256', {}).items():
        if file_hash(path) != expected:
            raise ProtocolError('inherited protected artifact changed')
    _, prior_path, parent = _execution_snapshot(root, manifest)
    with closing(AgentEvaluationBudget(manifest['budget_ledger'])) as ledger:
        unresolved = ledger.status()['unresolved_requests']
        if unresolved and not offline_only:
            raise ProtocolError('reconcile usage before freezing continuation')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ProtocolError('execution revision requires a bounded reason')
    version = 1 if prior_path.name == MANIFEST else int(prior_path.stem) + 1
    revision = {'version': version, 'parent_sha256': parent,
        'campaign_sha256': file_hash(root / MANIFEST), 'reason': reason,
        'implementation_sha256': _hashes((ROOT / 'popper').rglob('*.py'))}
    for name in SCIENTIFIC_SOURCES:
        key = str((ROOT / 'popper' / name).resolve())
        if revision['implementation_sha256'].get(key) != manifest['implementation_sha256'].get(key):
            raise ProtocolError('execution revision cannot change scoring or Research Kernel')
    directory = root / 'execution-revisions'
    directory.mkdir(exist_ok=True)
    with (directory / f'{version:04d}.json').open('x', encoding='utf-8') as stream:
        stream.write(canonical(revision))
    return {'revision': version, 'status': 'frozen_not_armed',
        'unresolved_requests_at_freeze': unresolved, 'sha256': file_hash(
        directory / f'{version:04d}.json')}


def _arm_path(root, source_path):
    return (Path(root) / 'armed.json' if source_path.name == MANIFEST
            else Path(root) / 'execution-arms' / source_path.name)


def load_campaign(root, run_id=None):
    root = Path(root).resolve()
    manifest = read_json(root / MANIFEST)
    verify(manifest['study_root'])
    if file_hash(Path(manifest['study_root']) / 'protocol.json') != manifest['protocol_sha256']:
        raise ProtocolError('original study protocol changed')
    sources, _, _ = _execution_snapshot(root, manifest)
    for path, expected in sources.items():
        if file_hash(path) != expected:
            raise ProtocolError('campaign implementation changed; new protocol revision required')
    # model.py is frozen registered input and must never be edited in place.
    for path, expected in manifest['materials_sha256'].items():
        if file_hash(path) != expected:
            raise ProtocolError('campaign materials changed')
    for path, expected in manifest.get('inherited_protected_files_sha256', {}).items():
        if file_hash(path) != expected:
            raise ProtocolError('inherited protected artifact changed')
    run = next((run for run in manifest['runs'] if run['run_id'] == run_id), None)
    if run_id is not None and run is None:
        raise ProtocolError('run ID not registered in this campaign')
    return manifest, run


def protected_files(manifest):
    private = list((Path(manifest['study_root']) / 'evaluator-private').glob('*.json'))
    private += [Path(run['project']) / (split + '.json') for run in manifest['runs']
                for split in ('train', 'dev', 'test')]
    sources = [ROOT / 'popper/domains/oscillator_trajectory.py',
               ROOT / 'popper/research/evaluation_service.py', ROOT / 'popper/research/oscillator_study.py']
    return list(dict.fromkeys(private + sources + [p for source in sources
        for p in source.parent.joinpath('__pycache__').glob(source.stem + '.*.pyc')]
        + [Path(p) for p in manifest.get('inherited_protected_files_sha256', {})]))


def arm_campaign(root, regression_log):
    manifest, _ = load_campaign(root)
    root = Path(root).resolve()
    _, source_path, source_sha = _execution_snapshot(root, manifest)
    path = _arm_path(root, source_path)
    if path.exists():
        raise ProtocolError('existing arm receipt must be reconciled, not overwritten')
    log = Path(regression_log).resolve()
    text = log.read_text(encoding='utf-8')
    # Evidence must correspond to these exact sources, generated after prepare.
    if log.stat().st_mtime < source_path.stat().st_mtime or ' failed' in text or ' passed' not in text:
        raise ProtocolError('a fresh passing full regression on frozen campaign sources is required')
    import re
    match = re.search(r'(\d+) passed, (\d+) skipped, (\d+) subtests passed in ', text)
    if not match or int(match[1]) < 1000:
        raise ProtocolError('not a complete project regression log')
    with closing(AgentEvaluationBudget(manifest['budget_ledger'])) as ledger:
        ledger_state = ledger.status()
        if ledger_state['unresolved_requests']:
            raise ProtocolError('unresolved usage prevents continuation arming')
        if source_path.name == MANIFEST:
            current_ids = [r[0] for r in ledger.connection.execute('SELECT id FROM requests ORDER BY id')]
            if current_ids != manifest.get('preexisting_request_ids', []):
                raise ProtocolError('shared budget changed after prepare; reconcile before arming')
            if any((root / run['run_id'] / 'initial-request.json').exists() for run in manifest['runs']):
                raise ProtocolError('arming must precede this campaign model requests')
    probe = probe_read_isolation(protected_files(manifest))
    receipt = {'campaign_sha256': file_hash(root / MANIFEST), 'regression_sha256': file_hash(log),
               'regression_log': str(log), 'read_probe': probe,
               'execution_snapshot_sha256': source_sha,
               'formal_requests_before_arm': ledger_state['request_count'],
               'scope': 'initial model proposals only; all writes/executions require separate user approval'}
    path.parent.mkdir(exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(canonical(receipt))
    return receipt


def propose(root, run_id, *, chat_llm=None):
    root = Path(root).resolve()
    manifest, run = load_campaign(root, run_id)
    _, source_path, source_sha = _execution_snapshot(root, manifest)
    armed = read_json(_arm_path(root, source_path))
    if armed['campaign_sha256'] != file_hash(root / MANIFEST) or not armed['read_probe']['passed']:
        raise ProtocolError('campaign not armed for this exact snapshot')
    if armed.get('execution_snapshot_sha256', file_hash(root / MANIFEST)) != source_sha:
        raise ProtocolError('execution source revision is not armed')
    directory = root / run_id
    request_path = directory / 'initial-request.json'
    answer_path = directory / 'initial-answer.json'
    if request_path.exists():
        return {'status': 'already_attempted_do_not_redispatch',
                'request': read_json(request_path), 'answer': read_json(answer_path) if answer_path.exists() else None}
    with closing(AgentEvaluationBudget(manifest['budget_ledger'])) as ledger:
        if ledger.status()['unresolved_requests']:
            raise ProtocolError('campaign stopped: unknown usage or ambiguous dispatch; no further run launches')
    ws = Workstation(('127.0.0.1', 0), run['project'], trusted_local=True,
        chat_base_url=manifest['base_url'] if chat_llm is None else None,
        chat_model=manifest['model'] if chat_llm is None else None,
        chat_llm=chat_llm, evaluation_budget_path=manifest['budget_ledger'])
    try:
        ws.chat_model = manifest['model']
        # The project lease serializes concurrent launches; recheck after taking
        # it in case a previous process completed between precheck and acquire.
        if request_path.exists():
            return {'status': 'already_attempted_do_not_redispatch',
                    'request': read_json(request_path),
                    'answer': read_json(answer_path) if answer_path.exists() else None}
        session = ws.create_agent_session('Frozen initial research proposal')
        store = AgentSessionStore(run['project'])
        try:
            store.set_token_budget(session['session_id'], 400000)
        finally:
            store.close()
        prompt = read_json(directory / 'initial-prompt.json')['prompt']
        request = {'session_id': session['session_id'], 'prompt_sha256': digest(prompt),
                   'idempotency_key': 'formal-initial-proposal-v1', 'provider': 'synthetic' if chat_llm else 'real'}
        write_json(request_path, request)  # persists intent BEFORE any outbound dispatch
        result = ws.launch_session_turn(session['session_id'], prompt, request['idempotency_key'], new_trace_id())
        write_json(directory / 'initial-dispatch.json', {'turn_id': result['turn']['turn_id'],
                   'job_id': result['job']['job_id'], 'provider': request['provider']})
        deadline = time.monotonic() + 600
        while ws.wait_for_jobs(timeout=1):
            if time.monotonic() > deadline:
                raise ProtocolError('model job did not reach terminal state; reconcile before any retry')
        store = AgentSessionStore.readonly(run['project'])
        try:
            turn = store.get_turn(result['turn']['turn_id'])
            job = store.get_agent_job(result['job']['job_id'])
        finally:
            store.close()
        answer = {'session_id': session['session_id'], 'turn_id': turn['turn_id'],
                  'status': turn['status'], 'provider': request['provider'], 'error': turn.get('error'),
                  'model_job_id': job['job_id']}
        if turn['status'] == 'completed':
            try:
                answer['preview'] = preview_proposal(run['project'], manifest['budget_ledger'],
                                                     session['session_id'], turn['turn_id'])
                if 'implementation' not in answer['preview']:
                    raise ProtocolError('initial answer omitted code; no automatic rescue request')
                answer['status'] = 'awaiting_user_study_approval'
            except Exception as error:
                answer['status'] = 'invalid_initial_answer'
                answer['error'] = {'type': type(error).__name__, 'message': str(error)}
        write_json(answer_path, answer)
        return answer
    finally:
        ws.wait_for_jobs(timeout=30)
        ws.server_close()


def approve_study(root, run_id, approval_sha256):
    manifest, run = load_campaign(root, run_id)
    answer = read_json(Path(root) / run_id / 'initial-answer.json')
    state = initialize_reviewed_study(run['project'], run['research'], manifest['budget_ledger'],
                                     answer['session_id'], answer['turn_id'], approval_sha256)
    freeze_binding(run['research'], protected_files(manifest))
    return {'study': state['study'], 'status': 'study_created_no_revision_or_experiment_executed'}


def initial_code_preview(root, run_id):
    manifest, run = load_campaign(root, run_id)
    answer = read_json(Path(root) / run_id / 'initial-answer.json')
    authored = preview_proposal(run['project'], manifest['budget_ledger'],
                                answer['session_id'], answer['turn_id'])
    state = read_research_state(run['research'])
    if not state.get('ok'):
        raise ProtocolError('approve the exact authored Study before proposing its Revision')
    implementation = authored['implementation']
    registered = read_json(Path(run['research']) / 'research.json')
    hypothesis_id = registered['candidate_hypothesis_ids'][implementation['candidate_index']]
    design = next(design for design in state['designs'] if design['hypothesis_id'] == hypothesis_id)
    spec = {'study_id': state['manifest']['study_id'], 'hypothesis_id': hypothesis_id,
            'design_id': design['design_id'], 'expected_state_version': state['state_version'],
            'path': 'model.py', 'expected_sha256': file_hash(Path(run['project']) / 'model.py'),
            'replacement': implementation['replacement']}
    preview = preview_initial_revision(run['research'], run['project'], spec)
    return {'spec': spec, 'preview': preview, 'source_turn_id': answer['turn_id'],
            'source_sha256': authored['source_sha256'], 'effect': 'create only; no experiment',
            'provider': answer['provider']}


def approve_initial_code(root, run_id, approval_sha256):
    manifest, run = load_campaign(root, run_id)
    path = Path(root) / run_id / 'first-revision-binding.json'
    if path.exists():
        prior = read_json(path)
        if prior['approval_sha256'] != approval_sha256:
            raise ProtocolError('initial source approval changed; cannot rebind')
        from .revisions import RevisionStore
        RevisionStore(Path(run['research']) / 'revisions').verify(prior['result']['revision_id'])
        return prior
    checked = initial_code_preview(root, run_id)
    result = submit_initial_revision(run['research'], run['project'], checked['spec'],
                                     approval_sha256, actor='agent')
    receipt = {'source_turn_id': checked['source_turn_id'], 'source_sha256': checked['source_sha256'],
               'approval_sha256': approval_sha256, 'provider': checked['provider'], 'result': result,
               'effect': 'approved source creation only; execution needs separate approval'}
    write_json(path, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'arm', 'freeze-execution', 'propose', 'approve-study',
                                        'preview-code', 'approve-code', 'status'))
    parser.add_argument('root')
    parser.add_argument('--study-root')
    parser.add_argument('--model', default='mimo-v2.5-pro')
    parser.add_argument('--base-url', default='https://api.xiaomimimo.com/v1')
    parser.add_argument('--run-id')
    parser.add_argument('--regression-log')
    parser.add_argument('--approval-sha256')
    parser.add_argument('--reason')
    parser.add_argument('--shared-budget')
    parser.add_argument('--offline-only', action='store_true',
                        help='freeze repair evidence only; unresolved usage still prohibits arming and calls')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare(args.study_root, args.root, args.model, args.base_url,
                         shared_budget=args.shared_budget)
    elif args.action == 'arm':
        result = arm_campaign(args.root, args.regression_log)
    elif args.action == 'freeze-execution':
        result = freeze_execution_revision(args.root, args.reason, offline_only=args.offline_only)
    elif args.action == 'propose':
        result = propose(args.root, args.run_id)
    elif args.action == 'approve-study':
        result = approve_study(args.root, args.run_id, args.approval_sha256)
    elif args.action == 'preview-code':
        result = initial_code_preview(args.root, args.run_id)
    elif args.action == 'approve-code':
        result = approve_initial_code(args.root, args.run_id, args.approval_sha256)
    else:
        manifest, _ = load_campaign(args.root)
        with closing(AgentEvaluationBudget(manifest['budget_ledger'])) as ledger:
            result = ledger.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
