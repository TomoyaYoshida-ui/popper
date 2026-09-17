"""Frozen submission and Ed25519 receipts for a separate holdout authority.

Signatures authenticate a pinned key; they do not attest OS isolation. Private
keys and labels must be kept outside the candidate runner's readable domain.
"""
from __future__ import annotations

import base64
import math
import re
from pathlib import Path, PurePosixPath

from ..core import EVALUATORS, ProtocolError, digest, evaluator_metric, file_hash


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
        return serialization, Ed25519PrivateKey, Ed25519PublicKey
    except ImportError:
        raise ProtocolError('Independent confirmation requires the confirmation dependency extra') from None


def load_private_key(path, create=False):
    serialization, private_type, _ = _crypto()
    path = Path(path)
    if not path.exists() and create:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = private_type.generate()
        raw = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        with path.open('xb') as stream:
            stream.write(raw)
    if not path.is_file() or path.is_symlink():
        raise ProtocolError('Signing key must be an existing regular private file')
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError):
        raise ProtocolError('Invalid signing key') from None
    if not isinstance(key, private_type):
        raise ProtocolError('Signing key must use Ed25519')
    return key


def public_key_b64(key):
    serialization, private_type, public_type = _crypto()
    if isinstance(key, private_type):
        key = key.public_key()
    if not isinstance(key, public_type):
        raise ProtocolError('Expected Ed25519 key')
    return base64.b64encode(key.public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)).decode('ascii')


def _public_key(value):
    _, _, public_type = _crypto()
    try:
        if not isinstance(value, str):
            raise ValueError
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32 or base64.b64encode(raw).decode('ascii') != value:
            raise ValueError
        return public_type.from_public_bytes(raw)
    except (ValueError, TypeError):
        raise ProtocolError('Invalid pinned Ed25519 public key') from None


def sign_payload(payload, key):
    from ..core import canonical
    if not isinstance(payload, dict):
        raise ProtocolError('Signed payload must be an object')
    public = public_key_b64(key)
    signature = key.sign(canonical(payload).encode('utf-8'))
    return {'payload': payload, 'signature': base64.b64encode(signature).decode('ascii'),
            'public_key_id': digest(public)[:24]}


def verify_envelope(envelope, pinned_public_key):
    from ..core import canonical
    key = _public_key(pinned_public_key)
    _fields(envelope, {'payload', 'signature', 'public_key_id'}, 'signed envelope')
    if envelope['public_key_id'] != digest(pinned_public_key)[:24]:
        raise ProtocolError('Receipt issuer does not match the independently pinned key')
    try:
        if not isinstance(envelope['payload'], dict) or not isinstance(envelope['signature'], str):
            raise ValueError
        signature = base64.b64decode(envelope['signature'], validate=True)
        if len(signature) != 64:
            raise ValueError
        key.verify(signature, canonical(envelope['payload']).encode('utf-8'))
    except Exception as error:
        # Do not disclose any payload or key material through error reporting.
        raise ProtocolError('Invalid Ed25519 signature or payload') from error
    return envelope['payload']


def confirmation_service_hash():
    base = Path(__file__).resolve().parent
    paths = {'core.py': base.parent / 'core.py',
             'evaluation_service.py': base / 'evaluation_service.py',
             'confirmation_contracts.py': base / 'confirmation_contracts.py',
             'confirmation_service.py': base / 'confirmation_service.py'}
    return digest({name: file_hash(path) for name, path in paths.items()})


def _fields(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ProtocolError(f'Invalid {label} fields')


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ProtocolError(f'Invalid {label}')


def _hash(value, label):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ProtocolError(f'Invalid {label} SHA-256')


def _seeds(value):
    if (not isinstance(value, list) or not value or len(value) > 100
            or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in value)
            or len(set(value)) != len(value)):
        raise ProtocolError('Invalid complete seed contract')


def _finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ProtocolError(f'Invalid finite {label}')


def safe_code_path(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ProtocolError('Code paths must be portable relative POSIX paths')
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or '..' in path.parts or path.as_posix() != value
            or path.suffix != '.py' or len(value) > 240
            or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) or part.endswith('.')
                   or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                                     *[f'COM{i}' for i in range(1, 10)],
                                                     *[f'LPT{i}' for i in range(1, 10)]}
                   for part in path.parts)):
        raise ProtocolError('Unsafe code path')
    return value


def _code_files(files, entrypoint):
    if not isinstance(files, dict) or not files or len(files) > 200:
        raise ProtocolError('Invalid code file manifest')
    seen = set()
    for name, value in files.items():
        safe_code_path(name)
        if name.casefold() in seen:
            raise ProtocolError('Ambiguous code path casing')
        seen.add(name.casefold())
        _hash(value, 'code')
    if entrypoint not in files:
        raise ProtocolError('Entrypoint not in frozen code')


def validate_contract(envelope, pinned_public_key):
    value = verify_envelope(envelope, pinned_public_key)
    version = value.get('schema_version') if isinstance(value, dict) else None
    fields = {'schema_version', 'kind', 'contract_id', 'issuer_id', 'dataset_id',
              'dataset_version', 'evaluation_group', 'train_sha256', 'dev_sha256',
              'holdout_sha256', 'features_sha256', 'evaluator_id', 'evaluator_hash',
              'scoring_code_sha256', 'service_code_sha256', 'seeds', 'min_effect',
              'runtime_id', 'runner_public_key', 'allowed_backend', 'trust'}
    if version == '2.0':
        fields.add('analysis_slices')
    _fields(value, fields, 'holdout contract')
    if version not in {'1.0', '2.0'} or value['kind'] != 'holdout_contract':
        raise ProtocolError('Unsupported holdout contract')
    identity = {key: item for key, item in value.items() if key != 'contract_id'}
    if value['contract_id'] != 'HC-' + digest(identity)[:24]:
        raise ProtocolError('Contract identity mismatch')
    if value['issuer_id'] != digest(pinned_public_key)[:24]:
        raise ProtocolError('Contract issuer mismatch')
    for name in ('dataset_id', 'dataset_version', 'evaluation_group', 'runtime_id', 'allowed_backend'):
        _text(value[name], name)
    for name in ('train_sha256', 'dev_sha256', 'holdout_sha256', 'features_sha256',
                 'evaluator_hash', 'scoring_code_sha256', 'service_code_sha256'):
        _hash(value[name], name)
    evaluator = value['evaluator_id']
    if not isinstance(evaluator, str) or evaluator not in EVALUATORS or value['evaluator_hash'] != digest(EVALUATORS[evaluator]):
        raise ProtocolError('Unsupported registered evaluator')
    _seeds(value['seeds'])
    if version == '2.0':
        from .evaluation_service import _analysis_slices
        _analysis_slices(value['analysis_slices'], evaluator)
    _finite(value['min_effect'], 'effect threshold')
    if value['min_effect'] <= 0:
        raise ProtocolError('Confirmation requires a positive preregistered effect threshold')
    _public_key(value['runner_public_key'])
    if value['trust'] != 'engineering_same_account':
        raise ProtocolError('This version has no verified external permission attestation')
    return value


def validate_submission(submission, contract_payload=None):
    _fields(submission, {'schema_version', 'kind', 'submission_id', 'identity'}, 'submission')
    version = submission['schema_version']
    if version not in {'1.0', '2.0'} or submission['kind'] != 'confirmation_submission':
        raise ProtocolError('Unsupported confirmation submission')
    value = submission['identity']
    fields = {'study_id', 'family_id', 'contract_id', 'contract_sha256',
              'source_manifest_sha256', 'hypothesis_id', 'hypothesis_version', 'design_id',
              'dev_observation_id', 'dev_observation_sha256', 'train_sha256', 'dev_sha256',
              'metric', 'evaluator_id', 'seeds', 'min_effect', 'control', 'candidate'}
    if version == '2.0':
        fields |= {'confirmation_kind', 'analysis_slices', 'dev_evidence_refs'}
    _fields(value, fields, 'submission identity')
    if submission['submission_id'] != 'SUB-' + digest(value)[:24]:
        raise ProtocolError('Submission identity mismatch')
    for name in ('study_id', 'family_id', 'contract_id', 'hypothesis_id', 'design_id', 'dev_observation_id'):
        _text(value[name], name)
    if type(value['hypothesis_version']) is not int or value['hypothesis_version'] < 1:
        raise ProtocolError('Invalid hypothesis version')
    for name in ('contract_sha256', 'source_manifest_sha256', 'dev_observation_sha256', 'train_sha256', 'dev_sha256'):
        _hash(value[name], name)
    _seeds(value['seeds'])
    if version == '2.0':
        if value['confirmation_kind'] != 'scope_boundary':
            raise ProtocolError('Unsupported v2 confirmation kind')
        from .evaluation_service import _analysis_slices
        _analysis_slices(value['analysis_slices'], value['evaluator_id'])
        refs = value['dev_evidence_refs']
        if (not isinstance(refs, list) or not 6 <= len(refs) <= 34
                or any(not isinstance(ref, str) or not ref for ref in refs)
                or len(set(refs)) != len(refs)
                or value['dev_observation_id'] not in refs):
            raise ProtocolError('Boundary submission requires complete development evidence refs')
    _finite(value['min_effect'], 'effect threshold')
    if value['min_effect'] <= 0:
        raise ProtocolError('Confirmation requires a positive effect threshold')
    evaluator = value['evaluator_id']
    if not isinstance(evaluator, str) or evaluator not in EVALUATORS or value['metric'] != EVALUATORS[evaluator]['metric']:
        raise ProtocolError('Submission metric mismatch')
    for role in ('control', 'candidate'):
        fields = {'config', 'entrypoint', 'files'}
        entry = value[role]
        if role == 'candidate':
            mode = entry.get('implementation_mode') if isinstance(entry, dict) else None
            if mode is None and version == '1.0':
                # Legacy 1.0 candidate: a generated revision carried inline.
                fields |= {'revision_id', 'revision_manifest_sha256', 'execution_changes'}
            else:
                fields.add('implementation_mode')
                if mode == 'generated_revision':
                    fields |= {'revision_id', 'revision_manifest_sha256', 'execution_changes'}
                elif mode != 'registered_implementation':
                    raise ProtocolError('Unsupported candidate implementation mode')
        _fields(entry, fields, role)
        if not isinstance(entry['config'], dict):
            raise ProtocolError('Frozen intervention must be an object')
        _code_files(entry['files'], entry['entrypoint'])
    candidate = value['candidate']
    generated = candidate.get('implementation_mode') != 'registered_implementation'
    if generated:
        if not re.fullmatch(r'REV-[0-9a-f]{24}', str(candidate['revision_id'])):
            raise ProtocolError('Candidate must identify a generated immutable revision')
        _hash(candidate['revision_manifest_sha256'], 'revision manifest')
        changes = candidate['execution_changes']
        if not isinstance(changes, dict) or set(changes) != set(candidate['files']):
            raise ProtocolError('Candidate execution change contract missing')
        for lines in changes.values():
            if not isinstance(lines, list) or any(type(line) is not int or line < 1 for line in lines) or len(lines) != len(set(lines)):
                raise ProtocolError('Invalid candidate execution lines')
        # An empty change set is not rejected here: the worker already recorded the
        # execution evidence, and line traces cannot observe deletions, condition or
        # definition-header edits, or comment edits.
    if contract_payload is not None:
        for field in ('contract_id', 'train_sha256', 'dev_sha256', 'evaluator_id', 'seeds', 'min_effect'):
            if value[field] != contract_payload[field]:
                raise ProtocolError(f'Submission differs from preregistered holdout contract: {field}')
        if version != contract_payload['schema_version']:
            raise ProtocolError('Submission version differs from holdout contract')
        if version == '2.0' and value['analysis_slices'] != contract_payload['analysis_slices']:
            raise ProtocolError('Submission slices differ from holdout contract')
    return value


def validate_ticket(ticket, contract_envelope, submission, pinned_service_key):
    contract = validate_contract(contract_envelope, pinned_service_key)
    identity = validate_submission(submission, contract)
    value = verify_envelope(ticket, pinned_service_key)
    expected = {'schema_version': '1.0', 'kind': 'holdout_ticket',
                'ticket_id': 'TKT-' + digest({'contract_id': contract['contract_id'],
                                            'submission_id': submission['submission_id']})[:24],
                'contract_id': contract['contract_id'], 'contract_sha256': digest(contract_envelope),
                'submission_id': submission['submission_id'], 'submission_sha256': digest(submission),
                'consumption_key': digest({name: contract[name] for name in
                                           ('dataset_id', 'dataset_version', 'evaluation_group')}),
                'features_sha256': contract['features_sha256']}
    _fields(value, set(expected) | {'issued_at'}, 'execution ticket')
    if identity['contract_sha256'] != digest(contract_envelope):
        raise ProtocolError('Submission contract envelope mismatch')
    for name, item in expected.items():
        if value[name] != item:
            raise ProtocolError(f'Execution ticket binding mismatch: {name}')
    _text(value['issued_at'], 'ticket timestamp')
    return value


def validate_result(result, contract_envelope, submission, pinned_service_key):
    import statistics
    contract = validate_contract(contract_envelope, pinned_service_key)
    identity = validate_submission(submission, contract)
    value = verify_envelope(result, pinned_service_key)
    version = contract['schema_version']
    fields = {'schema_version', 'kind', 'ticket_id', 'contract_id', 'contract_sha256',
              'submission_id', 'submission_sha256', 'runner_receipt_sha256',
              'scoring_code_sha256', 'service_code_sha256', 'status', 'control', 'candidate',
              'effect', 'passed', 'error_type', 'trust'}
    if version == '2.0':
        fields.add('confirmation_kind')
    _fields(value, fields, 'holdout result')
    expected = {'schema_version': version, 'kind': 'holdout_result',
                'ticket_id': 'TKT-' + digest({'contract_id': contract['contract_id'],
                                            'submission_id': submission['submission_id']})[:24],
                'contract_id': contract['contract_id'], 'contract_sha256': digest(contract_envelope),
                'submission_id': submission['submission_id'], 'submission_sha256': digest(submission),
                'scoring_code_sha256': contract['scoring_code_sha256'],
                'service_code_sha256': contract['service_code_sha256'], 'trust': contract['trust']}
    if version == '2.0':
        expected['confirmation_kind'] = identity['confirmation_kind']
    for field, expected_value in expected.items():
        if value[field] != expected_value:
            raise ProtocolError(f'Holdout result binding mismatch: {field}')
    _hash(value['runner_receipt_sha256'], 'runner receipt')
    if value['status'] == 'failed':
        if any(value[field] is not None for field in ('control', 'candidate', 'effect', 'passed')):
            raise ProtocolError('Failed confirmation cannot carry scientific scores')
        _text(value['error_type'], 'failure type')
        return value
    if value['status'] != 'succeeded' or value['error_type'] is not None:
        raise ProtocolError('Invalid holdout result status')
    metric_spec = evaluator_metric(contract['evaluator_id'])
    slice_scores = {}
    for role in ('control', 'candidate'):
        scores = value[role]
        score_fields = {'per_seed', 'mean', 'std'}
        if version == '2.0':
            score_fields.add('slices')
        _fields(scores, score_fields, f'{role} scores')
        points = scores['per_seed']
        if not isinstance(points, list) or len(points) != len(contract['seeds']):
            raise ProtocolError('Incomplete confirmation seed coverage')
        for point, seed in zip(points, contract['seeds']):
            _fields(point, {'seed', 'value'}, 'seed score')
            if type(point['seed']) is not int or point['seed'] != seed:
                raise ProtocolError('Confirmation seed mismatch')
            _finite(point['value'], 'seed score')
            if not metric_spec.accepts(point['value']):
                raise ProtocolError('Confirmation score out of range')
        values = [point['value'] for point in points]
        for name, expected_value in {'mean': statistics.mean(values),
                                     'std': statistics.stdev(values) if len(values) > 1 else 0.0}.items():
            _finite(scores[name], name)
            if scores[name] < 0 or not math.isclose(scores[name], expected_value, rel_tol=1e-12, abs_tol=1e-12):
                raise ProtocolError('Confirmation summary does not match per-seed scores')
        if version == '2.0':
            registered = contract['analysis_slices']
            if (not isinstance(scores['slices'], list)
                    or [item.get('slice_id') for item in scores['slices']]
                    != [item['slice_id'] for item in registered]):
                raise ProtocolError('Confirmation slices do not match registration')
            slice_scores[role] = {}
            for item in scores['slices']:
                _fields(item, {'slice_id', 'n', 'per_seed', 'mean', 'std'}, 'slice scores')
                if type(item['n']) is not int or item['n'] < 1:
                    raise ProtocolError('Confirmation slice size is invalid')
                points = item['per_seed']
                if not isinstance(points, list) or len(points) != len(contract['seeds']):
                    raise ProtocolError('Incomplete confirmation slice seed coverage')
                for point, seed in zip(points, contract['seeds']):
                    _fields(point, {'seed', 'value'}, 'slice seed score')
                    if point['seed'] != seed:
                        raise ProtocolError('Confirmation slice seed mismatch')
                    _finite(point['value'], 'slice seed score')
                    if not metric_spec.accepts(point['value']):
                        raise ProtocolError('Confirmation slice score out of range')
                values = [point['value'] for point in points]
                expected_stats = {'mean': statistics.mean(values),
                                  'std': statistics.stdev(values) if len(values) > 1 else 0.0}
                for name, expected_value in expected_stats.items():
                    _finite(item[name], f'slice {name}')
                    if not math.isclose(item[name], expected_value, rel_tol=1e-12, abs_tol=1e-12):
                        raise ProtocolError('Confirmation slice summary is inconsistent')
                slice_scores[role][item['slice_id']] = item['mean']
    effect = value['candidate']['mean'] - value['control']['mean']
    if EVALUATORS[contract['evaluator_id']]['metric']['direction'] == 'min':
        effect = -effect
    _finite(value['effect'], 'effect')
    passed = effect >= contract['min_effect']
    if version == '2.0' and identity['confirmation_kind'] == 'scope_boundary':
        direction = EVALUATORS[contract['evaluator_id']]['metric']['direction']
        effects = []
        for item in contract['analysis_slices']:
            slice_id = item['slice_id']
            raw = slice_scores['candidate'][slice_id] - slice_scores['control'][slice_id]
            effects.append(raw if direction == 'max' else -raw)
        passed = (abs(effect) < contract['min_effect'] and len(effects) >= 2
                  and max(effects) >= contract['min_effect'] and min(effects) < contract['min_effect'])
    if (not math.isclose(value['effect'], effect, rel_tol=1e-12, abs_tol=1e-12)
            or type(value['passed']) is not bool or value['passed'] != passed):
        raise ProtocolError('Confirmation effect or threshold verdict is inconsistent')
    return value
