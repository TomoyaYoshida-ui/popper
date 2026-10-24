"""Scoop context and novelty citation checks. No network access."""
import json

import pytest

from popper.core import ProtocolError
from popper.research.scoop_context import (
    load_scoop_hits, project_scoop_hit, prompt_requires_scoop,
    scoop_request_blocked,
)
from popper.research.runtime import ResearchRuntime


def _paper(**extra):
    base = {
        'paper_id': 'P-default01',
        'source': 'arxiv', 'arxiv_id': '2401.00099', 'title': 'Scoop neighbor',
        'abstract': 'A short scoop excerpt about prior art.',
        'content': 'FULLTEXT_MARKER ' * 40,
    }
    base.update(extra)
    return base


def _write_scoop(root, *, status='completed', papers=None, selected=None, step5=None):
    """Write a minimal scoop run. By default every step2 paper is selected+fulltext."""
    scoop = root / 'scoop'
    scoop.mkdir(parents=True)
    papers = papers or [_paper()]
    (scoop / 'step7.json').write_text(
        json.dumps({'status': status, 'level': 5, 'label': 'No Overlap'}),
        encoding='utf-8')
    (scoop / 'step2.json').write_text(
        json.dumps({'papers': papers}), encoding='utf-8')
    ids = [p['paper_id'] for p in papers if isinstance(p.get('paper_id'), str)]
    if selected is None:
        selected = list(ids)
    (scoop / 'step4.json').write_text(
        json.dumps({'candidate_paper_ids': selected}), encoding='utf-8')
    if step5 is None:
        step5 = [{'paper_id': pid, 'access': 'fulltext'} for pid in selected]
    (scoop / 'step5.json').write_text(
        json.dumps({'papers': step5}), encoding='utf-8')
    return scoop


def _host():
    class _Host:
        pass
    host = _Host()
    host._reject = ResearchRuntime._reject.__get__(host)
    host._validate_refs = ResearchRuntime._validate_refs.__get__(host)
    host._validate_action = ResearchRuntime._validate_action.__get__(host)
    host._parse_proposal_action = ResearchRuntime._parse_proposal_action.__get__(host)
    return host


def _facts(*, scoop=None, literature=None):
    return {
        'candidates': [{'hypothesis_id': 'H-known', 'executed': False}],
        'raw_evidence': [{'evidence_refs': ['OBS-known']}],
        'remaining_experiments': 1,
        'scoop': scoop or [],
        'literature': literature or [],
    }


def test_projected_scoop_hit_keeps_id_source_identifier_and_excerpt_only():
    hit = project_scoop_hit(_paper())
    assert hit['id'].startswith('SCOOP-')
    assert hit['source'] == 'arxiv'
    assert hit['identifier'] == {'arxiv': '2401.00099'}
    assert hit['title'] == 'Scoop neighbor'
    assert 'short scoop excerpt' in hit['excerpt']
    assert 'FULLTEXT_MARKER' not in json.dumps(hit)
    assert project_scoop_hit(_paper())['id'] == hit['id']


def test_completed_scoop_hits_are_loaded_provisional_are_not(tmp_path):
    study = tmp_path / 'study'
    _write_scoop(study, status='completed', papers=[_paper(title='Done paper')])
    hits = load_scoop_hits(study)
    assert len(hits) == 1
    assert hits[0]['title'] == 'Done paper'
    assert hits[0]['id'].startswith('SCOOP-')
    assert hits[0].get('verified_fulltext') is True

    provisional = tmp_path / 'prov'
    _write_scoop(provisional, status='provisional', papers=[_paper(title='Prov paper')])
    assert load_scoop_hits(provisional) == []


def test_campaign_scoop_hits_are_bounded(tmp_path):
    project = tmp_path / 'project'
    scoop = project / 'integrations' / 'runs' / 'demo' / 'scoop'
    scoop.mkdir(parents=True)
    (scoop / 'step7.json').write_text(json.dumps({'status': 'completed'}), encoding='utf-8')
    papers = [_paper(title=f'Scoop {i}', arxiv_id=f'2402.{i:05d}',
                     paper_id=f'P-bound{i:02d}') for i in range(13)]
    (scoop / 'step2.json').write_text(json.dumps({'papers': papers}), encoding='utf-8')
    ids = [p['paper_id'] for p in papers]
    (scoop / 'step4.json').write_text(
        json.dumps({'candidate_paper_ids': ids}), encoding='utf-8')
    (scoop / 'step5.json').write_text(
        json.dumps({'papers': [{'paper_id': pid, 'access': 'fulltext'} for pid in ids]}),
        encoding='utf-8')
    hits = load_scoop_hits(project)
    assert len(hits) == 12
    assert all(hit['id'].startswith('SCOOP-') for hit in hits)


def test_scoop_hits_exclude_unselected_step2_papers(tmp_path):
    study = tmp_path / 'study'
    kept = _paper(paper_id='P-kept', title='Deep paper', arxiv_id='2403.00001')
    other = _paper(paper_id='P-skip', title='Search only', arxiv_id='2403.00002')
    _write_scoop(study, papers=[kept, other], selected=['P-kept'],
                 step5=[{'paper_id': 'P-kept', 'access': 'fulltext'}])
    hits = load_scoop_hits(study)
    assert len(hits) == 1
    assert hits[0]['title'] == 'Deep paper'
    assert hits[0]['verified_fulltext'] is True


def test_abstract_only_selected_not_citable(tmp_path):
    study = tmp_path / 'study'
    paper = _paper(paper_id='P-abs', title='Abstract only paper')
    _write_scoop(study, papers=[paper], selected=['P-abs'],
                 step5=[{'paper_id': 'P-abs', 'access': 'abstract_only'}])
    assert load_scoop_hits(study) == []


def test_novelty_claim_requires_citable_ids_and_rejects_empty():
    host = _host()
    known = project_scoop_hit(_paper())
    context = _facts(scoop=[known])
    bare = {
        'kind': 'novelty_conclusion', 'rationale': '查新完成，无撞车。',
    }
    with pytest.raises(ProtocolError) as missing:
        host._parse_proposal_action(bare, context)
    assert missing.value.details == [{'path': 'action.scoop_ids', 'rule': 'scoop_id_required'}]

    with pytest.raises(ProtocolError) as empty:
        host._parse_proposal_action(bare, _facts())
    assert empty.value.details == [{'path': 'scoop', 'rule': 'no_scoop_hits'}]
    assert '无撞车' in str(empty.value) or '查新' in str(empty.value)

    with pytest.raises(ProtocolError) as unknown:
        host._parse_proposal_action({
            'kind': 'novelty_conclusion', 'rationale': '依据查新',
            'scoop_ids': ['SCOOP-missing'],
        }, context)
    assert unknown.value.details == [{'path': 'action.scoop_ids', 'rule': 'unknown_scoop'}]

    accepted = host._parse_proposal_action({
        'kind': 'novelty_conclusion', 'rationale': '依据已落盘 scoop 命中',
        'scoop_ids': [known['id']],
    }, context)
    host._validate_action(accepted, context)
    assert accepted.model_dump()['scoop_ids'] == [known['id']]


def test_novelty_may_cite_literature_ids_when_present():
    from popper.research.literature_context import project_paper
    host = _host()
    lit = project_paper({
        'source': 'arxiv', 'arxiv_id': '2401.00001', 'title': 'Lit neighbor',
        'abstract': 'Literature excerpt.',
    })
    context = _facts(literature=[lit])
    accepted = host._parse_proposal_action({
        'kind': 'novelty_conclusion', 'rationale': '依据文献命中做查新说明',
        'literature_ids': [lit['id']],
    }, context)
    host._validate_action(accepted, context)
    assert accepted.literature_ids == [lit['id']]
    assert accepted.scoop_ids == []


def test_provisional_only_cannot_authorize_no_collision_prompt():
    assert prompt_requires_scoop('请给出查新结论：是否无撞车')
    assert prompt_requires_scoop('Run a scoop / prior art check')
    assert not prompt_requires_scoop('执行已登记候选')
    assert scoop_request_blocked([], [], require_scoop=True)
    assert scoop_request_blocked([], [], planning_prompt='查新是否完成')
    assert not scoop_request_blocked([{'id': 'SCOOP-1'}], [], require_scoop=True)
    assert not scoop_request_blocked([], [{'id': 'LIT-1'}], planning_prompt='无撞车吗')
    # provisional hits never enter the scoop list; empty scoop + empty literature blocks.
    assert scoop_request_blocked([], None, planning_prompt='scoop novelty check')
