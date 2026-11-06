import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts import launcher_agent as node


def fixture(tmp_path):
    key='a'*64;cid='b'*64
    blob=tmp_path/'index';blob.write_bytes(b'abc');blob.chmod(0o444);s=blob.stat()
    identity={'key':key,'container_id':cid,'started_at':'2026-11-04T00:00:00Z'}
    files={'index':{'bytes':3,'sha256':'c'*64}}
    (tmp_path/'.ready.json').write_text(json.dumps({**identity,'files':files,'identities':{'index':[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]}}))
    return identity,files


def probe(tmp_path,identity):
    import subprocess,sys
    source=node.RAM_CACHE_PROBE.replace("pathlib.Path('/dev/shm')",f'pathlib.Path({str(tmp_path)!r})')
    return subprocess.run([sys.executable,'-c',source,json.dumps(identity)],capture_output=True,text=True)


def test_sealed_cache_inventory_needs_no_payload_rescan(tmp_path):
    identity,files=fixture(tmp_path)
    result=probe(tmp_path,identity)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==files


@pytest.mark.parametrize('change',['different-owner','writable','changed-file','symlink','unsealed'])
def test_cache_inventory_rejects_stale_or_unsealed_objects(tmp_path,change):
    identity,_=fixture(tmp_path);blob=tmp_path/'index'
    if change=='different-owner':identity['container_id']='d'*64
    elif change=='writable':blob.chmod(0o644)
    elif change=='changed-file':blob.unlink();blob.write_bytes(b'abc');blob.chmod(0o444)
    elif change=='symlink':blob.rename(tmp_path/'target');blob.symlink_to(tmp_path/'target')
    else:(tmp_path/'.ready.json').unlink()
    assert probe(tmp_path,identity).returncode!=0


@pytest.mark.parametrize('backing,expected',[('tmpfs',1),('ext4',0)])
def test_inventory_only_credits_labelled_docker_tmpfs(tmp_path,monkeypatch,backing,expected):
    identity,files=fixture(tmp_path);key=identity['key'];volume='swarm-cache-'+key[:32];name='swarm-job-'+key[:32]
    row={'Id':identity['container_id'],'Name':'/'+name,'Image':'sha256:'+'e'*64,
         'Config':{'Labels':{'org.lifetech.cache-owner':key,'org.lifetech.group':'shared-cache'}},
         'State':{'Running':True,'StartedAt':identity['started_at']},
         'Mounts':[{'Type':'volume','Name':volume,'Destination':'/dev/shm'}]}
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='ps':output=row['Id']
        elif argv[1]=='inspect':output=json.dumps([row])
        elif argv[1]=='volume':output=json.dumps([{'Driver':'local','Options':{'type':backing,'device':backing},'Labels':{'org.lifetech.cache-owner':key}}])
        else:output=json.dumps(files)
        return SimpleNamespace(returncode=0,stdout=output)
    monkeypatch.setattr(node.subprocess,'run',run)
    assert len(node.verified_ram_caches()['caches'])==expected
    if backing!='tmpfs':assert all(c[1]!='exec' for c in calls)
