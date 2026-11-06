import hashlib
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import threading
import pytest
from scripts import swarm_source_service as service
from scripts import swarm_tls_transfer
from scripts.swarm_source_client import SourceClient


@pytest.fixture
def source(tmp_path,monkeypatch):
    def openssl(*args):subprocess.run(['openssl',*args],cwd=tmp_path,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    openssl('req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout','ca.key','-out','ca.crt','-subj','/CN=fixture-ca','-days','1')
    fingerprints={}
    for name in ('source','controller','other'):
        openssl('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',name+'.key','-out',name+'.csr','-subj','/CN='+name)
        (tmp_path/(name+'.ext')).write_text('extendedKeyUsage=serverAuth,clientAuth\nsubjectAltName=IP:127.0.0.1\n')
        openssl('x509','-req','-in',name+'.csr','-CA','ca.crt','-CAkey','ca.key','-CAcreateserial','-out',name+'.crt','-days','1','-extfile',name+'.ext')
        fingerprints[name]=hashlib.sha256(ssl.PEM_cert_to_DER_cert((tmp_path/(name+'.crt')).read_text())).hexdigest()
    peers={name:{'machine':name*16,'address':'127.0.0.1','certificate_sha256':fingerprints[name]} for name in fingerprints}
    bases={}
    for name in peers:
        base=tmp_path/name;base.mkdir();(base/'objects').mkdir();(base/'inboxes').mkdir()
        for src,dest in [('ca.crt','ca.crt'),(name+'.crt','identity.crt'),(name+'.key','identity.key')]:shutil.copyfile(tmp_path/src,base/dest)
        (base/'config.json').write_text(json.dumps({'name':name,'machine':peers[name]['machine'],'port':9448,'peers':peers}))
        bases[name]=base
    monkeypatch.setattr(service,'__file__',str(tmp_path/'source.py'))
    shutil.copyfile(Path(swarm_tls_transfer.__file__).with_name('swarm_bulk.py'),tmp_path/'bulk.py')
    monkeypatch.setitem(__import__('sys').modules,'transfer',swarm_tls_transfer)
    work=tmp_path/'work';shards=work/'shards';shards.mkdir(parents=True);outputs=tmp_path/'outputs';outputs.mkdir()
    for n in range(2):
        files=[]
        for mate in range(2):
            path=shards/f'{n:06d}-{mate}.gz';path.write_bytes(b'synthetic input'+bytes([n,mate]));files.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size})
        (shards/f'{n:06d}.json').write_text(json.dumps({'shard':n,'pairs':2,'generation':'existing-generation','files':files}))
    server=service.make_server(bases['source'],{'controller':'controller','inputs':[str(work)],'outputs':[str(outputs)]},'127.0.0.1',0)
    for name,base in bases.items():
        cfg=json.loads((base/'config.json').read_text());cfg['peers']['source']['source_port']=server.server_port;(base/'config.json').write_text(json.dumps(cfg))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield SourceClient('source',bases['controller']),work,outputs,bases
    finally:server.shutdown();server.server_close();thread.join()


def test_source_catalogue_and_offers_preserve_existing_identities(source,monkeypatch):
    client,work,outputs,bases=source
    from scripts import swarm_runtime
    monkeypatch.setattr(swarm_runtime,'AgentClient',lambda *a,**k:pytest.fail('Source API must not contact node command service'))
    rows=client.shards(str(work))['shards'];assert len(rows)==2
    plan=client.plan(str(work),'other',0)
    assert [r['id'] for r in plan['items']]==['000000-0','000000-1','000001-0','000001-1']
    first=plan['items'][0]['input']
    assert first['peer']=='source' and first['machine']=='source'*16
    path=rows[0]['files'][0]['path'];sha=rows[0]['files'][0]['sha256']
    expected=hashlib.sha256((path+'\0'+sha+'\0other').encode()).hexdigest()
    assert first['object']==expected
    assert client.plan(str(work),'other',0)['items'][0]['input']['object']==expected
    manifest=json.loads((bases['source']/'objects'/(expected+'.json')).read_text())
    cfg=json.loads((bases['source']/'config.json').read_text())
    assert manifest['clients']==[cfg['peers']['other']['certificate_sha256']]


def test_source_service_rejects_traversal_and_noncontroller_clients(source):
    client,work,outputs,bases=source
    with pytest.raises(RuntimeError,match='409'):client.call('/v1/shards',{'work':str(work/'..')})
    other=SourceClient('source',bases['other'])
    with pytest.raises(RuntimeError,match='403'):other.shards(str(work))
    outside=work.parent/'outside';outside.write_bytes(b'private')
    link=work/'escape';link.symlink_to(outside)
    with pytest.raises(RuntimeError,match='409'):client.offers('other',{'data':{'path':str(link),'sha256':'a'*64,'bytes':7}})
    (work/'shards/stream-verified.json').symlink_to(outside)
    with pytest.raises(RuntimeError,match='409'):client.shards(str(work))


def test_output_grants_keep_existing_job_key_and_restrict_destination(source):
    client,work,outputs,bases=source
    request={'destination':str(outputs/'existing-shard'),'peer':'other','job_key':'b'*64,'files':{'result.json':1024},'max_bytes':1024}
    first=client.grant(request);second=client.grant(request)
    assert first==second and first['job_key']=='b'*64 and first['peer']=='source'
    with pytest.raises(RuntimeError,match='409'):client.grant({**request,'destination':str(work)})


def test_paged_catalogue_does_not_duplicate_shards(source):
    client,work,_,_=source
    first=client.call('/v1/shards',{'work':str(work),'limit':1})
    second=client.call('/v1/shards',{'work':str(work),'limit':1,'start':first['next']})
    assert first['shards'][0]['shard']==0 and second['shards'][0]['shard']==1
    assert second['next'] is None
