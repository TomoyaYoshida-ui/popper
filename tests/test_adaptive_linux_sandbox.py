"""Real kernel gates; never replace Linux isolation with a scripted executor."""
import os
from pathlib import Path
import sys
import pytest
from tests.test_adaptive_runtime_v2 import make_runtime, problem, experiment, SOURCE
from popper.core import digest
from popper.research.adaptive_sandbox import strict_executor


@pytest.fixture
def isolated(tmp_path):
    available=(sys.platform=='linux' and os.getuid()!=0 and Path('/sys/fs/cgroup/popper-adaptive').is_dir())
    if not available:
        if os.environ.get('POPPER_REQUIRE_ADAPTIVE_SANDBOX')=='1':pytest.fail('Required adaptive sandbox/cgroup is unavailable')
        pytest.skip('Non-root Linux with delegated adaptive cgroup required')
    r=make_runtime(tmp_path);r.worker.executor=strict_executor
    r.apply(problem(),'problem')
    yield r
    r.close()


def test_real_kernel_execution_and_one_confirmation(isolated):
    r=isolated;r.apply(experiment(),'candidate')
    attempt=r.records('attempt_completed')[0]['data']
    assert attempt['status']=='succeeded',attempt
    selection=r.freeze_selection('kernel-gate')
    result=r.confirm({r.study_id:digest(selection)})
    assert result['judgment']=='supported'
    receipts=r.worker.list_receipts()
    assert all(x['execution_backend']=='linux_bubblewrap_whitelist_v1' for x in receipts)
    before=len(r.store.list('run'))
    assert r.confirm({r.study_id:digest(selection)})==result
    assert len(r.store.list('run'))==before


def test_private_inputs_credentials_network_and_pip_are_unavailable(isolated,monkeypatch):
    r=isolated;monkeypatch.setenv('POPPER_API_KEY','unit-test-private-key')
    check=f'''import os,socket,importlib.util
from pathlib import Path
assert 'POPPER_API_KEY' not in os.environ
assert not Path({str(r.paths['confirmation'])!r}).exists()
assert not Path({str(Path(__file__).parents[1]/'popper/research/prediction_comparison.py')!r}).exists()
assert importlib.util.find_spec('pip.__main__') is None
s=socket.socket();s.settimeout(1)
try:s.connect(('1.1.1.1',80));raise RuntimeError('network escaped namespace')
except OSError:pass
finally:s.close()
'''
    r.apply(experiment(check+SOURCE.replace('PRED',"r['features'][0]")),'isolation')
    attempt=r.records('attempt_completed')[0]['data']
    if attempt['status']!='succeeded':
        import json
        print(json.dumps(attempt))
    assert attempt['status']=='succeeded',attempt


def test_process_tree_limit_is_enforced(isolated):
    source='''import subprocess,sys
children=[];limited=False
try:
 for _ in range(48):
  try:children.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']))
  except BlockingIOError:limited=True;break
 assert limited,repr([(p.pid,p.poll()) for p in children])
finally:
 for child in children:child.terminate()
 for child in children:child.wait()
'''+SOURCE.replace('PRED',"r['features'][0]")
    isolated.apply(experiment(source),'pids')
    attempt=isolated.records('attempt_completed')[0]['data']
    if attempt['status']!='succeeded':
        import json
        print(json.dumps(attempt))
    assert attempt['status']=='succeeded',attempt
