"""Shared planning runtime over the existing authoritative controller/store.

The planner proposes actions and unverified interpretations. The controller owns
execution, the evaluator owns measurements and judgments, and the frozen project
owns selection. JSON reports are projections of durable receipts, never recovery
authority. No model reply is needed to enforce a deterministic experiment limit.
Retrieved literature enters context as bounded identifiers and excerpts.
It is not a reading agent and cannot authorize a review without those hits.
Completed (non-provisional) scoop hits enter the same way for novelty claims;
skipped or provisional scoop is not novelty completion.
Already-downloaded public fulltext enters as bounded ``FT-*`` excerpts only;
page or body claims must cite those ids. No open-web fetch happens here.
Study-scoped session memory (working / short-term notes) also enters context
for planning and reflection. Memory never replaces independent scores or
judgment three-states.
"""
from __future__ import annotations

from contextlib import nullcontext
import inspect
import threading

from ..core import ProtocolError, digest
from .fulltext_context import (CLAIM_FIELDS as FULLTEXT_CLAIM_FIELDS,
                               FULLTEXT_KINDS, FulltextClaim,
                               claimed_fulltext_ids, citable_fulltext_ids,
                               fulltext_claim_from, fulltext_request_blocked,
                               load_fulltext_excerpts)
from .literature_context import (CLAIM_FIELDS, LITERATURE_KINDS, LiteratureClaim,
                                 claimed_literature_ids, literature_claim_from,
                                 literature_request_blocked, load_literature_hits)
from .memory_context import project_memory_context, record_action_memory
from .resume_context import probe_last_action, project_resume_context
from .project_adapter import RegisteredProjectAdapter
from .runtime_protocol import (ExecuteCandidate, ReviseHypothesis, StopPlanning,
                               decision_instructions, parse_action, parse_assessment)
from .scoop_context import (SCOOP_CLAIM_FIELDS, SCOOP_KINDS, ScoopClaim,
                            claimed_scoop_ids, citable_novelty_ids,
                            load_scoop_hits, scoop_claim_from,
                            scoop_request_blocked)


class ResearchRuntime:
    def __init__(self, controller, *, session_id='default', profile='evidence_driven',
                 max_experiments=None, max_decisions=9):
        self.controller = controller
        self.store = controller.store
        self.study_id = controller.manifest['study_id']
        self.adapter = RegisteredProjectAdapter(controller)
        self.adapter.require_execution_support()
        self.session_id = session_id
        if not isinstance(session_id, str) or not session_id.strip():
            raise ProtocolError('Runtime session identity is required')
        if profile not in {'fixed_queue', 'generic_iteration', 'evidence_driven'}:
            raise ProtocolError('Unknown planning profile')
        self.profile = profile
        cap = self.adapter.spec['budget']
        self.max_experiments = cap if max_experiments is None else max_experiments
        if (type(self.max_experiments) is not int or not 0 <= self.max_experiments <= cap
                or type(max_decisions) is not int or max_decisions < 1):
            raise ProtocolError('Runtime limits must fit the frozen project budget')
        self.max_decisions = max_decisions
        binding = {'schema': 'research-runtime/v1', 'profile': profile,
                   'manifest_sha256': digest(controller.manifest),
                   'project_declaration_sha256': digest({'spec': self.adapter.spec, 'protocol': self.adapter.protocol}),
                   'max_experiments': self.max_experiments, 'max_decisions': max_decisions}
        old = self._record('session', 'binding')
        if old and old['body'] != binding:
            raise ProtocolError('Runtime session binding changed; cannot resume')
        if not old:
            self._write('session', 'binding', binding)

    @classmethod
    def preview_context(cls, controller, *, profile='evidence_driven'):
        """Read the same context without opening a session or writing receipts."""
        if profile not in {'fixed_queue', 'generic_iteration', 'evidence_driven'}:
            raise ProtocolError('Unknown planning profile')
        runtime = object.__new__(cls)
        runtime.controller, runtime.store = controller, controller.store
        runtime.study_id, runtime.profile = controller.manifest['study_id'], profile
        runtime.adapter = RegisteredProjectAdapter(controller)
        runtime.adapter.preflight()
        runtime.max_experiments = runtime.adapter.spec['budget']
        return runtime.context()

    def _id(self, kind, key):
        return 'RT-' + digest({'study_id': self.study_id, 'session': self.session_id,
                              'kind': kind, 'key': key})[:32]

    def _record(self, kind, key):
        return self.store.get('runtime_record', self._id(kind, key))

    def records(self, kind=None):
        return [r for r in self.store.list('runtime_record')
                if r['study_id'] == self.study_id and r['session_id'] == self.session_id
                and (kind is None or r['record_kind'] == kind)]

    def _write(self, kind, key, body):
        rid = self._id(kind, key)
        return self.store.execute_command(
            self.study_id, 'record_runtime',
            {'session_id': self.session_id, 'record_id': rid, 'record_kind': kind, 'body': body},
            idempotency_key='runtime-' + rid[3:],
            expected_version=self.store.get('study', self.study_id)['state_version'],
            actor='research_runtime', trace_id='trc_' + rid[3:])

    def executions(self):
        # All current-version independently evaluated candidates belong to the
        # study, including a worker result committed just before a planner crash.
        scope = self.adapter.primary_scope_id()
        result = []
        for observation in self.store.list('observation'):
            hid = observation['hypothesis_id']
            if hid not in self.controller.manifest['candidate_hypothesis_ids'] or observation['scope'] != scope:
                continue
            hypothesis = self.store.get('hypothesis', hid)
            run = self.store.get('run', observation['run_id'])
            if hypothesis['version'] == observation['hypothesis_version'] and run['status'] == 'succeeded':
                result.append({'hypothesis_id': hid, 'observation_id': observation['observation_id']})
        return result

    def context(self):
        project = self.adapter.describe()
        seen = {e['hypothesis_id'] for e in self.executions()}
        candidates = []
        for item in self.adapter.candidate_space():
            h = self.store.get('hypothesis', item['hypothesis_id'])
            candidates.append({**item, 'executed': item['hypothesis_id'] in seen,
                               'mechanism': h['mechanism'], 'applicability': h['applicability'],
                               'predictions': h['predictions']})
        raw, structured = [], []
        for judgment in self.store.list('judgment'):
            hid = judgment['hypothesis_id']
            if hid not in self.controller.manifest['candidate_hypothesis_ids'] or not judgment['scope'].startswith('dev'):
                continue
            if judgment['hypothesis_version'] != self.store.get('hypothesis', hid)['version']:
                continue
            evidence = judgment['evidence'] or {}
            raw.append({'hypothesis_id': hid, 'version': judgment['hypothesis_version'],
                        'evidence_refs': judgment['observation_refs'], 'scope': judgment['scope'],
                        **{k: evidence.get(k) for k in (
                            'metric', 'unit', 'aggregation', 'mean_effect', 'ci95', 'quality', 'n_units',
                            'baseline_mae', 'candidate_mae', 'baseline_mse', 'candidate_mse')}})
            structured.append({k: judgment.get(k) for k in (
                'hypothesis_id', 'hypothesis_version', 'old_judgment', 'new_judgment',
                'scope', 'reason', 'next_action', 'observation_refs')})
        if not self.adapter.protocol:
            for e in self.executions():
                o = self.store.get('observation', e['observation_id'])
                raw.append({'hypothesis_id': o['hypothesis_id'], 'version': o['hypothesis_version'],
                            'scope': o['scope'], 'evidence_refs': [o['observation_id']],
                            'metric': o['unit'], 'value': o['value'], 'quality': 'descriptive_only'})
        roots = (self.controller.run_dir, self.controller.project)
        remaining = max(0, self.max_experiments - len(seen))
        # preview_context() builds a partial runtime without a session binding.
        last_step, last_kind = (None, None)
        if getattr(self, 'session_id', None) is not None and hasattr(self, 'max_decisions'):
            last_step, last_kind = probe_last_action(
                self, max_probe=self.max_decisions)
        study = self.store.get('study', self.study_id)
        integrity = self.store.verify() if hasattr(self.store, 'verify') else {}
        result = {'project': project, 'objective': project['objective'],
                  'primary_scope_id': project['primary_scope_id'], 'scopes': project['scopes'],
                  'candidates': candidates, 'raw_evidence': raw,
                  'literature': load_literature_hits(*roots),
                  'scoop': load_scoop_hits(*roots),
                  'fulltext': load_fulltext_excerpts(*roots),
                  'memory': project_memory_context(self.controller.run_dir),
                  'resume': project_resume_context(
                      self.controller.run_dir,
                      phase=study.get('status'),
                      integrity_ok=bool(isinstance(integrity, dict) and integrity.get('ok')),
                      last_runtime_step=last_step,
                      last_action_kind=last_kind,
                      remaining_experiments=remaining),
                  'remaining_experiments': remaining}
        if self.profile == 'evidence_driven':
            result['structured_judgments'] = structured
        return result

    def _reject(self, message, path, rule):
        error = ProtocolError(message)
        error.details = [{'path': path, 'rule': rule}]
        raise error

    def _validate_refs(self, refs, context):
        known = {ref for item in context['raw_evidence'] for ref in item['evidence_refs']}
        if not set(refs) <= known:
            self._reject('Proposal references unknown or withheld evidence',
                         'action.evidence_refs', 'unknown_evidence')

    def _parse_proposal_action(self, raw, context):
        """Accept a computational action, or a literature/scoop/fulltext claim that cites stored hits."""
        fulltext_claimed = claimed_fulltext_ids(raw)
        if fulltext_claimed is not None:
            known = citable_fulltext_ids(context)
            if not known:
                self._reject('没有已下载全文摘录，不能引用论文正文或页码',
                             'fulltext', 'no_fulltext_hits')
            if not fulltext_claimed:
                self._reject('声称依据全文时必须引用已下载全文摘录的 id',
                             'action.fulltext_ids', 'fulltext_id_required')
            if not set(fulltext_claimed) <= known:
                self._reject('规划动作引用了本次上下文中不存在的全文摘录 id',
                             'action.fulltext_ids', 'unknown_fulltext')
            if isinstance(raw, dict) and raw.get('kind') in FULLTEXT_KINDS:
                return fulltext_claim_from(raw, fulltext_claimed)
            raw = {key: value for key, value in raw.items()
                   if key not in FULLTEXT_CLAIM_FIELDS}
            # Fall through so literature/scoop/compute fields on the same action still parse.
        scoop_claimed = claimed_scoop_ids(raw)
        if scoop_claimed is not None:
            known = citable_novelty_ids(context)
            if not known:
                self._reject('没有合格查新命中，不能生成查新完成或无撞车结论',
                             'scoop', 'no_scoop_hits')
            if not scoop_claimed:
                self._reject('声称依据查新时必须引用已落盘 scoop/文献命中的 id',
                             'action.scoop_ids', 'scoop_id_required')
            if not set(scoop_claimed) <= known:
                self._reject('规划动作引用了本次上下文中不存在的查新/文献 id',
                             'action.scoop_ids', 'unknown_scoop')
            if isinstance(raw, dict) and raw.get('kind') in SCOOP_KINDS:
                return scoop_claim_from(raw, scoop_claimed)
            raw = {key: value for key, value in raw.items()
                   if key not in SCOOP_CLAIM_FIELDS and key not in CLAIM_FIELDS}
            return parse_action(raw)
        claimed = claimed_literature_ids(raw)
        if claimed is not None:
            known = {item.get('id') for item in context.get('literature') or []
                     if isinstance(item, dict)}
            if not context.get('literature'):
                self._reject('没有检索命中，不能生成文献综述',
                             'literature', 'no_literature_hits')
            if not claimed:
                self._reject('声称依据文献时必须引用已检索文献的 id',
                             'action.literature_ids', 'literature_id_required')
            if not set(claimed) <= known:
                self._reject('规划动作引用了本次检索上下文中不存在的文献 id',
                             'action.literature_ids', 'unknown_literature')
        if isinstance(raw, dict) and raw.get('kind') in LITERATURE_KINDS:
            return literature_claim_from(raw, claimed or [])
        if isinstance(raw, dict) and claimed is not None:
            raw = {key: value for key, value in raw.items() if key not in CLAIM_FIELDS}
        if isinstance(raw, dict) and fulltext_claimed is not None:
            raw = {key: value for key, value in raw.items() if key not in FULLTEXT_CLAIM_FIELDS}
        return parse_action(raw)

    def _validate_action(self, action, context):
        if isinstance(action, FulltextClaim):
            known = citable_fulltext_ids(context)
            if not known:
                self._reject('没有已下载全文摘录，不能引用论文正文或页码',
                             'fulltext', 'no_fulltext_hits')
            if not set(action.fulltext_ids) <= known:
                self._reject('规划动作引用了本次上下文中不存在的全文摘录 id',
                             'action.fulltext_ids', 'unknown_fulltext')
            self._validate_refs(action.evidence_refs, context)
            return
        if isinstance(action, ScoopClaim):
            known = citable_novelty_ids(context)
            cited = set(action.scoop_ids) | set(action.literature_ids)
            if not known:
                self._reject('没有合格查新命中，不能生成查新完成或无撞车结论',
                             'scoop', 'no_scoop_hits')
            if not cited <= known:
                self._reject('规划动作引用了本次上下文中不存在的查新/文献 id',
                             'action.scoop_ids', 'unknown_scoop')
            self._validate_refs(action.evidence_refs, context)
            return
        if isinstance(action, LiteratureClaim):
            known = {item.get('id') for item in context.get('literature') or []
                     if isinstance(item, dict)}
            if not context.get('literature'):
                self._reject('没有检索命中，不能生成文献综述',
                             'literature', 'no_literature_hits')
            if not set(action.literature_ids) <= known:
                self._reject('规划动作引用了本次检索上下文中不存在的文献 id',
                             'action.literature_ids', 'unknown_literature')
            self._validate_refs(action.evidence_refs, context)
            return
        self._validate_refs(action.evidence_refs, context)
        candidates = {item['hypothesis_id']: item for item in context['candidates']}
        if isinstance(action, StopPlanning):
            return
        hid = action.candidate_id if isinstance(action, ExecuteCandidate) else action.hypothesis_id
        if hid not in candidates or candidates[hid]['executed']:
            path = 'action.candidate_id' if isinstance(action, ExecuteCandidate) else 'action.hypothesis_id'
            self._reject('Action requires an unexecuted registered candidate',
                         path, 'unexecuted_registered_candidate')
        if isinstance(action, ExecuteCandidate) and context['remaining_experiments'] == 0:
            self._reject('Frozen experiment limit reached', 'action', 'experiment_limit')
        if isinstance(action, ReviseHypothesis) and not action.evidence_refs:
            self._reject('Revision must cite independently verified evidence',
                         'action.evidence_refs', 'evidence_required')

    def _assessment(self, payload, context, step):
        try:
            assessment = parse_assessment(payload)
            if assessment is None:
                return None
            candidates = {item['hypothesis_id']: item for item in context['candidates']}
            subject = candidates.get(assessment.subject_hypothesis_id)
            if not subject or subject['version'] != assessment.hypothesis_version:
                raise ProtocolError('Assessment subject or version is not current')
            self.adapter.scope_for_observation(assessment.scope_id)
            self._validate_refs(assessment.evidence_refs, context)
            applicable = {ref for e in context['raw_evidence']
                          if e['hypothesis_id'] == assessment.subject_hypothesis_id
                          and e['scope'] == assessment.scope_id for ref in e['evidence_refs']}
            if (not set(assessment.evidence_refs) <= applicable
                    or (assessment.judgment != 'not_evaluated' and not assessment.evidence_refs)):
                raise ProtocolError('Assessment is not bound to its subject evidence')
            data = {**assessment.model_dump(), 'trust': 'unverified'}
            self._write('agent_assessment', step, data)
            return data
        except ProtocolError as error:
            self._write('assessment_rejected', step,
                        {'trust': 'unverified', 'error_type': type(error).__name__,
                         'details': getattr(error, 'details', [{'path': 'assessment', 'rule': 'evidence_binding'}])})
            return None

    def close_planner(self, status, reason):
        if status not in {'stopped', 'failed', 'exhausted', 'cancelled', 'recovery_blocked'}:
            raise ProtocolError('Unknown planner terminal status')
        if not self.records('planner_closed'):
            with self.store.transaction():
                finalist = self.adapter.select_finalist(self.executions())
                if finalist:
                    # This immutable receipt is registered in the immediately
                    # following event. Its version must refer to the committed
                    # terminal planner record, not the state preceding it.
                    receipt = finalist['selection_receipt']
                    receipt['state_version'] += 1
                    receipt['receipt_sha256'] = digest({k: v for k, v in receipt.items()
                                                        if k != 'receipt_sha256'})
                self._write('planner_closed', 'terminal',
                            {'status': status, 'reason': reason, 'finalist': finalist})
        return self.result()

    def apply_proposal(self, payload, *, step, trusted_local=False, sandboxed=False, should_cancel=None):
        """Apply a received action once; assessment rejection never drops an action."""
        previous = self._record('action_completed', step)
        if previous:
            received = self._record('response_received', step)
            if received['body']['payload_sha256'] != digest(payload):
                raise ProtocolError('Decision identity reused with another payload')
            body = previous['body']
            self._persist_action_memory(body, step)
            return body
        if self.records('planner_closed'):
            raise ProtocolError('Planning channel is closed')
        if not isinstance(payload, dict) or set(payload) - {'action', 'assessment'} or 'action' not in payload:
            raise ProtocolError('Decision envelope requires action and optional assessment')
        context = self.context()
        received = self._record('response_received', step)
        action = self._parse_proposal_action(payload['action'], context)
        if received and received['body']['payload_sha256'] != digest(payload):
            raise ProtocolError('Decision identity reused with another payload')
        recovered = None
        if received and isinstance(action, ExecuteCandidate):
            if self.store.get('hypothesis', action.candidate_id)['version'] != received['body']['hypothesis_version']:
                raise ProtocolError('Accepted action hypothesis version changed')
        if received and isinstance(action, ExecuteCandidate):
            recovered = next((e for e in self.executions() if e['hypothesis_id'] == action.candidate_id), None)
        if not recovered:
            if received and isinstance(action, ReviseHypothesis):
                current = self.store.get('hypothesis', action.hypothesis_id)
                recovered = (current['version'] == received['body']['hypothesis_version'] + 1
                    and current['mechanism'] == action.mechanism
                    and current['applicability'] == action.applicability
                    and current['predictions'] == action.predictions)
            if not recovered:
                self._validate_action(action, context)
        if not received:
            started = self._record('request_started', step)
            if started and started['body']['context_sha256'] != digest(context):
                raise ProtocolError('Research context changed while awaiting proposal')
            hid = (action.candidate_id if isinstance(action, ExecuteCandidate)
                   else action.hypothesis_id if isinstance(action, ReviseHypothesis) else None)
            body = {'payload': payload, 'payload_sha256': digest(payload),
                    'hypothesis_version': self.store.get('hypothesis', hid)['version'] if hid else None}
            self._write('response_received', step, body)
        recorded_assessment = self._record('agent_assessment', step)
        assessment = (recorded_assessment['body'] if recorded_assessment else
                      None if self._record('assessment_rejected', step) else
                      self._assessment(payload.get('assessment'), context, step))
        if should_cancel and should_cancel():
            raise ProtocolError('Planning action cancelled before execution')
        executed = None
        if recovered and isinstance(action, ExecuteCandidate):
            # A saved observation proves execution, not completion of every
            # independent comparison. The public cached path fills missing
            # judgments without rerunning or charging the experiment again.
            executed = self.controller.execute_registered_candidate(
                action.candidate_id,
                expected_version=self.store.get('study', self.study_id)['state_version'],
                trusted_local=trusted_local, sandboxed=sandboxed, should_cancel=should_cancel)
        elif isinstance(action, ExecuteCandidate):
            executed = self.controller.execute_registered_candidate(
                action.candidate_id,
                expected_version=self.store.get('study', self.study_id)['state_version'],
                trusted_local=trusted_local, sandboxed=sandboxed, should_cancel=should_cancel)
        elif isinstance(action, ReviseHypothesis) and not recovered:
            self.controller.revise_registered_hypothesis(action.hypothesis_id,
                mechanism=action.mechanism, applicability=action.applicability,
                predictions=action.predictions, evidence_refs=action.evidence_refs,
                expected_version=self.store.get('study', self.study_id)['state_version'])
        result = {'action': action.model_dump(), 'assessment': assessment,
                  'observation_id': executed['observation']['observation_id'] if executed else None}
        self._write('action_completed', step, result)
        self._persist_action_memory(result, step)
        if isinstance(action, StopPlanning):
            self.close_planner('stopped', 'agent_stop')
        return result

    def _persist_action_memory(self, body, step):
        """Session notes only; never written into observations or judgments."""
        action = body.get('action') if isinstance(body, dict) else None
        if not isinstance(action, dict):
            return
        rejection = None
        if self._record('assessment_rejected', step) and body.get('assessment') is None:
            rejection = 'assessment rejected: not bound to verified evidence'
        judgments = None
        observation_id = body.get('observation_id')
        hid = action.get('candidate_id') or action.get('hypothesis_id')
        if observation_id and hid:
            judgments = [
                j for j in self.store.list('judgment')
                if j.get('hypothesis_id') == hid]
        record_action_memory(
            self.controller.run_dir, study_id=self.study_id,
            action=action, assessment=body.get('assessment'),
            observation_id=observation_id, judgments=judgments,
            rejection=rejection)

    def run(self, client=None, *, candidate_order=None, trusted_local=False,
            sandboxed=False, should_cancel=None, on_progress=None,
            require_literature=False, require_scoop=False, require_fulltext=False,
            planning_prompt=None):
        if self.records('planner_closed'):
            return self.result()
        self.controller.prepare_registered_baseline(trusted_local=trusted_local,
            sandboxed=sandboxed, should_cancel=should_cancel)
        for step in range(self.max_decisions):
            if self._record('action_completed', step):
                continue
            if should_cancel and should_cancel():
                return self.close_planner('cancelled', 'cancelled')
            context = self.context()
            started = self._record('request_started', step)
            received = self._record('response_received', step)
            if context['remaining_experiments'] == 0 and received is None:
                return self.close_planner('stopped', 'experiment_limit')
            if started and not received:
                return self.close_planner('recovery_blocked', 'request_started_without_durable_response')
            try:
                if received is None and fulltext_request_blocked(
                        context.get('fulltext'), require_fulltext=require_fulltext,
                        planning_prompt=planning_prompt):
                    self._reject('没有已下载全文摘录，不能引用论文正文或页码',
                                 'fulltext', 'no_fulltext_hits')
                if received is None and scoop_request_blocked(
                        context.get('scoop'), context.get('literature'),
                        require_scoop=require_scoop, planning_prompt=planning_prompt):
                    self._reject('没有合格查新命中，不能生成查新完成或无撞车结论',
                                 'scoop', 'no_scoop_hits')
                if received is None and literature_request_blocked(
                        context.get('literature'), require_literature=require_literature,
                        planning_prompt=planning_prompt):
                    self._reject('没有检索命中，不能生成文献综述',
                                 'literature', 'no_literature_hits')
                if received:
                    payload = received['body']['payload']
                elif client is None:
                    order = candidate_order or [c['hypothesis_id'] for c in context['candidates']]
                    seen = {e['hypothesis_id'] for e in self.executions()}
                    hid = next((h for h in order if h not in seen), None)
                    action = ({'kind': 'execute_candidate', 'candidate_id': hid,
                               'rationale': 'Registered deterministic queue', 'evidence_refs': []}
                              if hid else {'kind': 'stop_planning', 'rationale': 'No remaining candidate', 'evidence_refs': []})
                    payload = {'action': action, 'assessment': None}
                else:
                    key = self._id('request_started', step)
                    self._write('request_started', step, {'step': step, 'context_sha256': digest(context),
                                                         'logical_request_id': key})
                    cancel = _CallbackCancellation(should_cancel)
                    if getattr(client, 'decide', None) is not None:
                        def persist(event):
                            kind = event.get('kind')
                            if kind not in {'model_response_received', 'model_response_rejected', 'model_output_accepted'}:
                                return
                            self._write(kind, event['event_id'], event)
                        payload = client.decide(
                            context, validate_action=lambda action: self._validate_action(action, context),
                            on_event=persist, logical_request_id=key, on_progress=on_progress,
                            cancel_event=cancel)
                    else:
                        operation = (client.logical_operation(key, max_attempts=2, cancel_event=cancel)
                                     if inspect.getattr_static(client, 'logical_operation', None) is not None else nullcontext())
                        with operation:
                            prompt = (planning_prompt if isinstance(planning_prompt, str)
                                      and planning_prompt.strip() else decision_instructions())
                            for repair in range(2):
                                request = context if repair == 0 else {'context': context, 'validation_errors': feedback}
                                payload = client(prompt, request,
                                                 logical_request_id=key, on_progress=on_progress,
                                                 cancel_event=cancel)
                                try:
                                    if not isinstance(payload, dict) or set(payload) - {'action', 'assessment'}:
                                        raise ProtocolError('Decision envelope fields are invalid')
                                    action = self._parse_proposal_action(payload.get('action'), context)
                                    self._validate_action(action, context)
                                    break
                                except ProtocolError as error:
                                    details = getattr(error, 'details', None) or []
                                    if any(isinstance(item, dict) and item.get('rule') in {
                                            'no_literature_hits', 'no_scoop_hits',
                                            'no_fulltext_hits'}
                                           for item in details):
                                        raise
                                    if repair:
                                        raise
                                    feedback = getattr(error, 'details', [{'path': 'action', 'rule': 'registered_action_or_evidence'}])
                self.apply_proposal(payload, step=step, trusted_local=trusted_local,
                    sandboxed=sandboxed, should_cancel=should_cancel)
                if self.records('planner_closed'):
                    return self.result()
                if len({e['hypothesis_id'] for e in self.executions()}) >= self.max_experiments:
                    return self.close_planner('stopped', 'experiment_limit')
            except Exception as error:
                details = getattr(error, 'details', None) or []
                if any(isinstance(item, dict) and item.get('rule') == 'no_fulltext_hits'
                       for item in details):
                    if on_progress:
                        on_progress({'status': 'failed', 'category': 'no_fulltext_hits'})
                    return self.close_planner('failed', str(error))
                if any(isinstance(item, dict) and item.get('rule') == 'no_scoop_hits'
                       for item in details):
                    if on_progress:
                        on_progress({'status': 'failed', 'category': 'no_scoop_hits'})
                    return self.close_planner('failed', str(error))
                if any(isinstance(item, dict) and item.get('rule') == 'no_literature_hits'
                       for item in details):
                    if on_progress:
                        on_progress({'status': 'failed', 'category': 'no_literature_hits'})
                    return self.close_planner('failed', str(error))
                category = getattr(error, 'category', None) or 'planner_or_execution_failure'
                if on_progress:
                    on_progress({'status': 'failed', 'category': category})
                exhausted = category in {'request_budget_exhausted', 'usage_unresolved', 'attempt_budget_exhausted'}
                status = ('recovery_blocked' if category == 'request_recovery_blocked'
                          else 'cancelled' if category == 'cancelled'
                          else 'exhausted' if exhausted else 'failed')
                return self.close_planner(status, category)
        return self.close_planner('exhausted', 'decision_limit')

    def result(self):
        final = self.records('planner_closed')
        planner = ({k: final[-1]['body'][k] for k in ('status', 'reason')}
                   if final else {'status': 'active', 'reason': None})
        executions = self.executions()
        finalist = self.adapter.select_finalist(executions)
        if final:
            frozen = final[-1]['body']['finalist']
            if (frozen is None) != (finalist is None):
                raise ProtocolError('Frozen planner selection no longer matches registered facts')
            if frozen:
                receipt = finalist['selection_receipt']
                receipt['state_version'] = frozen['selection_receipt']['state_version']
                receipt['receipt_sha256'] = digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'})
                if receipt != frozen['selection_receipt']:
                    raise ProtocolError('Frozen planner evidence identity changed')
            finalist = frozen
        assessments = [r['body'] for r in self.records('agent_assessment')]
        return {'schema': 'research-runtime-result/v1', 'session_id': self.session_id,
                'planner': planner,
                'development': {'status': ('completed' if planner['status'] == 'stopped' else 'partial')
                                if executions else 'not_completed', 'executions': executions},
                'finalist': finalist, 'agent_assessments': assessments,
                'assessment_rejections': len(self.records('assessment_rejected')),
                'scientific_judgments': self.store.list('judgment'),
                'integrity': self.store.verify()}


class _CallbackCancellation:
    """Cancellation event interface accepted by the existing network lifecycle."""
    def __init__(self, callback):
        self.callback = callback
        self.event = threading.Event()

    def is_set(self):
        return self.event.is_set() or bool(self.callback and self.callback())

    def wait(self, timeout=None):
        if timeout is None:
            timeout = .1
        import time
        deadline = time.monotonic() + timeout
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.event.wait(min(.05, remaining))
        return True
