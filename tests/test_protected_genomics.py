"""Synthetic end-to-end protection, real Crypt4GH, mTLS and RAM worker checks."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import time

import pytest

from scripts import swarm_crypt4gh as crypto
from scripts import swarm_data_authorization as auth
from scripts.swarm_protected_contract import job_key
from scripts.swarm_protected_sessions import ProtectedSessions
from scripts.protected_genomics_cli import approve
from scripts import protected_genomics_worker as worker
from scripts.protected_genomics import prepare
from scripts import swarm_source_service as service
from scripts import swarm_tls_transfer as transfer
from scripts.swarm_source_client import SourceClient


@pytest.fixture
def encrypted_source(tmp_path,monkeypatch):
    def openssl(*args):subprocess.run(['openssl',*args],cwd=tmp_path,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    openssl('req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout','ca.key','-out','ca.crt','-subj','/CN=synthetic-ca','-days','1','-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
    peers={};bases={}
    for name in ('source','controller','worker','other'):
        openssl('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',name+'.key','-out',name+'.csr','-subj','/CN='+name)
        (tmp_path/(name+'.ext')).write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth,clientAuth\nsubjectAltName=IP:127.0.0.1\n')
        openssl('x509','-req','-in',name+'.csr','-CA','ca.crt','-CAkey','ca.key','-CAcreateserial','-out',name+'.crt','-days','1','-extfile',name+'.ext')
        fingerprint=hashlib.sha256(ssl.PEM_cert_to_DER_cert((tmp_path/(name+'.crt')).read_text())).hexdigest()
        peers[name]={'machine':hashlib.sha256(name.encode()).hexdigest(),'address':'127.0.0.1','certificate_sha256':fingerprint}
        base=tmp_path/name;base.mkdir();bases[name]=base
        for original,target in [('ca.crt','ca.crt'),(name+'.crt','identity.crt'),(name+'.key','identity.key')]:shutil.copyfile(tmp_path/original,base/target)
    base=bases['source'];(base/'objects').mkdir();(base/'inboxes').mkdir();configuration=base/'source-authorization';configuration.mkdir(mode=0o700)
    custody=os.urandom(32);key_file=configuration/'custody.key';key_file.write_bytes(custody);key_file.chmod(0o600)
    payload=b'@synthetic\nACGTACGT\n+\nIIIIIIII\n'*6000
    plaintext=tmp_path/'original.fastq';plaintext.write_bytes(payload)
    inputs=tmp_path/'encrypted';inputs.mkdir();outputs=tmp_path/'results';outputs.mkdir()
    row=crypto.ingest(plaintext,inputs/'reads.c4gh',crypto.public_key(custody))
    row.update(plaintext_sha256=hashlib.sha256(payload).hexdigest(),plaintext_bytes=len(payload),key_file=str(key_file))
    catalogue=configuration/'catalogue.json';catalogue.write_text(json.dumps({'reads':row}));catalogue.chmod(0o600)
    auth_cfg={'issuer':'https://id.example','resource':'https://source.example/genomics',
              'introspection_endpoint':'https://id.example/oauth/mcp/introspect','secret_file':str(configuration/'resource.secret'),
              'approvals_file':str(configuration/'approvals.json')}
    result_private=os.urandom(32)
    contract={'version':'genomics.protected.v1','run_id':'e'*32,'peer':'worker','machine':peers['worker']['machine'],
              'image':'sha256:'+'f'*64,'argv':['python3','-c',
              'import os,pathlib; p=pathlib.Path(os.environ["INPUT_DIR"])/"reads"; (pathlib.Path(os.environ["OUTPUT_DIR"])/"summary.txt").write_text(str(len(p.read_bytes())))'],
              'inputs':{'reads':{'dataset':'reads',**{k:row[k] for k in ('sha256','bytes','plaintext_sha256','plaintext_bytes')}}},
              'outputs':{'summary.txt':1024},'result_recipient':crypto.public_key(result_private).hex(),
              'resources':{'cpus':1,'memory_mb':128},'scratch_mb':16}
    key=approve(contract,'owner:alice',time.time()+300,configuration/'approvals.json')
    claims={'active':True,'iss':auth_cfg['issuer'],'aud':auth_cfg['resource'],'sub':'owner:alice','scope':'genomics:data','exp':time.time()+300}
    monkeypatch.setattr(auth,'introspect',lambda cfg,token:claims if token=='user-token' else {'active':False})
    servers=[];threads=[]
    def serve(server):
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();servers.append(server);threads.append(thread)
    bulk=transfer.make_server('127.0.0.1',0,base/'objects',base/'identity.crt',base/'identity.key',base/'ca.crt');serve(bulk)
    for name,folder in bases.items():
        peers['source']['port']=bulk.server_port;peers['source']['output_port']=bulk.server_port
        (folder/'config.json').write_text(json.dumps({'name':name,'machine':peers[name]['machine'],'address':'127.0.0.1','port':bulk.server_port,'peers':peers}))
    monkeypatch.setattr(service,'__file__',str(tmp_path/'source.py'))
    shutil.copyfile(Path(transfer.__file__).with_name('swarm_bulk.py'),tmp_path/'bulk.py')
    monkeypatch.setitem(__import__('sys').modules,'transfer',transfer)
    policy={'controller':'controller','inputs':[str(inputs)],'outputs':[str(outputs)],'authorization':auth_cfg,'encrypted_catalogue':str(catalogue)}
    server=service.make_server(base,policy,'127.0.0.1',0);serve(server)
    for name,folder in bases.items():
        cfg=json.loads((folder/'config.json').read_text());cfg['peers']['source']['source_port']=server.server_port;(folder/'config.json').write_text(json.dumps(cfg))
    controller=SourceClient('source',bases['controller'],token_provider=lambda:'user-token',job_key=key)
    session=controller.call('/v1/session',{'peer':'worker','contract':contract})
    try:yield dict(bases=bases,contract=contract,key=key,controller=controller,session=session,claims=claims,
                   result_private=result_private,custody=custody,payload=payload,outputs=outputs,row=row,policy=policy)
    finally:
        for server in servers:server.shutdown();server.server_close()
        for thread in threads:thread.join()


def test_real_mtls_header_release_and_encrypted_ram_job(encrypted_source,monkeypatch):
    # The shared test host has swap enabled. Simulate only this kernel policy
    # observation for synthetic bytes; never weaken the production RAM guard.
    read_text=Path.read_text
    def synthetic_no_swap(path,*args,**kwargs):
        return 'Filename Type Size Used Priority\n' if str(path)=='/proc/swaps' else read_text(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',synthetic_no_swap)
    state=encrypted_source;contract=state['contract']
    spec=prepare(contract,{'host':'source','machine':hashlib.sha256(b'source').hexdigest()},state['session']['lease'],state['session']['inputs'])
    job={'request':spec,'worker_machine':contract['machine'],'contract_sha256':'a'*64,'recipe_fingerprint':'b'*64}
    with tempfile.TemporaryDirectory(dir='/dev/shm') as ram:
        result=worker.execute(job,tls=state['bases']['worker'],work=ram)
        assert list(Path(ram).iterdir())==[]
    assert result['job_key']==state['key']
    encrypted=state['outputs']/state['key']/'summary.txt.c4gh'
    assert encrypted.exists() and encrypted.read_bytes().startswith(b'crypt4gh')
    assert b'user-token' not in json.dumps(job).encode()
    assert state['custody'].hex() not in json.dumps(job)
    recovered=io.BytesIO()
    with encrypted.open('rb') as incoming:crypto.decrypt(incoming,recovered,state['result_private'])
    assert recovered.getvalue()==str(len(state['payload'])).encode()
    with encrypted.open('rb') as incoming:
        with pytest.raises(ValueError):crypto.decrypt(incoming,io.BytesIO(),os.urandom(32))


def test_release_denies_wrong_worker_revocation_and_changed_contract(encrypted_source):
    state=encrypted_source
    args={'lease':state['session']['lease'],'phase':'inputs','input':'reads','recipient':crypto.public_key(os.urandom(32)).hex()}
    with pytest.raises(RuntimeError,match='403'):SourceClient('source',state['bases']['other']).call('/v1/release',args)
    changed=copy.deepcopy(state['contract']);changed['argv']=['python3','-c','print("unauthorized")']
    with pytest.raises(RuntimeError,match='403'):state['controller'].call('/v1/session',{'peer':'worker','contract':changed})
    state['claims']['active']=False
    with pytest.raises(RuntimeError,match='403'):SourceClient('source',state['bases']['worker']).call('/v1/release',args)
    with pytest.raises(RuntimeError,match='403'):state['controller'].call('/v1/offers',{'peer':'worker','inputs':{'reads':state['row']}})


def test_crypto_rejects_tampering_and_worker_requires_ram(tmp_path,monkeypatch):
    private=os.urandom(32);encrypted=io.BytesIO();crypto.encrypt(io.BytesIO(b'ACGT'*20000),encrypted,crypto.public_key(private))
    damaged=bytearray(encrypted.getvalue());damaged[-1]^=1
    with pytest.raises(ValueError):crypto.decrypt(io.BytesIO(damaged),io.BytesIO(),private)
    read_text=Path.read_text
    def disk_mount(path,*args,**kwargs):
        if str(path)=='/proc/self/mountinfo':return '1 0 0:0 / / rw - ext4 /dev/synthetic rw\n'
        return read_text(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',disk_mount)
    with pytest.raises(ValueError,match='tmpfs'):worker.require_ram(tmp_path)
    with pytest.raises(ValueError):crypto.read_header(io.BytesIO(b'crypt4gh'+b'\1\0\0\0'+b'\xff'*4))


def test_approval_contract_and_ram_budget_are_immutable(encrypted_source):
    state=encrypted_source;changed=copy.deepcopy(state['contract']);changed['scratch_mb']=1
    with pytest.raises(ValueError,match='scratch'):job_key(changed)
    approvals=Path(state['policy']['authorization']['approvals_file']);approvals.write_text('{}')
    args={'lease':state['session']['lease'],'phase':'outputs'}
    with pytest.raises(RuntimeError,match='403'):SourceClient('source',state['bases']['worker']).call('/v1/release',args)


@pytest.mark.skipif(os.getenv('PROTECTED_DOCKER_TEST')!='1',reason='Explicit local Docker integration check')
def test_real_docker_protected_worker(encrypted_source,tmp_path):
    state=encrypted_source;contract=copy.deepcopy(state['contract'])
    image=subprocess.check_output(['docker','image','inspect','lifetech/genomics-protected:1','--format','{{.Id}}'],text=True).strip()
    contract['image']=image
    approvals=Path(state['policy']['authorization']['approvals_file'])
    key=approve(contract,'owner:alice',time.time()+300,approvals)
    controller=SourceClient('source',state['bases']['controller'],token_provider=lambda:'user-token',job_key=key)
    session=controller.call('/v1/session',{'peer':'worker','contract':contract})
    spec=prepare(contract,{'host':'source','machine':hashlib.sha256(b'source').hexdigest()},session['lease'],session['inputs'])
    from scripts.launcher_streams import validate
    validate(spec,{'genomics.protected.v1':{'image':image,'argv':['python3','/app/protected_worker.py','/work/job.json'],'version':'crypt4gh-1.8.6'}})
    job={'request':spec,'worker_machine':contract['machine'],'contract_sha256':'a'*64,'recipe_fingerprint':'b'*64}
    job_file=tmp_path/'job.json';job_file.write_text(json.dumps(job))
    # Synthetic test only: never disable the host's actual swap policy.
    swaps=tmp_path/'synthetic-no-swap';swaps.write_text('Filename Type Size Used Priority\n')
    command=['docker','run','--rm','--network','host','--read-only','--log-driver','none',
             '--user',f'{os.getuid()}:{os.getgid()}','--cap-drop','ALL','--security-opt','no-new-privileges',
             '--memory','128m','--memory-swap','128m','--pids-limit','32','--ulimit','core=0:0',
             '--tmpfs','/work:rw,nosuid,nodev,noexec,size=16m,mode=1777',
             '--mount','type=bind,src='+str(state['bases']['worker'])+',dst=/tls,readonly',
             '--mount','type=bind,src='+str(job_file)+',dst=/job.json,readonly',
             '--mount','type=bind,src='+str(swaps)+',dst=/proc/swaps,readonly',
             '--entrypoint','python3',image,'-c',
             'import sys,json;sys.path.insert(0,"/app");import protected_worker; print(json.dumps(protected_worker.execute(json.load(open("/job.json")))))']
    completed=subprocess.run(command,check=True,capture_output=True,text=True,timeout=60)
    result=json.loads(completed.stdout)
    assert result['job_key']==key
    recovered=io.BytesIO()
    with (state['outputs']/key/'summary.txt.c4gh').open('rb') as incoming:
        crypto.decrypt(incoming,recovered,state['result_private'])
    assert recovered.getvalue()==str(len(state['payload'])).encode()


def test_worker_rejects_analysis_substitution_before_decryption(encrypted_source,monkeypatch):
    state=encrypted_source;contract=copy.deepcopy(state['contract'])
    contract['argv']=['python3','-c','raise Exception("must never execute")']
    spec=prepare(contract,{'host':'source','machine':hashlib.sha256(b'source').hexdigest()},state['session']['lease'],state['session']['inputs'])
    job={'request':spec,'worker_machine':contract['machine'],'contract_sha256':'a'*64,'recipe_fingerprint':'b'*64}
    monkeypatch.setattr(worker,'require_ram',lambda path:None)
    with tempfile.TemporaryDirectory(dir='/dev/shm') as ram:
        with pytest.raises(ValueError,match='approved analysis'):worker.execute(job,tls=state['bases']['worker'],work=ram)
        assert list(Path(ram).iterdir())==[]


def test_cli_prepare_approve_revoke_and_recipe_registration(tmp_path):
    import sys
    def cli(*arguments):
        return subprocess.run([sys.executable,'-m','scripts.protected_genomics_cli',*map(str,arguments)],
                              check=True,capture_output=True,text=True)
    private=tmp_path/'custody.key';public=tmp_path/'custody.pub'
    cli('keygen','--private',private,'--public',public)
    assert private.stat().st_mode&0o777==0o600
    original=tmp_path/'synthetic.fastq';original.write_bytes(b'@synthetic\nACGT\n+\nIIII\n')
    encrypted=tmp_path/'synthetic.c4gh';catalogue=tmp_path/'catalogue.json'
    cli('ingest','--input',original,'--output',encrypted,'--recipient',public,
        '--custody-key',private,'--catalogue',catalogue,'--dataset','synthetic')
    analysis=tmp_path/'argv.json';analysis.write_text(json.dumps(['python3','-c','pass']))
    draft=tmp_path/'job.json'
    cli('prepare','--catalogue',catalogue,'--analysis',analysis,'--result-public',public,
        '--output',draft,'--peer','worker','--machine','a'*64,'--image','sha256:'+'b'*64,
        '--input','reads=synthetic','--result','result.json=1024')
    contract=json.loads(draft.read_text());key=job_key(contract)
    approvals=tmp_path/'approvals.json'
    cli('approve','--contract',draft,'--approvals',approvals,'--subject','owner:reviewed-person')
    assert json.loads(approvals.read_text())[key]['subject']=='owner:reviewed-person'
    cli('revoke','--job-key',key,'--approvals',approvals)
    assert json.loads(approvals.read_text())=={}
    # Existing encrypted data is never overwritten by a duplicate ingestion.
    before=encrypted.read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        cli('ingest','--input',original,'--output',encrypted,'--recipient',public,
            '--custody-key',private,'--catalogue',catalogue,'--dataset','synthetic')
    assert encrypted.read_bytes()==before
    if os.getenv('PROTECTED_DOCKER_TEST')=='1':
        registry=tmp_path/'stream-recipes.json'
        cli('install-recipe','--image','lifetech/genomics-protected:1','--registry',registry)
        from scripts.launcher_streams import recipes
        assert 'genomics.protected.v1' in recipes(tmp_path/'node-config.json')
