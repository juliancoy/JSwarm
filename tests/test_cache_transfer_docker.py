"""Real direct TLS stream between isolated, RAM-backed Docker cache fixtures."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import uuid

import pytest
from scripts import swarm_cache_transfer as transfer
from scripts.swarm_shared_cache import contract,SEAL,OWNER
from scripts.genomics_followup_fleet import RECEIVE


def test_direct_cache_transfer_stays_in_ram_and_verifies_contents(tmp_path,monkeypatch):
    docker=pytest.importorskip('docker');client=docker.from_env(timeout=30)
    try:
        client.ping()
        source_image=client.images.get('organization-launcher:webrtc-1').id
        target_image=client.images.get('organization-swarm-node:1').id
    except docker.errors.DockerException as error:
        client.close();pytest.skip('Local cache transport fixture images unavailable: '+str(error))
    if source_image==target_image:client.close();pytest.skip('Distinct fixture images required on one Docker daemon')
    token=uuid.uuid4().hex;data=(token.encode()+b'\n')*16384
    files={'fixture-'+token:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}
    containers=[]
    try:
        caches=[]
        for image in (source_image,target_image):
            key,value=contract(image,files);cache={**value,'key':key,'container':'swarm-job-'+key[:32]}
            c=client.containers.run(image,['python3','-c','import time;time.sleep(600)'],detach=True,
                name=cache['container'],network_mode='none',read_only=True,shm_size='8m',
                mem_limit='128m',memswap_limit='128m',pids_limit=64,cap_drop=['ALL'],
                security_opt=['no-new-privileges'],labels={OWNER:key,'org.lifetech.test-run':token})
            containers.append(c);caches.append(cache)
        source,destination=caches
        write=['docker','exec','-i',source['container'],'python3','-c',
               "import pathlib,sys;pathlib.Path('/dev/shm/'+sys.argv[1]).write_bytes(sys.stdin.buffer.read())",next(iter(files))]
        subprocess.run(write,input=data,check=True,capture_output=True,timeout=20)
        containers[0].reload();attrs=containers[0].attrs
        seal={**source,'container_id':attrs['Id'],'started_at':attrs['State']['StartedAt']}
        result=containers[0].exec_run(['python3','-c',SEAL,json.dumps(seal)])
        assert result.exit_code==0,result.output
        helper=client.containers.run(target_image,['python3','-c','import time;time.sleep(600)'],detach=True,
            network_mode='host',read_only=True,tmpfs={'/tmp':'rw,nosuid,nodev,size=16m,mode=1777'},
            mem_limit='256m',memswap_limit='256m',pids_limit=128,cap_drop=['ALL'],
            security_opt=['no-new-privileges'],labels={'org.lifetech.test-run':token},
            volumes={'/var/run/docker.sock':{'bind':'/var/run/docker.sock','mode':'rw'},
                     '/etc/machine-id':{'bind':'/etc/machine-id','mode':'ro'}})
        containers.append(helper)
        # The subprocess adapter substitutes fixture owners for signed API
        # commands; the complete production transfer runtime is unchanged.
        monkeypatch.setattr(transfer,'shell_command',lambda host,argv:['docker','exec','-i',helper.id,*argv])
        probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try:probe.connect(('10.42.0.1',8790));address=probe.getsockname()[0]
        finally:probe.close()
        config=tmp_path/'peers.json';config.write_text(json.dumps({'peers':{'source':address,'target':address}}))
        import scripts.swarm_runtime as runtime
        monkeypatch.setattr(runtime,'CONFIG',config)
        machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
        result=transfer.copy({'host':'source','machine':machine},source,{'host':'target','machine':machine},destination,RECEIVE)
        assert result['transport']=='direct-pinned-tls' and result['bytes']==len(data)
        check=containers[1].exec_run(['python3','-c',
            "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/dev/shm/"+next(iter(files))+"').read_bytes()).hexdigest())"])
        assert check.exit_code==0 and check.output.decode().strip()==hashlib.sha256(data).hexdigest()
        # Only the ordinary cache seal can publish a destination cache.
        assert containers[1].exec_run(['test','-e','/dev/shm/.ready.json']).exit_code!=0
        # A valid source seal changing before transfer cannot be trusted.
        changed=containers[0].exec_run(['python3','-c',
            "import pathlib;p=pathlib.Path('/dev/shm/"+next(iter(files))+"');p.chmod(0o600);p.write_bytes(b'changed')"])
        assert changed.exit_code==0
        with pytest.raises((ValueError,RuntimeError)):
            transfer.copy({'host':'source','machine':machine},source,{'host':'target','machine':machine},destination,RECEIVE)
    finally:
        for c in reversed(containers):c.remove(force=True)
        client.close()
