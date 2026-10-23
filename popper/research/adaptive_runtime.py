"""Append-only v2 research using the existing ResearchStore, Revisions and Worker.

The original task contract never grows a mutable candidate array. Runtime records
are transactional metadata; observations and judgments use their narrow writers.
"""
from __future__ import annotations
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from ..core import ProtocolError, canonical, digest, file_hash, read_json, write_json, model_inputs, EVALUATORS
from ..identity import EntityKind, new_id
from ..transport.pydantic_planner import PydanticPlannerClient
from .adaptive_contract import AdaptiveTaskContract
from .adaptive_protocol import ProblemProposal, ExperimentProposal, RevisionProposal, StopProposal, parse_action, instructions
from .adaptive_sandbox import strict_executor
from .contracts import Study, StudyStatus, Hypothesis, HypothesisStatus, ExperimentDesign, RunStatus, Decision, Observation
from .revisions import RevisionStore
from .store import ResearchStore
from .workers import LocalWorker, JobSpec, InputArtifact
from . import prediction_comparison


# run() offers this many model steps. A revision consumes the current step, so a
# retest needs one step after it. Experiment slots are separate and are not spent
# by the revision itself.
_PLANNING_STEPS = 12
_EXPERIMENT_SLOTS = 8


def _action_error(message):
    error = ProtocolError(message)
    error.details = [{'path': 'action', 'rule': message}]
    return error


class AdaptivePlannerClient(PydanticPlannerClient):
    tool_names = ('propose_problem', 'run_experiment', 'revise_hypothesis', 'repair_implementation', 'stop_planning')
    parse_action = staticmethod(parse_action)
    instructions = staticmethod(instructions)


class AdaptiveResearchRuntime:
    def __init__(self, directory, contract, data_paths, baseline_source, *, profile='evidence_driven', executor=None):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.contract = AdaptiveTaskContract.model_validate(contract)
        self.paths = {k: Path(v).resolve() for k, v in data_paths.items()}
        self.profile = profile
        if profile not in {'fixed_queue', 'generic_iteration', 'evidence_driven'}:
            raise ProtocolError('Unknown v2 profile')
        for split, path in self.paths.items():
            if split not in self.contract.data_hashes or file_hash(path) != self.contract.data_hashes[split]:
                raise ProtocolError('Adaptive input identity changed')
        if set(self.paths) != set(self.contract.data_hashes):
            raise ProtocolError('Missing adaptive input role')
        self.store = ResearchStore(self.root / 'research.sqlite')
        self.revisions = RevisionStore(self.root / 'revisions')
        self.cancel_event=threading.Event()
        def cancellable_executor(command,workspace,env,stdout,stderr,spec):
            return strict_executor(command,workspace,env,stdout,stderr,spec,cancel_event=self.cancel_event)
        cancellable_executor.execution_backend=strict_executor.execution_backend
        self.worker = LocalWorker(self.root / 'jobs', self.revisions, executor=executor or cancellable_executor)
        self.started = time.monotonic()
        existing = self.records('task')
        if existing:
            binding = existing[0]['data']
            if (binding['contract'] != contract or binding['profile'] != profile
                    or binding.get('baseline_source_sha256') != digest(baseline_source)):
                raise ProtocolError('Frozen adaptive task/session binding changed')
            self.study_id = existing[0]['study_id']
        else:
            self.study_id = new_id(EntityKind.STUDY)
            manifest = {'schema_version': '2.0', 'planning_protocol': 'research-runtime/v2',
                'study_id': self.study_id, 'project': str(self.paths['train'].parent),
                'capability_mode': 'adaptive', 'policy': profile, 'metric': {'name': 'mae', 'direction': 'min'},
                'scorer_id': 'vector-mae-v1', 'scorer_hash': digest(EVALUATORS['vector-mae-v1']),
                'repeat_unit': 'analysis_unit', 'repeat_values': [], 'seeds': [self.contract.model_seed],
                'min_meaningful_effect': .05, 'task_contract_sha256': self.contract.sha256,
                'input_identity': {'input_hashes': {p.name: self.contract.data_hashes[k] for k,p in self.paths.items()}}}
            write_json(self.root / 'research.json', manifest)
            self.store.create_study(Study(self.study_id, self.study_id, self.contract.objective,
                canonical({'contract_sha256': self.contract.sha256, 'manifest_sha256': file_hash(self.root / 'research.json'),
                           'project': manifest['project']}), budget_cap=11.))
            self.record('task', 'binding', {'contract': contract, 'profile': profile,
                                          'baseline_source_sha256': digest(baseline_source)})
        integrity = self.store.verify()
        if not integrity['ok']:
            raise ProtocolError('Adaptive research integrity failed: ' + integrity['reason'])
        self.baseline_source = baseline_source

    def close(self):
        self.store.close()

    def cancel(self):
        self.cancel_event.set()
        for job in self.worker.list_jobs():
            if job['status']=='running':self.worker.cancel(job['job_id'])

    def records(self, topic=None):
        return [dict(r['body'], study_id=r['study_id']) for r in self.store.list('runtime_record')
                if r['session_id'] == 'adaptive-v2' and (topic is None or r['body'].get('topic') == topic)]

    def record(self, topic, key, data):
        identity = 'RT-' + digest({'study': self.study_id, 'topic': topic, 'key': key})[:32]
        existing = self.store.get('runtime_record', identity)
        body = {'schema': 'adaptive-record/v1', 'topic': topic, 'key': str(key), 'data': data}
        if existing:
            if existing['body'] != body:
                raise ProtocolError('Adaptive receipt identity conflicts with frozen content')
            return existing
        return self.store.execute_command(self.study_id, 'record_runtime',
            {'session_id': 'adaptive-v2', 'record_id': identity, 'record_kind': 'session',
             'body': body},
            idempotency_key='adaptive-' + identity[3:],
            expected_version=self.store.get('study', self.study_id)['state_version'],
            actor='adaptive_runtime', trace_id='trc_' + identity[3:])

    def context(self):
        evidence = [{k: j.get('evidence', {}).get(k) for k in ('mean_effect', 'ci95', 'quality',
                    'n_units', 'baseline_mae', 'candidate_mae')} |
                    {'evidence_refs': j['observation_refs'], 'hypothesis_id': j['hypothesis_id'],
                     'version': j['hypothesis_version']}
                    for j in self.store.list('judgment') if j['scope'] == 'dev']
        result = {'task': self.contract.public(), 'problem': self.records('problem'),
                  'raw_evidence': evidence, 'candidates': self.records('candidate'),
                  'attempts': [{**{k:r[k] for k in ('key',)},'data':
                      {k:r['data'][k] for k in ('status','candidate','baseline_mae','candidate_mae',
                           'observation_refs','failure_context','error_type','evaluation_diagnostic') if k in r['data']}}
                      for r in self.records('attempt_completed')],
                  'remaining_experiments': _EXPERIMENT_SLOTS - len(self.records('attempt_started')),
                  'used_wall_seconds': sum(r['data']['seconds'] for r in self.records('elapsed')),
                  'implementation_cli': 'model.py --train JSON --input label-free JSON --output JSON --config JSON --seed INT; '
                    'train rows are {id,features,y}, inputs {id,features}; output list of {id,prediction}.',
                  'current_sources': [{'candidate_id': r['key'], 'source_code':
                        (self.revisions.path(r['data']['revision_id']) / 'code/model.py').read_text(encoding='utf-8')}
                      for r in self.records('candidate')]}
        if self.profile == 'evidence_driven':
            result['structured_judgments'] = [{k: j[k] for k in ('judgment_id', 'hypothesis_id', 'hypothesis_version',
                'new_judgment', 'observation_refs', 'scope')} for j in self.store.list('judgment') if j['scope'] == 'dev']
            result['hypothesis_versions']=self.store.list('hypothesis')
            result['explanation_revisions']=self.records('revision')
        return result

    def _latest_judged_attempt(self):
        judged = [r['data'] for r in self.records('attempt_completed')
                  if r['data'].get('status') == 'succeeded' and r['data'].get('judgment_id')]
        if not judged:
            return None
        latest = judged[-1]
        judgment = self.store.get('judgment', latest['judgment_id'])
        if not judgment:
            return None
        evidence = judgment.get('evidence') if isinstance(judgment.get('evidence'), dict) else {}
        quality = evidence.get('quality')
        return {'judgment_id': latest['judgment_id'], 'new_judgment': judgment.get('new_judgment'),
                'config': latest['candidate']['config'],
                'quality_complete': isinstance(quality, str) and bool(quality)}

    def validate(self, action):
        if self.records('selection'):
            raise ProtocolError('Development is frozen')
        development_refs = {o['observation_id'] for o in self.store.list('observation') if o['scope'] == 'dev'}
        known = set(development_refs)
        known |= {m.get('id') for m in self.contract.materials}
        known |= {j['judgment_id'] for j in self.store.list('judgment') if j.get('scope') == 'dev'}
        if not set(action.evidence_refs) <= known:
            raise ProtocolError('Unknown or withheld evidence reference')
        if isinstance(action, ProblemProposal):
            if not action.evidence_refs:
                raise ProtocolError('Research problem requires public evidence references')
            if self.records('problem') or not all(x.strip() for x in action.explanations) or len(set(action.explanations)) != 2:
                raise ProtocolError('Research problem is frozen or explanations are identical')
        elif not self.records('problem'):
            raise ProtocolError('Propose a research problem before development')
        if isinstance(action, ExperimentProposal):
            if len(self.records('attempt_started')) >= _EXPERIMENT_SLOTS:
                raise ProtocolError('Frozen experiment cap reached')
            try:
                compile(action.source_code, 'model.py', 'exec')
            except SyntaxError:
                raise ProtocolError('Candidate Python syntax is invalid') from None
            if len(action.source_code.encode('utf-8')) > 64000:
                raise ProtocolError('Source exceeds frozen permission')
            if action.parent_candidate_id is not None and not any(r['key'] == action.parent_candidate_id for r in self.records('candidate')):
                raise ProtocolError('Unknown parent candidate')
            if action.kind=='repair_implementation' and not any(
                r['key']==action.parent_candidate_id and r['data']['status']=='failed'
                for r in self.records('attempt_completed')):
                raise ProtocolError('Repair requires a failed parent candidate')
            if action.kind == 'run_experiment' and self.profile == 'evidence_driven':
                latest = self._latest_judged_attempt()
                if (latest and latest['quality_complete']
                        and latest['new_judgment'] in {'refuted', 'inconclusive'}):
                    if latest['judgment_id'] not in action.evidence_refs:
                        raise _action_error('Experiment must cite judgment_id ' + latest['judgment_id'])
                    if canonical(action.config) == canonical(latest['config']):
                        raise _action_error('Experiment must change the configuration judged by ' + latest['judgment_id'])
        if isinstance(action, RevisionProposal):
            if not (set(action.evidence_refs) & development_refs) or not self.store.get('hypothesis', action.hypothesis_id):
                raise ProtocolError('Revision requires an existing hypothesis and development evidence')
            if not any(action.hypothesis_id in (r['data']['hypothesis_id'],r['data']['alternative_id'])
                       for r in self.records('candidate')):
                raise ProtocolError('Only registered candidate explanation pairs can be revised')
            # Soft rejects: the revision spends this planning step and drops the current
            # version's successes from finalist eligibility. A retest needs a later step
            # and an unused experiment slot. Do not hard-stop the cell.
            slots_after = _EXPERIMENT_SLOTS - len(self.records('attempt_started'))
            steps_after = _PLANNING_STEPS - len(self.records('decision_completed')) - 1
            if slots_after < 1:
                raise _action_error('Revision requires a remaining experiment slot')
            if steps_after < 1:
                raise _action_error('Revision requires a remaining planning step for retest')
            hypothesis = self.store.get('hypothesis', action.hypothesis_id)
            tested_current = any(
                row['data']['status'] == 'succeeded'
                and action.hypothesis_id in (row['data']['candidate']['hypothesis_id'],
                                             row['data']['candidate']['alternative_id'])
                and row['data']['candidate']['hypothesis_version'] == hypothesis['version']
                for row in self.records('attempt_completed'))
            if not tested_current:
                raise _action_error('Revision requires a successful test of the current explanation version')

    def _revision(self, source, hid, did, parent=None):
        compile(source, 'model.py', 'exec')
        data = source.encode('utf-8')
        before = '' if parent is None else (self.revisions.path(parent) / 'code/model.py').read_text(encoding='utf-8')
        difference = ''.join(difflib.unified_diff(before.splitlines(True), source.splitlines(True),
                                                fromfile='a/model.py', tofile='b/model.py'))
        identity = {'hypothesis_id': hid, 'design_id': did, 'parent_revision_id': parent,
                    'source_control': {'kind': 'unversioned'},
                    'actor': 'adaptive_runtime', 'task_contract_sha256': self.contract.sha256,
                    'files': {'model.py': hashlib.sha256(data).hexdigest()}, 'diff_sha256': digest(difference)}
        rid = 'REV-' + digest(identity)[:24]
        manifest = {'revision_id': rid, 'identity': identity, 'status': 'immutable',
                    'files': identity['files'], 'diff': difference}
        self.revisions.materialize(rid, manifest, {'model.py': data})
        return manifest

    def _register(self, source, mechanism, config, key, parent=None, hypothesis_id=None):
        hid = hypothesis_id or 'H-' + digest({'study': self.study_id, 'key': key})[:24]
        hypothesis = self.store.get('hypothesis', hid)
        if hypothesis is None:
            self.store.add_hypothesis(Hypothesis(hid, self.study_id, 1, None, mechanism,
                'frozen regression task', ('relative MAE improvement >5%',),
                ('independent interval does not support the prediction',), ('equivalence or degradation',)))
            hypothesis = self.store.get('hypothesis', hid)
        did = 'D-' + digest({'hid': hid, 'version': hypothesis['version'], 'key': key})[:24]
        baseline = canonical(self.contract.baseline)
        intervention = baseline if key == 'control' else canonical({'config': config, 'source_sha256': digest(source)})
        if self.store.get('design', did) is None:
            self.store.create_design(ExperimentDesign(did, hid, interventions=(intervention,),
                control=baseline, baseline=baseline, metric='mae', scorer_id='vector-mae-v1', metric_direction='min',
                analysis_unit='analysis_unit', splits=('dev', 'confirmation'), seeds=(self.contract.model_seed,),
                min_meaningful_effect=.05))
            self.store.freeze_design(did)
        revision = self._revision(source, hid, did, parent)
        alternate = 'H-' + digest({'alternative_to': hid})[:24]
        old_alternate = self.store.get('hypothesis', alternate)
        if old_alternate is None:
            self.store.add_hypothesis(Hypothesis(alternate, self.study_id, 1, None,
                'Alternative explanation: intervention is predictively equivalent', 'frozen regression task',
                ('relative MAE improvement within [-5%,5%]',), ('independent interval outside the equivalence band',),
                ('meaningful improvement or degradation',)))
        alt_did = 'D-' + digest({'alternative': alternate, 'version': hypothesis['version'], 'key': key})[:24]
        if self.store.get('design', alt_did) is None:
            self.store.create_design(ExperimentDesign(alt_did, alternate, interventions=(intervention,),
                control=baseline, baseline=baseline, metric='mae', scorer_id='vector-mae-v1', metric_direction='min',
                analysis_unit='analysis_unit', splits=('dev', 'confirmation'), seeds=(self.contract.model_seed,),
                min_meaningful_effect=.05))
            self.store.freeze_design(alt_did)
        payload = {'hypothesis_id': hid, 'hypothesis_version': hypothesis['version'], 'design_id': did,
                   'revision_id': revision['revision_id'], 'config': config, 'alternative_id': alternate}
        self.record('candidate' if key != 'control' else 'control', key, payload)
        return payload

    def _run(self, candidate, split):
        rid = 'RUN-' + digest({'revision': candidate['revision_id'], 'split': split})[:24]
        revision = self.revisions.path(candidate['revision_id'])
        registered = self.store.get('run', rid)
        if registered and registered['status'] in {'implementation_failed', 'infrastructure_failed', 'cancelled'}:
            raise ProtocolError('Previous failed execution is immutable')
        if registered is None:
            with self.store.transaction():
                reservation = self.store.budget(self.study_id).reserve(self.study_id, rid, 1.,
                    kind='confirmation' if split == 'confirmation' else 'dev')
                self.record('run_budget', rid, reservation)
                self.store.runs.register(rid, candidate['design_id'], revision_id=candidate['revision_id'],
                                         revision_manifest_sha256=file_hash(revision / 'revision.json'))
                self.store.runs.set_status(rid, RunStatus.RUNNING)
        io = self.root / 'inputs' / rid
        io.mkdir(parents=True, exist_ok=True)
        training = read_json(self.paths['train'])
        if split == 'confirmation':
            training += read_json(self.paths['dev'])
        write_json(io / 'train.json', training)
        write_json(io / 'inputs.json', model_inputs(read_json(self.paths[split]), 'vector-mae-v1'))
        write_json(io / 'config.json', candidate['config'])
        spec = JobSpec(rid, candidate['revision_id'], file_hash(revision / 'revision.json'), candidate['design_id'],
            'model.py', args=('--train', 'inputs/train.json', '--input', 'inputs/inputs.json',
                             '--config', 'inputs/config.json', '--output', 'outputs/predictions.json',
                             '--seed', str(self.contract.model_seed)),
            inputs=tuple(InputArtifact(str(io / name), name, file_hash(io / name), role) for name, role in
                         [('train.json', 'training_data'), ('inputs.json', 'features_without_labels'), ('config.json', 'config')]),
            outputs=('outputs/predictions.json',), timeout_seconds=300, mem_limit_mb=4096,
            cpu_time_seconds=300, max_processes=32, sandboxed=True)
        prior = self.worker.read_manifest(spec.job_id) if (self.worker.root / spec.job_id / 'job.json').exists() else None
        if prior and prior['status'] == 'running':
            raise ProtocolError('Interrupted worker outcome requires audit; not automatically replayed')
        receipt = self.worker.run(spec)
        if not any(r['key'] == rid for r in self.records('run_budget_settled')):
            with self.store.transaction():
                reservation = next(r['data'] for r in self.records('run_budget') if r['key'] == rid)
                self.store.budget(self.study_id).settle(reservation['reservation_id'], 1.)
                self.record('run_budget_settled', rid, {'receipt_sha256': digest(receipt)})
        if receipt['status'] != 'succeeded':
            if self.store.get('run', rid)['status'] == 'running':
                self.store.runs.set_status(rid, {'infrastructure_failed': RunStatus.INFRA_FAILED,
                    'cancelled': RunStatus.CANCELLED}.get(receipt['status'], RunStatus.IMPLEMENTATION_FAILED))
            context = self.worker.failure_context(spec.job_id)
            stderr = (((context.get('logs') or {}).get('stderr.log') or {}).get('text') or '')
            error = ProtocolError('Candidate execution failed; retained worker receipt available')
            error.category = receipt['status']
            error.details = [{
                'path': 'worker', 'rule': receipt['status'],
                'job_id': spec.job_id,
                'stderr_tail': stderr[-4000:],
            }]
            raise error
        if self.store.get('run', rid)['status'] == 'running':
            self.store.runs.set_status(rid, RunStatus.SUCCEEDED)
        path = self.root / 'predictions' / (rid + '.json')
        path.parent.mkdir(exist_ok=True)
        self.worker.fetch_artifact(spec.job_id, 'outputs/predictions.json', path)
        return rid, path, self.worker.root / spec.job_id / 'receipt.json'

    def _evaluate(self, candidate, split):
        controls = self.records('control')
        control = controls[0]['data'] if controls else self._register(
            self.baseline_source, 'Frozen common baseline', self.contract.baseline, 'control')
        br, bp, bs = self._run(control, split)
        cr, cp, cs = self._run(candidate, split)
        rows = read_json(self.paths[split])
        def descriptor(prediction,receipt,identity):
            files={'receipt':receipt,'manifest':receipt.parent/'job.json',
                   'revision':self.revisions.path(identity['revision_id'])/'revision.json'}
            return {'path':str(prediction),'sha256':file_hash(prediction),'worker':
                    {role:{'path':str(path),'sha256':file_hash(path)} for role,path in files.items()}}
        baseline_descriptor=descriptor(bp,bs,control)
        candidate_descriptor=descriptor(cp,cs,candidate)
        packet = {'schema': prediction_comparison.SCHEMA, 'dataset': str(self.paths[split]),
            'dataset_sha256': self.contract.data_hashes[split],
            'baseline': baseline_descriptor,
            'candidate': candidate_descriptor,
            'groups': {r['id']: self.contract.groups[r['id']] for r in rows},
            'independence_verified': self.contract.independence_verified, 'bootstrap_seed': self.contract.bootstrap_seed,
            'hypothesis_predictions': {candidate['hypothesis_id']: {'operator': 'gt', 'threshold': .05},
                candidate['alternative_id']: {'operator': 'between', 'threshold': [-.05, .05]}},
            'bindings': [ref for desc in (baseline_descriptor,candidate_descriptor)
                         for ref in desc['worker'].values()]}
        if self.contract.sampling is not None:
            packet['sampling'] = self.contract.sampling
        out = self.root / 'evaluations' / cr
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / 'packet.json', packet)
        with (out/'stdout.log').open('wb') as stdout,(out/'stderr.log').open('wb') as stderr:
            evaluated=subprocess.run([sys.executable, '-m', 'popper.research.prediction_comparison',
                            '--packet', str(out / 'packet.json'), '--response', str(out / 'response.json')],
                           stdout=stdout,stderr=stderr,timeout=120)
        write_json(out/'receipt.json',{'returncode':evaluated.returncode,
            'evaluator_implementation_sha256':file_hash(Path(prediction_comparison.__file__)),
            'packet_sha256':file_hash(out/'packet.json'),
            'logs':{name:file_hash(out/name) for name in ('stdout.log','stderr.log')}})
        if evaluated.returncode:
            raise ProtocolError('Independent evaluator rejected the prediction packet')
        result = read_json(out / 'response.json')
        refs = []
        for role, run in [('baseline_mae', br), ('candidate_mae', cr)]:
            oid = 'OBS-' + digest({'run': run, 'packet': digest(packet), 'role': role})[:24]
            self.store.observations.add(Observation(oid, run, 'vector-mae-v1', result[role], unit='mae', scope=split,
                artifact_id=str(out / 'response.json'), artifact_sha256=file_hash(out / 'response.json'),
                selector=role, evaluator_service_hash=file_hash(Path(prediction_comparison.__file__)),
                trust='separate_process_same_account'))
            refs.append(oid)
        descriptor = {'comparison_schema': prediction_comparison.SCHEMA, 'packet': str(out / 'packet.json'),
                      'packet_sha256': file_hash(out / 'packet.json'), 'response': str(out / 'response.json'),
                      'response_sha256': file_hash(out / 'response.json')}
        judgment = self.store.record_judgment(candidate['hypothesis_id'], candidate['hypothesis_version'],
                                             refs, comparison_artifact=descriptor)
        alternative = self.store.record_judgment(candidate['alternative_id'], candidate['hypothesis_version'],
                                                refs, comparison_artifact=descriptor)
        for identity, verdict in [(candidate['hypothesis_id'],judgment), (candidate['alternative_id'],alternative)]:
            state=({'supported':HypothesisStatus.SUPPORTED_IN_SCOPE,
                    'refuted':HypothesisStatus.CONTRADICTED_IN_SCOPE}.get(verdict['new_judgment'],HypothesisStatus.INCONCLUSIVE)
                   if split=='confirmation' else HypothesisStatus.INCONCLUSIVE)
            if self.store.get('hypothesis',identity)['status']!=state.value:
                self.store.set_hypothesis_status(identity,candidate['hypothesis_version'],state)
        return {'observation_refs': refs, 'judgment': judgment['new_judgment'],
                'alternative_judgment': alternative['new_judgment'],
                'judgment_id': judgment['judgment_id'], 'candidate_mae': result['candidate_mae'],
                'comparison': descriptor, 'candidate': candidate}

    def apply(self, action, key):
        applied = next((r for r in self.records('action_applied') if r['key'] == str(key)), None)
        if applied:
            if applied['data']['proposal'] != action.model_dump():
                raise ProtocolError('Action replay conflicts with the persisted proposal')
            return
        started = next((r for r in self.records('attempt_started') if r['key'] == str(key)), None)
        if started:
            if started['data']['proposal'] != action.model_dump():
                raise ProtocolError('Experiment replay conflicts with the persisted proposal')
            if any(r['key'] == str(key) for r in self.records('attempt_completed')):
                return
        else:
            self.validate(action)
        if not isinstance(action, ExperimentProposal):
            with self.store.transaction():
                self._apply_action(action, key)
                self.record('action_applied', key, {'proposal': action.model_dump()})
        else:
            self._apply_action(action, key)

    def _apply_action(self, action, key):
        if isinstance(action, ProblemProposal):
            self.record('problem', 'accepted', action.model_dump())
        elif isinstance(action, RevisionProposal):
            h = self.store.get('hypothesis', action.hypothesis_id)
            self.store.revise_hypothesis(Hypothesis(h['hypothesis_id'], self.study_id, h['version'] + 1,
                h['version'], action.mechanism, h['applicability'], tuple(action.predictions),
                tuple(h['falsification']), tuple(h['alternatives'])))
            pair=next(r['data'] for r in self.records('candidate')
                      if h['hypothesis_id'] in (r['data']['hypothesis_id'],r['data']['alternative_id']))
            alternate_id = pair['alternative_id'] if h['hypothesis_id']==pair['hypothesis_id'] else pair['hypothesis_id']
            alternate = self.store.get('hypothesis', alternate_id)
            if alternate:
                self.store.revise_hypothesis(Hypothesis(alternate_id, self.study_id, alternate['version'] + 1,
                    alternate['version'], alternate['mechanism'], alternate['applicability'],
                    tuple(alternate['predictions']), tuple(alternate['falsification']), tuple(alternate['alternatives'])))
            self.record('revision', key, action.model_dump())
        elif isinstance(action, StopProposal):
            self.freeze_selection('model_stop')
        else:
            parent = next((r['data'] for r in self.records('candidate') if r['key'] == action.parent_candidate_id), None)
            with self.store.transaction():
                candidate = self._register(action.source_code, action.mechanism, action.config, key,
                    parent=parent['revision_id'] if parent else None,
                    hypothesis_id=parent['hypothesis_id'] if parent else None)
                self.record('attempt_started', key, {'candidate': candidate, 'proposal': action.model_dump()})
            try:
                result = self._evaluate(candidate, 'dev')
            except Exception as error:
                failures=[]
                for job in self.worker.list_jobs():
                    if job['status'] in {'implementation_failed','infrastructure_failed','cancelled'}:
                        failures.append(self.worker.failure_context(job['job_id']))
                evaluation=self.root/'evaluations'/('RUN-'+digest({'revision':candidate['revision_id'],'split':'dev'})[:24])
                diagnostic=None
                if (evaluation/'receipt.json').exists():
                    receipt=read_json(evaluation/'receipt.json')
                    stderr=evaluation/'stderr.log'
                    if file_hash(stderr)!=receipt['logs']['stderr.log']:
                        raise ProtocolError('Evaluator diagnostic changed')
                    diagnostic={'receipt':str(evaluation/'receipt.json'),'receipt_sha256':file_hash(evaluation/'receipt.json'),
                                'source':'independent_evaluator','stderr':stderr.read_bytes()[-16000:].decode('utf-8',errors='replace')}
                self.record('attempt_completed', key, {'status': 'failed', 'error_type': type(error).__name__,
                    'candidate': candidate, 'failure_context': failures[-2:], 'evaluation_diagnostic':diagnostic})
            else:
                self.record('attempt_completed', key, {'status': 'succeeded', **result})

    def _confirmation_preflight(self, candidate):
        """Run confirmation-split execution under a side job id; never registers confirmation RUN-*."""
        preflight_id = 'PREFLIGHT-' + digest({'revision': candidate['revision_id'], 'split': 'confirmation'})[:24]
        revision = self.revisions.path(candidate['revision_id'])
        io = self.root / 'inputs' / preflight_id
        io.mkdir(parents=True, exist_ok=True)
        training = read_json(self.paths['train']) + read_json(self.paths['dev'])
        write_json(io / 'train.json', training)
        write_json(io / 'inputs.json', model_inputs(read_json(self.paths['confirmation']), 'vector-mae-v1'))
        write_json(io / 'config.json', candidate['config'])
        spec = JobSpec(preflight_id, candidate['revision_id'], file_hash(revision / 'revision.json'),
            candidate['design_id'], 'model.py',
            args=('--train', 'inputs/train.json', '--input', 'inputs/inputs.json',
                  '--config', 'inputs/config.json', '--output', 'outputs/predictions.json',
                  '--seed', str(self.contract.model_seed)),
            inputs=tuple(InputArtifact(str(io / name), name, file_hash(io / name), role) for name, role in
                         [('train.json', 'training_data'), ('inputs.json', 'features_without_labels'),
                          ('config.json', 'config')]),
            outputs=('outputs/predictions.json',), timeout_seconds=300, mem_limit_mb=4096,
            cpu_time_seconds=300, max_processes=32, sandboxed=True)
        if (self.worker.root / spec.job_id / 'job.json').exists():
            prior = self.worker.read_manifest(spec.job_id)
            if prior and prior['status'] == 'running':
                return {'ok': False, 'category': 'infrastructure_failed',
                        'message': 'Interrupted preflight outcome requires audit', 'details': []}
            if prior and prior['status'] == 'succeeded':
                return {'ok': True, 'job_id': spec.job_id, 'reused': True}
        receipt = self.worker.run(spec)
        if receipt['status'] == 'succeeded':
            return {'ok': True, 'job_id': spec.job_id, 'receipt_sha256': digest(receipt)}
        context = self.worker.failure_context(spec.job_id)
        stderr = (((context.get('logs') or {}).get('stderr.log') or {}).get('text') or '')
        return {
            'ok': False, 'category': receipt['status'], 'job_id': spec.job_id,
            'message': 'Confirmation-split preflight failed',
            'details': [{'path': 'worker', 'rule': receipt['status'],
                         'stderr_tail': stderr[-4000:]}],
        }

    def freeze_selection(self, reason):
        if not self.records('selection'):
            succeeded = [r['data'] for r in self.records('attempt_completed') if r['data']['status'] == 'succeeded']
            completed = [r for r in succeeded if self.store.get('hypothesis', r['candidate']['hypothesis_id'])
                         and self.store.get('hypothesis', r['candidate']['hypothesis_id'])['version']
                         == r['candidate']['hypothesis_version']]
            best = min(completed, key=lambda r: (r['candidate_mae'], r['candidate']['revision_id'])) if completed else None
            empty_reason = None
            if best is None:
                empty_reason = ('successful_attempts_superseded_by_revision' if succeeded
                                else 'no_successful_attempts')
            preflight = self._confirmation_preflight(best['candidate']) if best is not None else None
            payload = {'reason': reason, 'finalist': best, 'confirmation_preflight': preflight}
            if empty_reason is not None:
                payload['empty_finalist_reason'] = empty_reason
            self.record('selection', 'frozen', payload)
        return self.records('selection')[0]['data']

    def run(self, client):
        stop_categories = frozenset({
            'connect_timeout', 'read_timeout', 'total_timeout', 'cancelled',
            'authentication_or_permission', 'temporary_service', 'output_truncated',
            'stream_interrupted', 'connection_failure', 'incompatible_request',
            'response_size_limit', 'request_budget_exhausted', 'usage_unresolved',
            'usage_accounting_failure', 'request_recovery_blocked',
        })
        if self.records('selection'):
            return self.result()
        def reject_decision(error):
            category = getattr(error, 'category', None)
            if category in stop_categories:
                raise error
            details = list(getattr(error, 'details', None) or [
                {'path': 'action', 'rule': 'registered_action_or_evidence'}])
            logical_id = 'RT-' + digest({'study': self.study_id, 'step': n})[:32]
            receipts = [r['data'] for r in self.records('model_receipt')
                        if r['data'].get('kind') == 'model_response_rejected'
                        and r['data'].get('logical_request_id') == logical_id]
            audit = {
                'consumed_send': True,
                'logical_request_id': logical_id,
                'physical_attempts': [
                    {'attempt_index': r.get('attempt_index'),
                     'correction_attempt': bool(r.get('correction_attempt')),
                     'consumed_send': r.get('consumed_send', True),
                     'action_kind': r.get('action_kind'),
                     'details': r.get('details')}
                    for r in receipts],
                'reject_receipt_count': len(receipts),
            }
            self.record('response', key, {
                'rejected': True, 'category': category or 'action_contract_rejected',
                'details': details, 'audit': audit})
            self.record('decision_completed', key, {
                'status': 'rejected', 'category': category or 'action_contract_rejected',
                'details': details, 'audit': audit})
            if not self.records('selection'):
                self.record('elapsed', key, {'seconds': time.monotonic()-decision_start})

        for n in range(_PLANNING_STEPS):
            if self.cancel_event.is_set():
                self.freeze_selection('cancelled')
                break
            key = str(n)
            decision_start = time.monotonic()
            if self.records('selection'):
                break
            if len(self.records('attempt_started')) >= _EXPERIMENT_SLOTS or self.context()['used_wall_seconds'] > 5400:
                self.freeze_selection('experiment_or_wall_limit')
                break
            if any(r['key'] == key for r in self.records('decision_completed')):
                continue
            received = next((r for r in self.records('response') if r['key'] == key), None)
            if not received and any(r['key'] == key for r in self.records('request')):
                raise ProtocolError('Ambiguous request outcome; paid request cannot be replayed')
            if received:
                if received['data'].get('rejected'):
                    continue
                payload = received['data']['payload']
            else:
                self.record('request', key, {'context_sha256': digest(self.context())})
                try:
                    payload = client.decide(self.context(), validate_action=self.validate,
                        cancel_event=self.cancel_event,
                        logical_request_id='RT-' + digest({'study': self.study_id, 'step': n})[:32],
                        on_event=lambda event: self.record('model_receipt', event['event_id'], event))
                    # decide() may skip validate_action. Apply only after the same
                    # check, and keep the refusal on the soft-reject receipt path.
                    self.validate(parse_action(payload['action']))
                except ProtocolError as error:
                    reject_decision(error)
                    continue
                self.record('response', key, {'payload': payload})
            self.apply(parse_action(payload['action']), key)
            self.record('decision_completed', key, {'payload_sha256': digest(payload)})
            if not self.records('selection'):
                self.record('elapsed', key, {'seconds': time.monotonic()-decision_start})
        self.freeze_selection('decision_limit')
        return self.result()

    def confirm(self, frozen_cohort):
        selection = self.freeze_selection('confirmation_gate')
        if digest(selection) != frozen_cohort.get(self.study_id):
            raise ProtocolError('Confirmation requires the immutable cohort selection roster')
        if self.records('confirmation_completed'):
            return self.records('confirmation_completed')[0]['data']
        if not selection['finalist']:
            result = {'status': 'not_evaluated'}
        else:
            self.record('confirmation_started', 'once', {'selection_sha256': digest(selection)})
            result = self._evaluate(selection['finalist']['candidate'], 'confirmation')
            from .scientific_authority import authorize_confirmation
            with self.store.transaction():
                decision = Decision('DEC-' + digest(result)[:24], self.study_id,
                    self.store.get('study', self.study_id)['state_version'], tuple(result['observation_refs']),
                    action='stop', rationale='Frozen independent confirmation; no model feedback',
                    actor='confirmation_controller', judgment_refs=(result['judgment_id'],))
                self.store.add_decision(decision)
                with authorize_confirmation(actor='confirmation_controller'):
                    self.store.claims.confirm(self.study_id, decision.decision_id, *result['observation_refs'])
                result = {'status': 'confirmation_completed', **result}
                self.record('confirmation_completed', 'once', result)
                self.store.set_study_status(self.study_id, StudyStatus.CONCLUDED)
            return result
        with self.store.transaction():
            self.record('confirmation_completed', 'once', result)
            self.store.set_study_status(self.study_id, StudyStatus.CONCLUDED)
        return result

    def result(self):
        return {'schema': 'research-runtime/v2', 'study_id': self.study_id,
                'problem': self.records('problem'), 'attempts': self.records('attempt_completed'),
                'selection': self.records('selection'), 'integrity': self.store.verify()}
