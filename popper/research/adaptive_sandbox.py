"""Minimal Linux mount whitelist for generated regression code."""
import os
from pathlib import Path
import subprocess
import sys
import importlib.util
import sysconfig
import uuid
import time
from ..core import ProtocolError

BOOTSTRAP = '''import os,resource,runpy,sys
resource.setrlimit(resource.RLIMIT_AS,(4*1024**3,4*1024**3))
resource.setrlimit(resource.RLIMIT_CPU,(300,300))
resource.setrlimit(resource.RLIMIT_FSIZE,(64*1024**2,64*1024**2))
available=sorted(os.sched_getaffinity(0));os.sched_setaffinity(0,available[:2])
sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')
'''


def strict_executor(command, workspace, env, stdout, stderr, spec, *, cancel_event=None):
    if sys.platform != 'linux' or os.getuid() == 0 or not spec.sandboxed:
        raise ProtocolError('Adaptive generated code requires non-root Linux sandbox execution')
    cgroup_root=Path('/sys/fs/cgroup/popper-adaptive')
    if not cgroup_root.is_dir():
        raise ProtocolError('Delegated adaptive cgroup is required for aggregate memory/PID limits')
    cgroup=cgroup_root/('job-'+uuid.uuid4().hex)
    cgroup.mkdir()
    for name,value in [('memory.max',str(4*1024**3)),('memory.swap.max','0'),
                       ('pids.max',str(spec.max_processes)),('cpu.max','200000 100000')]:
        (cgroup/name).write_text(value)
    argv = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--proc', '/proc',
            '--dev', '/dev', '--tmpfs', '/tmp']
    roots = ['/usr', '/lib', '/lib64', str(Path(sys.prefix).resolve())]
    mounted = []
    for root in roots:
        if Path(root).exists() and root not in mounted:
            argv.extend(['--ro-bind', root, root])
            mounted.append(root)
    if Path('/etc/ld.so.cache').exists():
        argv.extend(['--ro-bind', '/etc/ld.so.cache', '/etc/ld.so.cache'])
    # A wheel installation places the evaluator and sealed generator code inside
    # the otherwise permitted dependency runtime. Keep that host package hidden.
    internal_roots={Path(__file__).resolve().parents[1]}
    internal_roots.update(Path(sysconfig.get_path(role))/'popper' for role in ('purelib','platlib'))
    for internal in sorted(internal_roots):
        if internal.is_dir() and any(internal.is_relative_to(Path(root)) for root in mounted):
            argv.extend(['--tmpfs',str(internal)])
    # Dependency installation is unavailable inside the candidate namespace.
    for module in ('pip', 'ensurepip'):
        found = importlib.util.find_spec(module)
        if found and found.submodule_search_locations:
            for location in found.submodule_search_locations:
                argv.extend(['--tmpfs', location])
    for root in (Path(sys.prefix)/'bin', Path('/usr/bin')):
        for name in ('pip','pip3','pip3.12'):
            if (root/name).exists():argv.extend(['--ro-bind','/dev/null',str(root/name)])
    for name in ('code', 'inputs'):
        argv.extend(['--ro-bind', str(workspace / name), '/workspace/' + name])
    argv.extend(['--dir','/workspace/outputs','--tmpfs','/workspace/tmp'])
    for name in spec.outputs:
        destination=workspace/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.touch(exist_ok=True)
        argv.extend(['--bind',str(destination),'/workspace/'+name])
    translated = [str(v).replace(str(workspace), '/workspace') for v in command[1:]]
    argv.extend(['--chdir', '/workspace', '--', command[0], '-c', BOOTSTRAP, *translated])
    env = {k: v for k, v in env.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
    env = {k:str(v).replace(str(workspace),'/workspace') for k,v in env.items()}
    env.update(OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2', MKL_NUM_THREADS='2',
               HOME='/workspace/tmp', TMPDIR='/workspace/tmp')
    # Move the launcher into its resource group before it can start candidate code.
    launcher='import os,sys\nf=open(sys.argv[1],"w");f.write(str(os.getpid())+"\\n");f.close()\nassert os.path.basename(os.path.dirname(sys.argv[1])) in open("/proc/self/cgroup").read()\nos.execvp(sys.argv[2],sys.argv[2:])'
    proc = subprocess.Popen([sys.executable,'-c',launcher,str(cgroup/'cgroup.procs'),*argv], env=env, stdout=stdout, stderr=stderr,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline=time.monotonic()+spec.timeout_seconds
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                raise ProtocolError('Adaptive execution cancelled')
            if time.monotonic()>deadline:
                raise subprocess.TimeoutExpired(argv,spec.timeout_seconds)
            time.sleep(.05)
        if proc.returncode != 0:
            raise ProtocolError('Sandboxed candidate failed; see the retained worker stderr')
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        (cgroup/'cgroup.kill').write_text('1')
        cgroup.rmdir()


strict_executor.execution_backend = 'linux_bubblewrap_whitelist_v1'
