"""Bind a reviewed conversation proposal to Study initialization; no model I/O.

This is an application adapter, not an autonomous experiment runner. A settled
campaign receipt establishes accounting, not whether a provider is a real model.
Mock provenance must still be labelled simulation by the campaign runner.
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from ..core import Experiment, ProtocolError, canonical, digest, file_hash, read_json, write_json
from ..domains.oscillator_trajectory import PACK
from ..sessions import AgentSessionStore
from ..task_context import load_input, redact_input
from .controller import ResearchController
from .readout import read_research_state


def preview_proposal(project, campaign_path, session_id, turn_id):
    """Read-only, exact-source preview; never invent missing hypotheses."""
    project, campaign_path = Path(project).resolve(), Path(campaign_path).resolve()
    store = AgentSessionStore.readonly(project)
    try:
        turn = store.get_turn(turn_id)
        if turn['session_id'] != session_id or turn['status'] != 'completed':
            raise ProtocolError('proposal requires a completed Turn in the explicit session')
        frozen = load_input(store, turn_id)
    finally:
        store.close()
    if frozen is None:
        raise ProtocolError('proposal has no persistent model input')
    with sqlite3.connect(campaign_path.as_uri() + '?mode=ro', uri=True) as connection:
        row = connection.execute(
            'SELECT snapshot,sha256,state,used FROM requests WHERE id=?', (turn_id,)).fetchone()
        unresolved = connection.execute("SELECT count(*) FROM requests WHERE state NOT IN ('settled','aggregate_reconciled','usage_over_reservation')").fetchone()[0]
    if (row is None or row[2] not in {'settled', 'usage_over_reservation'} or type(row[3]) is not int or row[3] < 0
            or unresolved or row[1] != digest(frozen) or json.loads(row[0]) != frozen):
        raise ProtocolError('proposal usage/input receipt is missing, changed or unresolved')
    body = turn['assistant_message']
    response_format = None
    if isinstance(body, str) and body.startswith('```json\n') and body.endswith('\n```'):
        body = body[8:-4]
        response_format = 'exact_single_json_fence; original source preserved'
    try:
        proposal = json.loads(body)
    except (TypeError, ValueError) as error:
        raise ProtocolError('proposal must be one complete JSON object; no inferred summary') from error
    required = {'kind', 'objective', 'candidates', 'hypotheses'}
    if (not isinstance(proposal, dict) or not required.issubset(proposal)
            or not set(proposal).issubset(required | {'implementation', 'initial_prediction', 'prediction_before_score'})
            or proposal['kind'] != 'oscillator_hypotheses_v1'
            or redact_input(proposal) != proposal):
        raise ProtocolError('proposal schema invalid or contains credentials')
    if 'prediction_before_score' in proposal and type(proposal['prediction_before_score']) is not bool:
        raise ProtocolError('prediction_before_score declaration must be boolean source data')
    if 'initial_prediction' in proposal:
        prediction = proposal['initial_prediction']
        if not isinstance(prediction, (str, dict)) or not prediction or (
                isinstance(prediction, str) and not prediction.strip()):
            raise ProtocolError('initial_prediction must be nonempty text or a JSON object')
        try:
            body = prediction if isinstance(prediction, str) else json.dumps(
                prediction, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        except (ValueError, TypeError, RecursionError) as error:
            raise ProtocolError('initial_prediction must be finite serializable JSON data') from error
        if len(body.encode('utf-8')) > 4096:
            raise ProtocolError('initial_prediction exceeds 4096 bytes')
    exp = Experiment(project, readonly=True)
    try:
        state = exp.verify_inputs()
    finally:
        exp.close()
    spec = state['spec']
    if state['evaluator_id'] != PACK.evaluator_id or state['phase'] != 'searching':
        raise ProtocolError('proposal requires an unconsumed oscillator project')
    scope_annotations = {}
    if response_format:
        scope_annotations['response_format'] = response_format
    if 'prediction_before_score' in proposal:
        scope_annotations['authored_declarations'] = {'prediction_before_score': proposal['prediction_before_score']}
        scope_annotations['declaration_authority'] = 'source data only; not proof of timing or execution approval'
    if proposal['objective'] != spec['objective']:
        if (not isinstance(proposal['objective'], str)
                or ''.join(proposal['objective'].split()) != ''.join(spec['objective'].split())):
            raise ProtocolError('proposal objective/candidate scope differs from preregistration')
        scope_annotations['authored_objective'] = proposal['objective']
        scope_annotations['objective_difference'] = 'whitespace_only; preregistration unchanged'
    if canonical(proposal['candidates']) != canonical(spec['candidates']):
        authored = proposal['candidates']
        registered = spec['candidates']
        if (not isinstance(authored, list) or len(authored) != len(registered)
                or not all(isinstance(item, dict) and set(item) == {'slot'} for item in registered)):
            raise ProtocolError('proposal objective/candidate scope differs from preregistration')
        for item, fixed in zip(authored, registered):
            if (not isinstance(item, dict) or not set(item).issubset({'slot', 'method', 'description'})
                    or type(item.get('slot')) is not int or item['slot'] != fixed['slot']
                    or any(not isinstance(value, str) or not value.strip()
                           or len(value.encode('utf-8')) > 4096
                           for key, value in item.items() if key != 'slot')):
                raise ProtocolError('proposal objective/candidate scope differs from preregistration')
        scope_annotations['authored_candidates'] = authored
        scope_annotations['candidate_binding'] = 'registered slots in original order; annotations only'
    hypotheses = proposal['hypotheses']
    if (not isinstance(hypotheses, list) or not 1 <= len(hypotheses) <= 6
            or len(hypotheses) != len(spec['candidates'])):
        raise ProtocolError('one authored hypothesis required per registered candidate (maximum six)')
    indexed = {}
    text_field_sources = []
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict) or set(hypothesis) != {
                'candidate_index', 'mechanism', 'applicability', 'predictions',
                'falsification', 'alternatives'}:
            raise ProtocolError('authored hypothesis fields invalid')
        index = hypothesis['candidate_index']
        if type(index) is not int or not 0 <= index < len(hypotheses) or index in indexed:
            raise ProtocolError('candidate index duplicated or outside registered scope')
        for key in ('mechanism', 'applicability'):
            if not isinstance(hypothesis[key], str) or not hypothesis[key].strip():
                raise ProtocolError('mechanism and applicability must be explicit')
        for key in ('predictions', 'falsification', 'alternatives'):
            values = hypothesis[key]
            if isinstance(values, str) and values.strip() and len(values.encode('utf-8')) <= 4096:
                text_field_sources.append({'candidate_index': index, 'field': key, 'text': values})
                hypothesis = deepcopy(hypothesis)
                hypothesis[key] = values = [values]
            if not isinstance(values, list) or not values or not all(
                    isinstance(value, str) and value.strip() for value in values):
                raise ProtocolError('predictions, refutation and alternatives must be explicit')
        indexed[index] = hypothesis
    if text_field_sources:
        scope_annotations['authored_text_fields'] = text_field_sources
        scope_annotations['text_field_binding'] = 'whole source text as one list entry; no inferred summary'
    implementation = proposal.get('implementation')
    if implementation is not None:
        if (not isinstance(implementation, dict) or set(implementation) != {
                'candidate_index', 'path', 'replacement'}
                or type(implementation['candidate_index']) is not int
                or implementation['candidate_index'] not in indexed
                or implementation['path'] != 'model.py'
                or not isinstance(implementation['replacement'], str)
                or not implementation['replacement'].strip()
                or len(implementation['replacement'].encode('utf-8')) > 65536):
            raise ProtocolError('initial implementation must bind one explicit candidate and model.py')
    result = {'kind': 'reviewed_conversation_hypotheses', 'project': str(project),
              'session_id': session_id, 'turn_id': turn_id,
              'model': turn.get('model_name'), 'input_sha256': row[1],
              'source_sha256': digest(turn['assistant_message']), 'actual_tokens': row[3],
              'input_hashes': state['input_hashes'], 'objective': spec['objective'],
              'candidates': spec['candidates'],
              'hypotheses': [indexed[index] for index in range(len(hypotheses))],
              'effect': 'Initialize Study only; no development or confirmation execution'}
    if implementation is not None:
        result['implementation'] = implementation
    if 'initial_prediction' in proposal:
        result['initial_prediction'] = proposal['initial_prediction']
    if scope_annotations:
        result['source_scope_annotations'] = scope_annotations
    return {**result, 'approval_sha256': digest(result)}


def initialize_reviewed_study(project, run_dir, campaign_path, session_id, turn_id,
                              approval_sha256):
    preview = preview_proposal(project, campaign_path, session_id, turn_id)
    if approval_sha256 != preview['approval_sha256']:
        raise ProtocolError('proposal or project changed; review again')
    run = Path(run_dir).resolve()
    receipt = run / 'agent-proposal-binding.json'
    if run.exists():
        if (not receipt.is_file() or read_json(receipt).get('preview') != preview
                or read_json(receipt).get('research_sha256') != file_hash(run / 'research.json')):
            raise ProtocolError('existing/interrupted Study cannot be overwritten or rebound')
        manifest = read_json(run / 'research.json')
        if (manifest.get('hypotheses') != preview['hypotheses']
                or manifest.get('project') != preview['project']
                or manifest.get('policy') != 'reviewed_conversation_proposal'):
            raise ProtocolError('Study does not contain the approved authored proposal')
        return read_research_state(run)

    class AuthoredProposal:
        name = 'reviewed_conversation_proposal'
        model = preview['model']

        def generate_hypotheses(self, objective, candidates):
            if objective != preview['objective'] or candidates != preview['candidates']:
                raise ProtocolError('initialization scope changed')
            return deepcopy(preview['hypotheses'])

        def choose(self, *_args):
            raise ProtocolError('Agent must explicitly propose next action; no fixed policy fallback')

    # Claim the destination before initialization; concurrent clicks must not
    # both initialize it. A crash leaves an explicit, non-overwritable site.
    try:
        run.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ProtocolError('Study initialization already started; reconcile first') from error
    ResearchController.initialize(project, run, policy=AuthoredProposal())
    write_json(receipt, {'preview': preview, 'research_sha256': file_hash(run / 'research.json')})
    return read_research_state(run)
