"""New protocol entrypoints share the controller and preserve legacy routes."""
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from popper.core import ProtocolError, initialize, read_json, write_json
from popper.cli import main
from popper.campaign_nodes import dev_node, freeze_node, confirm_node
from popper.research.controller import ResearchController
from popper.research.study_jobs import preview_study_action
from popper.server import Workstation
from popper.research.runtime import ResearchRuntime
from evaluation.verify_transfer_pilot import AuditReader
from test_unit_comparison import polynomial_project
from popper.capabilities import extra_available

pytestmark = pytest.mark.skipif(not extra_available('analysis'), reason='analysis extra required for paired comparison')


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    polynomial_project(root)
    spec = read_json(root/'experiment.json')
    spec['metadata'] = {'planning_protocol': 'research-runtime/v1'}
    write_json(root/'experiment.json', spec)
    initialize(root)
    return root


def test_campaign_workbench_and_study_preview_share_registered_selection(project, tmp_path):
    state = {'project': str(project), 'mode': 'trusted_local'}
    developed = dev_node(tmp_path/'campaign', state)
    assert developed['planning_runtime']['planner']['status'] == 'stopped'
    run = project/'.popper'/'research-runtime'
    c = ResearchController(run)
    try:
        counts = len(c.store.list('run'))
        assert c.exp.state()['phase'] == 'searching'
        assert not c.exp.state().get('test_consumed', False)
        selected = c._registered_selection()
        assert selected
        assert c.store.get('hypothesis', selected['hypothesis_id'])['status'] == 'untested'
    finally:
        c.close()
    before = (run/'research.sqlite').read_bytes()
    preview = preview_study_action(run, 'confirm')
    assert preview['selected_hypothesis_id'] == selected['hypothesis_id']
    assert before == (run/'research.sqlite').read_bytes()
    workstation = Workstation(('127.0.0.1', 0), project, trusted_local=True)
    try:
        workstation.launch('freeze')
        assert workstation.wait_for_jobs(60) == 0
        assert workstation.job['status'] == 'completed'
        assert workstation.job['result']['selection_receipt'] == selected
    finally:
        workstation.server_close()
    frozen = freeze_node(tmp_path/'campaign', state)
    assert frozen['selection_receipt'] == selected
    confirmed = confirm_node(tmp_path/'campaign', state)
    assert confirmed['phase'] == 'concluded'
    replay = confirm_node(tmp_path/'campaign', state)
    assert replay == confirmed
    c = ResearchController(run)
    try:
        assert len(c.store.list('run')) == counts + 2
        assert c.store.verify()['ok']
        with pytest.raises(ProtocolError):
            c.execute_registered_candidate(c.manifest['candidate_hypothesis_ids'][0],
                expected_version=c.store.get('study',c.manifest['study_id'])['state_version'], trusted_local=True)
    finally:
        c.close()

    before = (run/'research.sqlite').read_bytes()
    reader = AuditReader(run)
    try:
        restored = ResearchRuntime(reader, session_id='registered-run', max_experiments=2).result()
        assert restored['finalist'] == developed['planning_runtime']['finalist']
        assert restored['integrity']['ok']
    finally:
        reader.close()
    assert before == (run/'research.sqlite').read_bytes()


def test_cli_run_context_and_confirmation_use_original_entrypoint(project):
    run = project/'.popper'/'research-runtime'
    ResearchController.initialize(project, run)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(['research','plan',str(run),'--context-only']) == 0
    context = json.loads(out.getvalue())
    assert context['primary_scope_id'] == 'dev:slice:s9'
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(['research','run',str(run),'--trusted-local']) == 0
    result = json.loads(out.getvalue())
    assert result['phase'] == 'ready_for_confirmation'
    assert result['planning_runtime']['planner']['status'] == 'stopped'
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(['research','confirm',str(run),'--trusted-local']) == 0


def test_unknown_protocol_never_falls_back_to_experiment(project, tmp_path):
    # Tampered inputs are deliberately refused before any new execution.
    spec = read_json(project/'experiment.json')
    spec['metadata']['planning_protocol'] = 'unsupported/v99'
    write_json(project/'experiment.json', spec)
    with patch('popper.campaign_nodes._experiment') as legacy:
        with pytest.raises(ProtocolError, match='未支持'):
            dev_node(tmp_path/'campaign', {'project': str(project),'mode': 'trusted_local'})
        legacy.assert_not_called()


def test_campaign_preserves_planner_failure_with_partial_evidence(project, tmp_path):
    outcome = {'phase': 'researching', 'observations': [],
               'planning_runtime': {'planner': {'status': 'failed', 'reason': 'schema_failure'},
                                    'development': {'status': 'partial'}, 'finalist': {'hypothesis_id': 'H-valid'}}}
    with patch('popper.campaign_nodes._planning_controller') as factory:
        factory.return_value.run.return_value = outcome
        result = dev_node(tmp_path/'campaign', {'project':str(project),'mode':'trusted_local'})
    assert result['outcome'] == 'retryable'
    assert result['planning_runtime'] == outcome['planning_runtime']
