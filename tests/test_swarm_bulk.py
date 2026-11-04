import json
import subprocess
from unittest.mock import Mock
import pytest
from scripts import swarm_bulk as bulk


def setup(tmp_path,monkeypatch,size=64*1024**2):
    monkeypatch.setattr(bulk,'BASE',tmp_path)
    peers={'a':{'machine':'A','root':'/a'},'b':{'machine':'B','root':'/b'}}
    (tmp_path/'config.json').write_text(json.dumps({'peers':peers}))
    source={'host':'a','machine':'A'};dest={'host':'b','machine':'B'}
    receipt={'path':'/source','sha256':'a'*64,'bytes':size}
    return source,dest,receipt


def test_large_object_prefers_direct_https(tmp_path,monkeypatch):
    source,dest,receipt=setup(tmp_path,monkeypatch)
    run=Mock(side_effect=[subprocess.CompletedProcess([],0,json.dumps({'object':'x'})),subprocess.CompletedProcess([],0,'')])
    monkeypatch.setattr(bulk.subprocess,'run',run)
    commands=[]
    def execute(host,args):commands.append((host['host'],args));return args
    assert bulk.try_copy(source,dest,receipt,'/cache','{}',execute)=='mtls-https'
    assert [x[0] for x in commands]==['a','b']
    assert commands[0][1][1]=='/a/current/bulk.py'
    assert 'receive' in commands[1][1]


@pytest.mark.parametrize('condition',['small','missing','identity'])
def test_unavailable_or_small_keeps_ssh_path(tmp_path,monkeypatch,condition):
    source,dest,receipt=setup(tmp_path,monkeypatch,size=1 if condition=='small' else 64*1024**2)
    if condition=='missing':(tmp_path/'config.json').unlink()
    if condition=='identity':dest['machine']='replacement'
    run=Mock();monkeypatch.setattr(bulk.subprocess,'run',run)
    assert bulk.try_copy(source,dest,receipt,'/cache','{}',lambda h,c:c) is None
    run.assert_not_called()


@pytest.mark.parametrize('which',[0,1])
def test_integrity_failure_never_falls_back(tmp_path,monkeypatch,which):
    source,dest,receipt=setup(tmp_path,monkeypatch)
    results=[subprocess.CompletedProcess([],0,json.dumps({'object':'x'}))]*which+[subprocess.CompletedProcess([],78,'')]
    monkeypatch.setattr(bulk.subprocess,'run',Mock(side_effect=results))
    with pytest.raises(bulk.IntegrityError):bulk.try_copy(source,dest,receipt,'/cache','{}',lambda h,c:c)


def test_connection_failure_allows_pinned_ssh_fallback(tmp_path,monkeypatch):
    source,dest,receipt=setup(tmp_path,monkeypatch)
    monkeypatch.setattr(bulk.subprocess,'run',Mock(side_effect=[subprocess.CompletedProcess([],0,json.dumps({'object':'x'})),subprocess.CompletedProcess([],255,'')]))
    assert bulk.try_copy(source,dest,receipt,'/cache','{}',lambda h,c:c) is None


def test_https_partial_resumes_through_ssh_without_restarting_bytes(tmp_path,monkeypatch):
    import hashlib
    from scripts import swarm_runtime as runtime
    monkeypatch.setenv('HOME',str(tmp_path))
    data=b'synthetic read data\n'*10000;source=tmp_path/'source';source.write_bytes(data)
    receipt={'path':str(source),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    owner={'host':'source','local':True,'machine':'machine'}
    target={'host':'target','local':True,'cache':str(tmp_path/'cache'),'machine':'machine'}
    client=Mock();client.call.return_value={'machine':'machine'}
    monkeypatch.setattr(runtime,'AgentClient',lambda h:client)
    def interrupted(src,dst,receipt,path,metadata,execute):
        from pathlib import Path
        partial=Path(path).parent/'.rsync-partial'/Path(path).name
        partial.parent.mkdir();partial.write_bytes(data[:12345]);return None
    monkeypatch.setattr(bulk,'try_copy',interrupted)
    result=runtime.copy_verified_file(owner,target,receipt,resume=True)
    from pathlib import Path
    assert Path(result['path']).read_bytes()==data
    assert not (Path(result['path']).parent/'.rsync-partial'/'source').exists()


def test_reference_indexes_use_shared_verified_transport(tmp_path,monkeypatch):
    import hashlib
    from scripts import wgs_recall_swarm as recall
    from scripts import swarm_runtime as runtime
    reference=tmp_path/'reference';reference.mkdir()
    fasta=reference/'reference.fa';fasta.write_bytes(b'>synthetic\nACGT\n')
    manifest={'files':{'reference.fa':{'bytes':fasta.stat().st_size,'sha256':hashlib.sha256(fasta.read_bytes()).hexdigest()}}}
    (reference/'attestation.json').write_text(json.dumps(manifest))
    config=tmp_path/'peers.json';config.write_text(json.dumps({'name':'owner','peers':{'owner':'10.42.0.1'}}))
    monkeypatch.setattr(runtime,'CONFIG',config)
    client=Mock();client.call.return_value={'machine':'owner-machine'}
    monkeypatch.setattr(recall,'AgentClient',lambda h:client)
    moves=[]
    def copy(source,destination,receipt,**kw):
        moves.append((source,destination,receipt));return {**receipt,'path':'/verified/'+receipt['sha256']}
    monkeypatch.setattr(recall,'copy_receipt',copy)
    run=Mock(return_value=subprocess.CompletedProcess([],0,''));monkeypatch.setattr(recall.subprocess,'run',run)
    target={'host':'worker','local':False,'cache':'/cache','machine':'worker-machine'}
    monkeypatch.setattr(recall.distributed,'shell_command',lambda h,c:c)
    recall.distribute_reference(target,reference)
    assert {x[2]['path'] for x in moves}=={str(fasta),str(reference/'attestation.json')}
    assert all(call.args[0][0]!='rsync' for call in run.call_args_list)


def test_identical_offer_reuses_manifest_but_changed_client_rewrites(tmp_path,monkeypatch):
    import sys
    from scripts import swarm_tls_transfer as transfer
    monkeypatch.setitem(sys.modules,'transfer',transfer)
    monkeypatch.setattr(bulk,'BASE',tmp_path)
    (tmp_path/'objects').mkdir()
    source=tmp_path/'source';source.write_bytes(b'payload')
    config={'machine':'source','peers':{'worker':{'certificate_sha256':'first'}}}
    write=Mock(wraps=transfer.atomic_json);monkeypatch.setattr(transfer,'atomic_json',write)
    first=bulk.offer(config,source,'a'*64,7,'worker','source')
    assert bulk.offer(config,source,'a'*64,7,'worker','source')==first
    assert write.call_count==1
    config['peers']['worker']['certificate_sha256']='replacement'
    assert bulk.offer(config,source,'a'*64,7,'worker','source')==first
    assert write.call_count==2
    assert json.loads((tmp_path/'objects'/(first['object']+'.json')).read_text())['clients']==['replacement']
