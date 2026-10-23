"""Shared independent comparison recording for Adaptive (and future Controller reuse).

Adaptive may still schedule worker jobs locally, but publishing observations,
judgments, and conclusive hypothesis statuses must go through this module under
``authorize_evidence_write``. That removes a second silent authority for scoring.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from ..core import ProtocolError, digest, file_hash, read_json, write_json
from . import prediction_comparison
from .contracts import HypothesisStatus, Observation
from .scientific_authority import authorize_evidence_write


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
