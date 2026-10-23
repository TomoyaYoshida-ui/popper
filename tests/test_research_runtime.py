import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from popper.core import ProtocolError, initialize
from popper.research.controller import ResearchController
from popper.research.runtime import ResearchRuntime
from popper.capabilities import extra_available

pytestmark = pytest.mark.skipif(not extra_available('ml'), reason='ml extra required for quadratic execution')


@pytest.fixture
def controller(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'examples' / 'quadratic'
    project = tmp_path / 'project'
    shutil.copytree(source, project, ignore=shutil.ignore_patterns('.popper', '__pycache__'))
    initialize(project)
    ResearchController.initialize(project, tmp_path / 'research', budget_cap=6)
    c = ResearchController(tmp_path / 'research')
    yield c
    c.close()


def execute(hid):
    return {'action': {'kind': 'execute_candidate', 'candidate_id': hid,
                       'rationale': 'Frozen candidate test', 'evidence_refs': []}, 'assessment': None}


def stop(refs=()):
    return {'action': {'kind': 'stop_planning', 'rationale': 'Evidence collected',
                       'evidence_refs': list(refs)}, 'assessment': None}


def test_action_rejections_name_safe_rules_without_values():
    from popper.research.runtime_protocol import parse_action
    runtime = controller_free_runtime()
    facts = {'candidates': [{'hypothesis_id': 'H-known', 'executed': False}],
             'raw_evidence': [{'evidence_refs': ['OBS-known']}], 'remaining_experiments': 1}

    def reject(payload, path, rule):
        with pytest.raises(ProtocolError) as failure:
            runtime._validate_action(parse_action(payload), facts)
        assert failure.value.details == [{'path': path, 'rule': rule}]
        assert 'confidential_fixture_value' not in str(failure.value)

    reject({'kind': 'revise_hypothesis', 'hypothesis_id': 'H-known', 'mechanism': 'm',
            'applicability': 'scope', 'predictions': ['p'], 'rationale': 'r', 'evidence_refs': []},
           'action.evidence_refs', 'evidence_required')
    reject({'kind': 'revise_hypothesis', 'hypothesis_id': 'H-known', 'mechanism': 'm',
            'applicability': 'scope', 'predictions': ['p'], 'rationale': 'r',
            'evidence_refs': ['confidential_fixture_value']},
           'action.evidence_refs', 'unknown_evidence')
    reject({'kind': 'execute_candidate', 'candidate_id': 'confidential_fixture_value',
            'rationale': 'r', 'evidence_refs': []},
           'action.candidate_id', 'unexecuted_registered_candidate')
    reject({'kind': 'revise_hypothesis', 'hypothesis_id': 'confidential_fixture_value',
            'mechanism': 'm', 'applicability': 'scope', 'predictions': ['p'], 'rationale': 'r',
            'evidence_refs': ['OBS-known']},
           'action.hypothesis_id', 'unexecuted_registered_candidate')
    facts['remaining_experiments'] = 0
    reject({'kind': 'execute_candidate', 'candidate_id': 'H-known', 'rationale': 'r', 'evidence_refs': []},
           'action', 'experiment_limit')


def controller_free_runtime():
    class _Host:
        pass
    host = _Host()
    host._reject = ResearchRuntime._reject.__get__(host)
    host._validate_refs = ResearchRuntime._validate_refs.__get__(host)
    host._validate_action = ResearchRuntime._validate_action.__get__(host)
    return host


def test_limit_stops_without_requiring_an_extra_model_message(controller):
    runtime = controller.planning_runtime(max_experiments=1, max_decisions=1)
    calls = []
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    def model(*args, **kwargs):
        calls.append(kwargs['logical_request_id'])
        return execute(hid)
    result = runtime.run(model, trusted_local=True)
    assert len(calls) == 1
    assert result['planner'] == {'status': 'stopped', 'reason': 'experiment_limit'}
    assert result['finalist']['hypothesis_id'] == hid
    assert result['agent_assessments'] == []
    assert result['integrity']['ok']
    assert controller.exp.state()['phase'] == 'searching'


def test_stop_can_assess_an_evaluated_subject_without_execution_target(controller):
    runtime = controller.planning_runtime(max_experiments=2)
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    first = runtime.apply_proposal(execute(hid), step=0, trusted_local=True)
    reply = stop([first['observation_id']])
    reply['assessment'] = {'subject_hypothesis_id': hid, 'hypothesis_version': 1,
        'scope_id': 'dev', 'judgment': 'inconclusive', 'rationale': 'Descriptive metrics only',
        'evidence_refs': [first['observation_id']]}
    runs = len(controller.store.list('run'))
    runtime.apply_proposal(reply, step=1, trusted_local=True)
    result = runtime.result()
    assert len(controller.store.list('run')) == runs
    assert result['planner']['status'] == 'stopped'
    assert result['agent_assessments'][0]['trust'] == 'unverified'
    assert all(j['new_judgment'] != 'supported' for j in result['scientific_judgments'])
    assert result['integrity']['ok']


def test_bad_model_assessment_does_not_erase_or_invalidate_valid_action(controller):
    runtime = controller.planning_runtime(max_experiments=1)
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    reply = execute(hid)
    reply['assessment'] = {'score': 999, 'judgment': 'supported'}
    runtime.apply_proposal(reply, step=0, trusted_local=True)
    result = runtime.result()
    assert result['assessment_rejections'] == 1
    assert result['agent_assessments'] == []
    assert result['finalist']['hypothesis_id'] == hid
    assert result['integrity']['ok']


def test_planner_failure_retains_verified_evidence_and_audits_failure(controller):
    runtime = controller.planning_runtime(max_experiments=2)
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    replies = iter([execute(hid), {'action': {'kind': 'stop_planning', 'candidate_id': hid}},
                    {'action': {'kind': 'stop_planning', 'candidate_id': hid}}])
    result = runtime.run(lambda *a, **kw: next(replies), trusted_local=True)
    assert result['planner']['status'] == 'failed'
    assert result['development']['status'] == 'partial'
    assert result['finalist']['hypothesis_id'] == hid
    assert len(result['development']['executions']) == 1
    assert result['integrity']['ok']


def test_completed_action_replays_once_and_payload_collision_is_refused(controller):
    runtime = controller.planning_runtime()
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    first = runtime.apply_proposal(execute(hid), step=0, trusted_local=True)
    counts = {k: len(controller.store.list(k)) for k in ('run', 'observation', 'runtime_record')}
    assert first == runtime.apply_proposal(execute(hid), step=0, trusted_local=True)
    assert counts == {k: len(controller.store.list(k)) for k in counts}
    with pytest.raises(ProtocolError, match='identity'):
        runtime.apply_proposal(stop(), step=0, trusted_local=True)


def test_resume_after_execution_before_action_receipt_recovers_saved_facts(controller):
    runtime = controller.planning_runtime(max_experiments=2)
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    original = runtime._write
    def crash(kind, key, body):
        if kind == 'action_completed':
            raise RuntimeError('crash after independently evaluated observation')
        return original(kind, key, body)
    with patch.object(runtime, '_write', side_effect=crash):
        with pytest.raises(RuntimeError):
            runtime.apply_proposal(execute(hid), step=0, trusted_local=True)
    before = len(controller.store.list('run'))
    resumed = controller.planning_runtime(max_experiments=2)
    recovered = resumed.apply_proposal(execute(hid), step=0, trusted_local=True)
    assert recovered['observation_id']
    assert before == len(controller.store.list('run'))
    assert resumed.result()['integrity']['ok']


def test_ambiguous_model_send_is_not_replayed_on_restart(controller):
    runtime = controller.planning_runtime()
    runtime._write('request_started', 0, {'context_sha256': 'interrupted', 'logical_request_id': 'already-sent'})
    def forbidden(*args, **kwargs):
        pytest.fail('ambiguous request must not resend')
    result = runtime.run(forbidden, trusted_local=True)
    assert result['planner']['status'] == 'recovery_blocked'
    assert not result['development']['executions']
    assert result['integrity']['ok']


def test_revision_is_append_only_and_session_survives_version_change(controller):
    runtime = controller.planning_runtime(max_experiments=2)
    hid, target = controller.manifest['candidate_hypothesis_ids'][:2]
    first = runtime.apply_proposal(execute(hid), step=0, trusted_local=True)
    payload = {'action': {'kind': 'revise_hypothesis', 'hypothesis_id': target,
        'mechanism': 'Registered polynomial representation', 'applicability': 'Frozen development scope',
        'predictions': ['Meet the unchanged registered prediction'], 'rationale': 'Compare a different frozen candidate',
        'evidence_refs': [first['observation_id']]}, 'assessment': None}
    runtime.apply_proposal(payload, step=1, trusted_local=True)
    resumed = controller.planning_runtime(max_experiments=2)
    assert controller.store.get('hypothesis', target)['version'] == 2
    assert resumed.result()['integrity']['ok']
    with pytest.raises(ProtocolError):
        resumed.apply_proposal({**payload, 'action': {**payload['action'], 'hypothesis_id': hid}},
                               step=2, trusted_local=True)
    assert controller.store.get('hypothesis', hid)['version'] == 1


def test_ablation_context_has_identical_raw_facts_and_no_session_writes(controller):
    runtime = controller.planning_runtime()
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    runtime.apply_proposal(execute(hid), step=0, trusted_local=True)
    cursor = controller.store.event_cursor()
    plain = ResearchRuntime.preview_context(controller, profile='generic_iteration')
    rich = ResearchRuntime.preview_context(controller, profile='evidence_driven')
    assert plain == {k: v for k, v in rich.items() if k != 'structured_judgments'}
    assert controller.store.event_cursor() == cursor
    assert 'test.json' not in json.dumps(plain)


def _write_hits(controller):
    folder = controller.run_dir / 'literature'
    folder.mkdir(parents=True)
    (folder / 'search-fixture.json').write_text(json.dumps({'papers': [{
        'source': 'arxiv', 'arxiv_id': '2401.00001', 'title': 'Fixture neighbor',
        'abstract': 'A short excerpt about the registered method.',
        'content': 'FULLTEXT_MARKER should not enter context.',
    }]}), encoding='utf-8')


def test_planning_context_includes_retrieved_literature(controller):
    _write_hits(controller)
    context = ResearchRuntime.preview_context(controller)
    assert len(context['literature']) == 1
    hit = context['literature'][0]
    assert hit['id'].startswith('LIT-')
    assert hit['source'] == 'arxiv'
    assert hit['identifier'] == {'arxiv': '2401.00001'}
    assert hit['title'] == 'Fixture neighbor'
    assert 'short excerpt' in hit['excerpt']
    assert 'FULLTEXT_MARKER' not in json.dumps(context['literature'])


def test_planning_context_includes_study_memory_and_writes_persist(controller):
    from popper.research.memory_context import record_memory_entry

    empty = ResearchRuntime.preview_context(controller)
    assert empty['memory']['schema'] == 'research-memory-context/v1'
    assert empty['memory']['trust'] == 'unverified_session_notes'
    assert empty['memory']['entries'] == []

    runtime = controller.planning_runtime(max_experiments=1)
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    runtime.apply_proposal(execute(hid), step=0, trusted_local=True)
    after = runtime.context()
    assert after['memory']['entries']
    kinds = {e['kind'] for e in after['memory']['entries']}
    assert 'judgment_outcome' in kinds
    assert all(e['id'].startswith('MEM-') for e in after['memory']['entries'])
    # Next context sees the same durable notes (short-term summary under run_dir/memory).
    again = ResearchRuntime.preview_context(controller)
    assert {e['id'] for e in again['memory']['entries']} == {
        e['id'] for e in after['memory']['entries']}

    record_memory_entry(
        controller.run_dir, 'rejection_reason', 'fixture rejection note',
        study_id=controller.manifest['study_id'])
    noted = runtime.context()
    assert any(e['kind'] == 'rejection_reason' for e in noted['memory']['entries'])


def test_memory_ids_cannot_serve_as_observation_scores(controller):
    from popper.research.memory_context import record_memory_entry

    record_memory_entry(
        controller.run_dir, 'action_note', 'not a measurement',
        study_id=controller.manifest['study_id'])
    runtime = controller.planning_runtime(max_experiments=1)
    context = runtime.context()
    mem_ids = [e['id'] for e in context['memory']['entries']]
    assert mem_ids
    # Memory never appears as raw evidence / scores.
    blob = json.dumps(context['memory'])
    assert 'mean_effect' not in blob
    assert 'baseline_mae' not in blob
    for item in context['raw_evidence']:
        for ref in item['evidence_refs']:
            assert not str(ref).startswith('MEM-')
    # Citing a memory id as evidence is rejected like any unknown observation.
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    with pytest.raises(ProtocolError) as failure:
        runtime.apply_proposal({
            'action': {
                'kind': 'revise_hypothesis', 'hypothesis_id': hid,
                'mechanism': 'm', 'applicability': 'a', 'predictions': ['p'],
                'rationale': 'r', 'evidence_refs': mem_ids[:1],
            },
            'assessment': None,
        }, step=0, trusted_local=True)
    assert failure.value.details == [
        {'path': 'action.evidence_refs', 'rule': 'unknown_evidence'}]
    with pytest.raises(ProtocolError, match='evaluation fields'):
        record_memory_entry(
            controller.run_dir, 'action_note',
            {'summary': 'x', 'mean_effect': 0.5},
            study_id=controller.manifest['study_id'])


def test_registered_execution_ignores_literature_hits(controller):
    _write_hits(controller)
    runtime = controller.planning_runtime(max_experiments=1)
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    result = runtime.apply_proposal(execute(hid), step=0, trusted_local=True)
    assert result['observation_id']
    assert result['action']['kind'] == 'execute_candidate'
    assert 'literature_ids' not in result['action']


def test_literature_review_rejects_unknown_id_without_execution(controller):
    _write_hits(controller)
    runtime = controller.planning_runtime(max_experiments=1)
    context = runtime.context()
    before = len(controller.store.list('run'))
    with pytest.raises(ProtocolError) as failure:
        runtime.apply_proposal({'action': {
            'kind': 'literature_review', 'rationale': '文献综述',
            'literature_ids': ['LIT-missing'],
        }, 'assessment': None}, step=0, trusted_local=True)
    assert failure.value.details == [{'path': 'action.literature_ids', 'rule': 'unknown_literature'}]
    assert before == len(controller.store.list('run'))
    accepted = runtime.apply_proposal({'action': {
        'kind': 'literature_review', 'rationale': '摘录已覆盖该机制。',
        'literature_ids': [context['literature'][0]['id']],
    }, 'assessment': None}, step=0, trusted_local=True)
    assert accepted['observation_id'] is None
    assert accepted['action']['literature_ids'] == [context['literature'][0]['id']]


def test_novelty_conclusion_cites_literature_or_scoop_and_blocks_without_hits(controller):
    runtime = controller.planning_runtime(max_experiments=1)
    with pytest.raises(ProtocolError) as empty:
        runtime.apply_proposal({'action': {
            'kind': 'novelty_conclusion', 'rationale': '查新完成，无撞车',
            'scoop_ids': ['SCOOP-x'],
        }, 'assessment': None}, step=0, trusted_local=True)
    assert empty.value.details == [{'path': 'scoop', 'rule': 'no_scoop_hits'}]

    _write_hits(controller)
    runtime2 = controller.planning_runtime(session_id='novelty-lit', max_experiments=1)
    lit_id = runtime2.context()['literature'][0]['id']
    accepted = runtime2.apply_proposal({'action': {
        'kind': 'novelty_conclusion', 'rationale': '依据已检索文献说明近邻',
        'literature_ids': [lit_id],
    }, 'assessment': None}, step=0, trusted_local=True)
    assert accepted['action']['literature_ids'] == [lit_id]
    assert accepted['observation_id'] is None


def test_required_literature_with_no_hits_does_not_call_the_model(controller):
    runtime = controller.planning_runtime(max_experiments=1)
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError('model must not be called')

    result = runtime.run(forbidden, trusted_local=True, require_literature=True)
    assert calls == []
    assert result['planner'] == {
        'status': 'failed', 'reason': '没有检索命中，不能生成文献综述'}

    prompted = controller.planning_runtime(session_id='neighbor-prompt', max_experiments=1)
    prompted_calls = []

    def also_forbidden(*_args, **_kwargs):
        prompted_calls.append(1)
        raise AssertionError('model must not be called')

    prompted_result = prompted.run(
        also_forbidden, trusted_local=True, planning_prompt='请根据近邻工作回答')
    assert prompted_calls == []
    assert '没有检索命中' in prompted_result['planner']['reason']

    scoop_prompted = controller.planning_runtime(session_id='scoop-prompt', max_experiments=1)
    scoop_calls = []

    def scoop_forbidden(*_args, **_kwargs):
        scoop_calls.append(1)
        raise AssertionError('model must not be called')

    scoop_result = scoop_prompted.run(
        scoop_forbidden, trusted_local=True, planning_prompt='请给出查新结论：是否无撞车')
    assert scoop_calls == []
    assert '查新' in scoop_result['planner']['reason']
    assert scoop_result['planner']['status'] == 'failed'


def test_required_literature_with_hits_still_executes_registered_candidate(controller):
    _write_hits(controller)
    runtime = controller.planning_runtime(max_experiments=1)
    hid = controller.manifest['candidate_hypothesis_ids'][1]
    calls = []

    def model(*_args, **_kwargs):
        calls.append(1)
        return execute(hid)

    result = runtime.run(model, trusted_local=True, require_literature=True)
    assert calls == [1]
    assert result['planner']['status'] == 'stopped'
    assert result['finalist']['hypothesis_id'] == hid


def test_missing_scope_protocol_rejected_before_baseline_side_effect(controller):
    with patch('popper.research.project_adapter.RegisteredProjectAdapter.preflight',
               side_effect=ProtocolError('invalid frozen mapping')):
        with pytest.raises(ProtocolError):
            controller.execute_registered_candidate(controller.manifest['candidate_hypothesis_ids'][0],
                expected_version=controller.store.get('study', controller.manifest['study_id'])['state_version'],
                trusted_local=True)
    assert not controller.store.list('run')
