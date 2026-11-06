import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from scripts import swarm_gpu_runtime as runtime


def test_node_bundle_installation_is_atomic_idempotent_and_detects_tampering(tmp_path):
    data=b'pinned runtime binary';sha=hashlib.sha256(data).hexdigest();machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    payload={'machine':machine,'key':'a'*64,'manifest':{'tool':sha},'files':{'tool':base64.b64encode(data).decode()}}
    def run():return subprocess.run([sys.executable,'-c',runtime.INSTALL],input=json.dumps(payload),text=True,capture_output=True,env={**os.environ,'HOME':str(tmp_path)})
    first=run();assert first.returncode==0,first.stderr
    assert run().stdout==first.stdout
    target=Path(json.loads(first.stdout)['runtime_directory'])/'tool';target.chmod(0o600);target.write_bytes(b'changed')
    assert run().returncode!=0


def test_runtime_source_rejects_unpinned_or_escaping_artifacts():
    with pytest.raises(ValueError):runtime.install({}, {'image':'mutable:tag','files':{'tool':'/usr/bin/tool'},'sha256':{'tool':'a'*64}})
    with pytest.raises(ValueError):runtime.install({}, {'image':'sha256:'+'b'*64,'files':{'../escape':'/usr/bin/tool'},'sha256':{'../escape':'a'*64}})


@pytest.mark.parametrize("ipv4_unreachable",[False,True])
def test_runtime_bootstrap_uses_current_pinned_owner_address(tmp_path,monkeypatch,ipv4_unreachable):
    from scripts import swarm_runtime
    from agent_updates import LauncherUpdates
    from types import SimpleNamespace
    data=b'qualified binary';sha=hashlib.sha256(data).hexdigest()
    config=tmp_path/'peers.json';config.write_text(json.dumps({'peers':{'node':'10.42.0.2'}}))
    monkeypatch.setattr(swarm_runtime,'CONFIG',config)
    seen=[];addresses=[]
    def command(self,host,source):
        addresses.append(host['lan_address'])
        assert source==runtime.INSTALL
        return ['owner-bootstrap']
    monkeypatch.setattr(LauncherUpdates,'command',command)
    def output(argv,**kwargs):
        if argv[:2]==['docker','create']:return 'container\n'
        assert argv==['owner-bootstrap']
        if ipv4_unreachable and len(addresses)==1:raise subprocess.CalledProcessError(255,argv)
        seen.append(json.loads(kwargs['input']))
        return '{"execution_owner":"jswarm-node"}'
    def run(argv,**kwargs):
        if argv[:2]==['docker','cp']:Path(argv[-1]).write_bytes(data)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runtime.subprocess,'check_output',output)
    monkeypatch.setattr(runtime.subprocess,'run',run)
    result=runtime.install({'host':'node','machine':'a'*64}, {'image':'sha256:'+'b'*64,'files':{'tool':'/tool'},'sha256':{'tool':sha}})
    assert result['execution_owner']=='jswarm-node'
    assert seen[0]['manifest']=={'tool':sha}

    assert addresses==(['10.42.0.2','node'] if ipv4_unreachable else ['10.42.0.2'])
