"""Produce platform evidence from actual commands, before paid transfer evaluation."""
from __future__ import annotations
import argparse
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from popper.core import ProtocolError,file_hash,write_json
from evaluation.run_transfer_pilot import ROOT,snapshot


def test_summary(path):
    tree=ET.parse(path)
    suites=list(tree.getroot().iter('testsuite'))
    return {key:sum(int(s.get(key,0)) for s in suites) for key in ('tests','failures','errors','skipped')}


def validate(name,out):
    out=Path(out).resolve(); out.mkdir(parents=True,exist_ok=True)
    native=platform.system()
    if (name=='windows' and native!='Windows') or (name in {'linux','linux-sandbox'} and native!='Linux'):
        raise ProtocolError('门禁必须在相应原生系统执行')
    if native=='Linux':
        if (name!='docker' and os.getuid()==0) or str(ROOT).startswith(('/mnt/','/tmp/')):
            raise ProtocolError('Linux 验证必须由非 root 用户在原生文件系统执行')
        fs=subprocess.check_output(['stat','-f','-c','%T',str(ROOT)],text=True).strip()
        if fs not in {'ext2/ext3','btrfs','xfs','overlayfs'}: raise ProtocolError('Linux 文件系统不是已认可的原生系统')
    else: fs='Windows'
    initial=snapshot(); log=out/(name+'.log'); artifacts=[]; summary=None
    env=dict(os.environ); env.pop('POPPER_API_KEY',None)
    env['PYTHONIOENCODING']='utf-8'
    if name in {'windows','linux'}:
        junit=out/(name+'-junit.xml')
        cmd=[sys.executable,'-m','pytest','-q','-rfEs','--tb=short','--junitxml',str(junit)]
        if name=='linux': env['POPPER_REQUIRE_SANDBOX']='1'
    elif name=='linux-sandbox':
        cmd=[sys.executable,'-X','utf8',str(ROOT/'.github/scripts/e2e_sandbox_experiment.py'),
             '--expect-backend','linux_bubblewrap','--workspace',str(out/'sandbox-e2e'),'--keep']
    elif name=='docker':
        # Administrative build only. Container smoke itself runs as the Dockerfile's non-root runner.
        commands=[['docker','pull','python:3.12-slim']]
        with log.open('w',encoding='utf-8') as stream:
            pulled=subprocess.run(commands[0],env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=600)
            if pulled.returncode==0:
                image=json.loads(subprocess.check_output(['docker','image','inspect','python:3.12-slim'],env=env,text=True))[0]
                base=image['RepoDigests'][0]
                commands.append(['docker','build','-f','scripts/Dockerfile.transfer','--build-arg','BASE_IMAGE='+base,'-t','popper-transfer:validation','.'])
                built=subprocess.run(commands[-1],cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=600)
                if built.returncode==0:
                    commands.append(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','popper-transfer:validation','--help'])
                    completed=subprocess.run(commands[-1],env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=60)
                else: completed=built
            else: completed=pulled; base=None
        unchanged=initial==snapshot()
        gate={'schema':'transfer-environment-gate/v1','name':name,'status':'passed' if completed.returncode==0 and unchanged else 'failed',
            'returncode':completed.returncode,'implementation':initial,'source_unchanged':unchanged,'commands':commands,
            'base_image_digest':base,'mounts':[],'network_smoke':'none','container_user':'runner',
            'artifacts':[{'path':str(log),'sha256':file_hash(log)}]}
        write_json(out/(name+'.json'),gate); print(json.dumps({'name':name,'status':gate['status']}))
        return 0 if gate['status']=='passed' else 1
    else: raise ProtocolError('未知环境门禁')
    with log.open('w',encoding='utf-8') as stream:
        completed=subprocess.run(cmd,cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=3600)
    artifacts.append({'path':str(log),'sha256':file_hash(log)})
    if name in {'windows','linux'} and junit.exists():
        summary=test_summary(junit); artifacts.append({'path':str(junit),'sha256':file_hash(junit)})
    source_same=initial==snapshot()
    passed=completed.returncode==0 and source_same
    if name in {'windows','linux'}: passed=passed and summary is not None and summary['tests']>0 and summary['errors']==summary['failures']==0
    if name=='linux-sandbox':
        text=log.read_text(encoding='utf-8')
        passed=passed and '[e2e-sandbox] 一整个 --sandbox' in text
    gate={'schema':'transfer-environment-gate/v1','name':name,'status':'passed' if passed else 'failed',
          'command':cmd,'returncode':completed.returncode,'implementation':initial,'source_unchanged':source_same,
          'platform':platform.platform(),'python':platform.python_version(),'uid':os.getuid() if native=='Linux' else None,
          'filesystem':fs,'tests':summary,'artifacts':artifacts,'raw_credentials_forwarded':False}
    write_json(out/(name+'.json'),gate)
    print(json.dumps({k:gate[k] for k in ('name','status','returncode','tests')},ensure_ascii=False))
    return 0 if passed else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('name',choices=('windows','linux','linux-sandbox','docker'))
    parser.add_argument('--out',type=Path,required=True); args=parser.parse_args()
    sys.exit(validate(args.name,args.out))
