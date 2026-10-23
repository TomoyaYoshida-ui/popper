"""Shared Adaptive worker execution + independent comparison recording.

Adaptive must not own a private scoring/run authority: candidate worker jobs and
judgment publishes go through this module. ``ResearchController`` remains the
registered-project executor; Adaptive fixtures call these helpers instead of
duplicating store/run/evaluation logic inline.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from ..core import ProtocolError, digest, file_hash, model_inputs, read_json, write_json
from . import prediction_comparison
from .contracts import HypothesisStatus, Observation, RunStatus
from .scientific_authority import authorize_evidence_write
from .workers import InputArtifact, JobSpec


def execute_adaptive_candidate_run(
        *,
        store,
        study_id: str,
        root: Path,
        paths: dict,
        contract,
        worker,
        revisions,
        candidate: dict,
        split: str,
        record,
        records):
    """Register budget/run, execute sandboxed worker job, settle budget.

    ``record`` / ``records`` are Adaptive runtime append-only metadata hooks
    (same semantics as ``AdaptiveResearchRuntime.record`` / ``.records``).
    """
    rid = 'RUN-' + digest({'revision': candidate['revision_id'], 'split': split})[:24]
    revision = revisions.path(candidate['revision_id'])
    registered = store.get('run', rid)
    if registered and registered['status'] in {
            'implementation_failed', 'infrastructure_failed', 'cancelled'}:
        raise ProtocolError('Previous failed execution is immutable')
    if registered is None:
        with store.transaction():
            reservation = store.budget(study_id).reserve(
                study_id, rid, 1.,
                kind='confirmation' if split == 'confirmation' else 'dev')
            record('run_budget', rid, reservation)
            store.runs.register(
                rid, candidate['design_id'], revision_id=candidate['revision_id'],
                revision_manifest_sha256=file_hash(revision / 'revision.json'))
            store.runs.set_status(rid, RunStatus.RUNNING)
    io = root / 'inputs' / rid
    io.mkdir(parents=True, exist_ok=True)
    training = read_json(paths['train'])
    if split == 'confirmation':
        training += read_json(paths['dev'])
    write_json(io / 'train.json', training)
    write_json(io / 'inputs.json', model_inputs(read_json(paths[split]), 'vector-mae-v1'))
    write_json(io / 'config.json', candidate['config'])
    spec = JobSpec(
        rid, candidate['revision_id'], file_hash(revision / 'revision.json'),
        candidate['design_id'], 'model.py',
        args=('--train', 'inputs/train.json', '--input', 'inputs/inputs.json',
              '--config', 'inputs/config.json', '--output', 'outputs/predictions.json',
              '--seed', str(contract.model_seed)),
        inputs=tuple(
            InputArtifact(str(io / name), name, file_hash(io / name), role)
            for name, role in [
                ('train.json', 'training_data'),
                ('inputs.json', 'features_without_labels'),
                ('config.json', 'config')]),
        outputs=('outputs/predictions.json',), timeout_seconds=300, mem_limit_mb=4096,
        cpu_time_seconds=300, max_processes=32, sandboxed=True)
    prior = (worker.read_manifest(spec.job_id)
             if (worker.root / spec.job_id / 'job.json').exists() else None)
    if prior and prior['status'] == 'running':
        raise ProtocolError(
            'Interrupted worker outcome requires audit; not automatically replayed')
    receipt = worker.run(spec)
    if not any(r['key'] == rid for r in records('run_budget_settled')):
        with store.transaction():
            reservation = next(
                r['data'] for r in records('run_budget') if r['key'] == rid)
            store.budget(study_id).settle(reservation['reservation_id'], 1.)
            record('run_budget_settled', rid, {'receipt_sha256': digest(receipt)})
    if receipt['status'] != 'succeeded':
        if store.get('run', rid)['status'] == 'running':
            store.runs.set_status(rid, {
                'infrastructure_failed': RunStatus.INFRA_FAILED,
                'cancelled': RunStatus.CANCELLED,
            }.get(receipt['status'], RunStatus.IMPLEMENTATION_FAILED))
        context = worker.failure_context(spec.job_id)
        stderr = (((context.get('logs') or {}).get('stderr.log') or {}).get('text') or '')
        error = ProtocolError('Candidate execution failed; retained worker receipt available')
        error.category = receipt['status']
        error.details = [{
            'path': 'worker', 'rule': receipt['status'],
            'job_id': spec.job_id,
            'stderr_tail': stderr[-4000:],
        }]
        raise error
    if store.get('run', rid)['status'] == 'running':
        store.runs.set_status(rid, RunStatus.SUCCEEDED)
    path = root / 'predictions' / (rid + '.json')
    path.parent.mkdir(exist_ok=True)
    worker.fetch_artifact(spec.job_id, 'outputs/predictions.json', path)
    return rid, path, worker.root / spec.job_id / 'receipt.json'


def run_prediction_comparison_subprocess(packet, out: Path, *, timeout=120):
    """Run the independent MAE comparison process; returns parsed response."""
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / 'packet.json', packet)
    with (out / 'stdout.log').open('wb') as stdout, (out / 'stderr.log').open('wb') as stderr:
        evaluated = subprocess.run(
            [sys.executable, '-m', 'popper.research.prediction_comparison',
             '--packet', str(out / 'packet.json'),
             '--response', str(out / 'response.json')],
            stdout=stdout, stderr=stderr, timeout=timeout)
    write_json(out / 'receipt.json', {
        'returncode': evaluated.returncode,
        'evaluator_implementation_sha256': file_hash(Path(prediction_comparison.__file__)),
        'packet_sha256': file_hash(out / 'packet.json'),
        'logs': {name: file_hash(out / name) for name in ('stdout.log', 'stderr.log')},
    })
    if evaluated.returncode:
        raise ProtocolError('Independent evaluator rejected the prediction packet')
    return read_json(out / 'response.json')


def publish_paired_mae_result(
        store, *,
        candidate,
        baseline_run_id,
        candidate_run_id,
        packet,
        response,
        out: Path,
        split: str,
        actor: str = 'adaptive_scientific_execution'):
    """Write observations/judgments/statuses for one Adaptive paired comparison."""
    evaluator_hash = file_hash(Path(prediction_comparison.__file__))
    response_path = out / 'response.json'
    descriptor = {
        'comparison_schema': prediction_comparison.SCHEMA,
        'packet': str(out / 'packet.json'),
        'packet_sha256': file_hash(out / 'packet.json'),
        'response': str(response_path),
        'response_sha256': file_hash(response_path),
    }
    with authorize_evidence_write(actor=actor):
        refs = []
        for role, run in [('baseline_mae', baseline_run_id),
                          ('candidate_mae', candidate_run_id)]:
            oid = 'OBS-' + digest({'run': run, 'packet': digest(packet), 'role': role})[:24]
            store.observations.add(Observation(
                oid, run, 'vector-mae-v1', response[role], unit='mae', scope=split,
                artifact_id=str(response_path), artifact_sha256=file_hash(response_path),
                selector=role, evaluator_service_hash=evaluator_hash,
                trust='separate_process_same_account'))
            refs.append(oid)
        judgment = store.record_judgment(
            candidate['hypothesis_id'], candidate['hypothesis_version'],
            refs, comparison_artifact=descriptor)
        alternative = store.record_judgment(
            candidate['alternative_id'], candidate['hypothesis_version'],
            refs, comparison_artifact=descriptor)
        for identity, verdict in [
                (candidate['hypothesis_id'], judgment),
                (candidate['alternative_id'], alternative)]:
            state = ({
                'supported': HypothesisStatus.SUPPORTED_IN_SCOPE,
                'refuted': HypothesisStatus.CONTRADICTED_IN_SCOPE,
            }.get(verdict['new_judgment'], HypothesisStatus.INCONCLUSIVE)
                     if split == 'confirmation' else HypothesisStatus.INCONCLUSIVE)
            if store.get('hypothesis', identity)['status'] != state.value:
                store.set_hypothesis_status(
                    identity, candidate['hypothesis_version'], state)
        return {
            'observation_refs': refs,
            'judgment': judgment['new_judgment'],
            'alternative_judgment': alternative['new_judgment'],
            'judgment_id': judgment['judgment_id'],
            'candidate_mae': response['candidate_mae'],
            'comparison': descriptor,
            'candidate': candidate,
        }
