"""LAN acceptance: staged bytes are not an admission or a compute failure.

Synthetic local files exercise the actual transfer/checksum subprocesses; agent
and container boundaries are controlled to test policy/capacity races, not LAN speed.
"""
import hashlib
import json
from unittest.mock import MagicMock
import pytest
from scripts import swarm_runtime as runtime


def ready(machine='machine', accepting=True):
    return {'machine':machine,'ready':True,'protocol':'lifetech-agent.v1',
            'worker_policy':{'accepting_jobs':accepting},
            'capabilities':['renewable-resources','docker-workers','generic-workers']}


def install_client(monkeypatch, statuses, available=4, acquired=True):
    events=[]; statuses=iter(statuses)
    class Client:
        def __init__(self,host):pass
        def call(self,op,args=None,**kw):
            events.append((op,args))
            if op=='status':return next(statuses)
            if op=='resource.capacity':return {'available_mb':8192,'available_cpus':available}
            if op=='resource.acquire':return {'acquired':acquired}
            return {}
        def close(self):pass
    monkeypatch.setattr(runtime,'AgentClient',Client)
    return events


@pytest.mark.parametrize('changed_identity',[False,True])
def test_policy_or_identity_change_after_admission_never_starts_compute(tmp_path,monkeypatch,changed_identity):
    later=ready(machine='replacement') if changed_identity else ready(accepting=False)
    events=install_client(monkeypatch,[ready(),later])
    launch=MagicMock();monkeypatch.setattr(runtime.subprocess,'Popen',launch)
    error=RuntimeError if changed_identity else runtime.CapacityWait
    with pytest.raises(error):
        runtime.run_job({'host':'worker','local':True,'machine':'machine'},'pinned','align',[],str(tmp_path),2,2048,tmp_path/'job.json',group='lan-tests')
    launch.assert_not_called()
    assert [op for op,_ in events].count('resource.acquire')==1
    assert events[-1][0]=='resource.release'


def test_capacity_race_is_requeued_without_starting_or_losing_staged_bytes(tmp_path,monkeypatch):
    staged=tmp_path/'verified.fastq';staged.write_bytes(b'synthetic verified reads')
    events=install_client(monkeypatch,[ready()],acquired=False)
    launch=MagicMock();monkeypatch.setattr(runtime.subprocess,'Popen',launch)
    with pytest.raises(runtime.CapacityWait):
        runtime.run_job({'host':'worker','local':True,'machine':'machine'},'pinned','align',[(staged,'/reads',True)],str(tmp_path),2,2048,tmp_path/'job.json',group='lan-tests')
    launch.assert_not_called()
    assert staged.read_bytes()==b'synthetic verified reads'
    assert events[-1][0]=='resource.release'


def test_busy_network_does_not_reject_verified_compute_and_cache_survives_pause(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    data=b'synthetic paired reads\n'*10000;source=tmp_path/'source.gz';source.write_bytes(data)
    install_client(monkeypatch,[ready()])
    receipt={'path':str(source),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    target={'host':'target','local':True,'cache':str(tmp_path/'cache'),'machine':'machine'}
    copied=runtime.copy_verified_file({'host':'source','local':True,'machine':'machine'},target,receipt,resume=True)
    # Owner pauses after transfer: retain the validated cache for the next try.
    install_client(monkeypatch,[ready(accepting=False)])
    with pytest.raises(runtime.CapacityWait):
        runtime.run_job(target,'pinned','align',[],str(tmp_path),2,2048,tmp_path/'job.json',group='lan-tests')
    from pathlib import Path
    assert Path(copied['path']).read_bytes()==data
    busy=ready();busy.update(network_mbps=940,link_speed_mbps=1000)
    events=install_client(monkeypatch,[busy,busy])
    process=MagicMock();process.wait.return_value=0
    launch=MagicMock(return_value=process);monkeypatch.setattr(runtime.subprocess,'Popen',launch)
    monkeypatch.setattr(runtime,'cleanup_container',lambda *args:True)
    runtime.run_job(target,'pinned','align',[(copied['path'],'/reads',True)],str(tmp_path),2,2048,tmp_path/'job.json',group='lan-tests')
    launch.assert_called_once()
    assert json.loads((tmp_path/'job.json').read_text())['phase']=='complete'
    assert events[-1][0]=='resource.release'


def test_verified_transfer_receipt_detects_same_size_cache_corruption(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('HOME',str(tmp_path))
    install_client(monkeypatch,[ready(),ready(),ready()])
    source=tmp_path/'source';source.write_bytes(b'abcdefgh')
    receipt={'path':str(source),'bytes':8,'sha256':hashlib.sha256(b'abcdefgh').hexdigest()}
    owner={'host':'source','local':True,'machine':'machine'}
    target={'host':'target','local':True,'cache':str(tmp_path/'cache')}
    out=runtime.copy_verified_file(owner,target,receipt)
    cached=Path(out['path']);proof=cached.with_name(cached.name+'.verified.json')
    assert proof.exists()
    before=json.loads(proof.read_text())
    assert runtime.copy_verified_file(owner,target,receipt)['path']==str(cached)
    cached.write_bytes(b'XXXXXXXX')
    runtime.copy_verified_file(owner,target,receipt)
    assert cached.read_bytes()==b'abcdefgh'
    assert json.loads(proof.read_text())['identity']!=before['identity']
