"""Wall-clock labels cannot determine execution or conversation causality."""
import sqlite3
from unittest.mock import patch

import pytest

from popper.core import ProtocolError
from popper.sessions import AgentSessionStore


def settle_tool(store,session,turn,key):
    call,_=store.propose_tool_call(session,turn,'research.status',{'study_id':'S-test'},
        'read',key,actor='agent',trace_id='trc_causal_tool')
    store.claim_tool_call(call['call_id'])
    store.finish_tool_call(call['call_id'],result={'status':'active'})
    return call


def test_clock_rollback_keeps_six_turn_chain_and_readonly_restart(tmp_path):
    store=AgentSessionStore(tmp_path)
    session=store.default_session(create=True,model='synthetic')['session_id']
    clock={'value':'2026-10-09T02:36:01.507008+00:00'}
    with patch('popper.sessions._now',side_effect=lambda:clock['value']):
        parent,_=store.start_turn(session,'Root request','causal-root-request',
            actor='local_user',trace_id='trc_causal_root')
        expected=[parent]
        current=parent
        for step in range(5):
            store.complete_turn(current['turn_id'],f'Proposal {step}')
            settle_tool(store,session,current['turn_id'],f'causal-tool-{step}')
            clock['value']='2026-10-09T02:36:01.409008+00:00'
            child,_,replayed=store.continue_after_tools(current['turn_id'],
                trace_id='trc_causal_continue',token_reservation=100)
            assert not replayed
            expected.append(child);current=child
        store.complete_turn(current['turn_id'],'Sixth real turn completed')
    assert expected[1]['created']<expected[0]['created']
    assert [t['turn_id'] for t in store.turns(session)]==[t['turn_id'] for t in expected]
    assert len(store.queued_agent_jobs())==5
    assert [j['turn_id'] for j in store.agent_jobs(session)]==[t['turn_id'] for t in expected[1:]]
    replay,_,duplicate=store.continue_after_tools(parent['turn_id'],trace_id='trc_causal_replay',token_reservation=100)
    assert duplicate and replay['turn_id']==expected[1]['turn_id']
    with pytest.raises(ProtocolError,match='已继续'):
        settle_tool(store,session,parent['turn_id'],'causal-stale-tool')
    assert store.verify_turn_events()['ok']
    assert store.verify_tool_events()['ok']
    store.close()
    database=tmp_path/'.popper'/'sessions.sqlite'
    before=database.read_bytes()
    readonly=AgentSessionStore.readonly(tmp_path)
    try:
        assert [t['turn_id'] for t in readonly.turns(session)]==[t['turn_id'] for t in expected]
        assert readonly.turns(session)[1]['created']==expected[1]['created']
        assert readonly.model_messages(session)[-1]['content']=='Sixth real turn completed'
    finally:readonly.close()
    assert database.read_bytes()==before


def test_newer_user_request_blocks_old_tool_continuation_even_if_clock_is_earlier(tmp_path):
    store=AgentSessionStore(tmp_path)
    try:
        session=store.default_session(create=True)['session_id']
        with patch('popper.sessions._now',return_value='2026-10-09T02:36:01.507008+00:00'):
            old,_=store.start_turn(session,'Old request','causal-old-request',actor='local_user',trace_id='trc_causal_old')
            store.complete_turn(old['turn_id'],'Read proposal')
            settle_tool(store,session,old['turn_id'],'causal-old-tool')
        with patch('popper.sessions._now',return_value='2026-10-09T02:36:01.409008+00:00'):
            newer,_=store.start_turn(session,'New user request','causal-new-request',actor='local_user',trace_id='trc_causal_new')
        assert store.turns(session)[-1]['turn_id']==newer['turn_id']
        with pytest.raises(ProtocolError,match='后续 turn'):
            store.continue_after_tools(old['turn_id'],trace_id='trc_causal_stale')
    finally:store.close()


def test_tool_proposals_follow_creation_events_and_never_completion_time(tmp_path):
    store=AgentSessionStore(tmp_path)
    try:
        session=store.default_session(create=True)['session_id']
        turn,_=store.start_turn(session,'Two tools','causal-tools-request',actor='local_user',trace_id='trc_causal_tools')
        store.complete_turn(turn['turn_id'],'Two ordered proposals')
        calls=[]
        for n,stamp in enumerate(('2026-10-09T02:36:01.507008+00:00','2026-10-09T02:36:01.409008+00:00')):
            with patch('popper.sessions._now',return_value=stamp):
                call,_=store.propose_tool_call(session,turn['turn_id'],'research.status',{'study_id':f'S-{n}'},
                    'read',f'causal-multi-tool-{n}',actor='agent',trace_id='trc_causal_tool')
                calls.append(call);store.claim_tool_call(call['call_id'])
        assert [c['call_id'] for c in store.running_tool_calls()]==[c['call_id'] for c in calls]
        for call in reversed(calls):store.finish_tool_call(call['call_id'],result={'status':'active'})
        assert [c['call_id'] for c in store.tool_calls(session)]==[c['call_id'] for c in calls]
        tools=[m for m in store.model_messages(session) if m['role']=='tool']
        assert [m['tool_call_id'] for m in tools]==[c['call_id'] for c in calls]
    finally:store.close()


def test_legacy_rows_without_events_keep_insertion_order_without_backfill(tmp_path):
    store=AgentSessionStore(tmp_path)
    session=store.default_session(create=True)['session_id']
    legacy=[]
    with patch.object(store,'_append_turn_event'):
        for n,stamp in enumerate(('2026-10-09T02:36:01.507008+00:00','2026-10-09T02:36:01.409008+00:00')):
            with patch('popper.sessions._now',return_value=stamp):
                turn,_=store.start_turn(session,f'Legacy {n}',f'causal-legacy-request-{n}',actor='legacy',trace_id='trc_causal_legacy')
                store.complete_turn(turn['turn_id'],f'Legacy result {n}');legacy.append(turn)
    new,_=store.start_turn(session,'Event-backed request','causal-event-request',actor='local_user',trace_id='trc_causal_event')
    store.complete_turn(new['turn_id'],'Event-backed reply')
    store.close();database=tmp_path/'.popper'/'sessions.sqlite';before=database.read_bytes()
    reader=AgentSessionStore.readonly(tmp_path)
    try:assert [t['turn_id'] for t in reader.turns(session)]==[t['turn_id'] for t in legacy]+[new['turn_id']]
    finally:reader.close()
    assert before==database.read_bytes()
    with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as conn:
        assert conn.execute('SELECT COUNT(*) FROM turn_events').fetchone()[0]==2
