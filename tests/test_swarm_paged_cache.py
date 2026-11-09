import copy
import json
from pathlib import Path
import pytest
from scripts import swarm_paged_cache as pager


def plan():
    image='sha256:'+'a'*64
    files={'index':{'path':'/public/immutable/reference/index','bytes':10,'sha256':'b'*64}}
    p={'schema':'jswarm-paged-cache.v1','visibility':'public','key':'','source':'source','runtime':pager.runtime_revision(),'image':image,'files':files,'export':'/public/immutable/reference','port':21123,'address':'10.42.0.1','clients':{'source':'10.42.0.1','target':'10.42.0.2'}}
    p['key']=pager.plan_key(p);return p


def test_explicit_public_immutable_plan_and_read_only_allowlist():
    p=plan();assert pager.validate(p)==p
    config=pager.server_config(p)
    assert 'Access_Type = None' in config and 'Access_Type = RO' in config
    assert 'All_Squash' in config and 'Clients = 10.42.0.1,10.42.0.2;' in config
    for change in ({'visibility':'private'},{'key':'changed'},{'port':True},{'clients':{'bad':'*; Access_Type=RW'}}):
        with pytest.raises(ValueError):pager.validate({**p,**change})
    changed=copy.deepcopy(p);changed['files']['index']['path']='/private/input'
    with pytest.raises(ValueError):pager.validate(changed)


def test_every_selected_machine_is_provisioned_after_source(tmp_path,monkeypatch):
    cfg=tmp_path/'peers.json';cfg.write_text(json.dumps({'name':'source','peers':{'target':'10.42.0.2','source':'10.42.0.1','rootless':'10.42.0.3'}}))
    monkeypatch.setattr(pager,'CONFIG',cfg);calls=[]
    monkeypatch.setattr(pager,'PLAN',tmp_path/'deployment.json')
    monkeypatch.setattr(pager,'discover_aliases',lambda p:p.get('aliases',{}))
    def deploy(host,p):
        if host['host']!='source':assert calls[0]=='source'
        calls.append(host['host']);return {'ready':True}
    monkeypatch.setattr(pager,'deploy_node',deploy)
    rows=pager.deploy_fleet(plan())
    assert set(rows)=={'source','target','rootless'} and all(r['ready'] for r in rows.values())
    assert calls[0]=='source'


def test_unavailable_pager_preserves_verified_fallback_for_node(tmp_path,monkeypatch):
    cfg=tmp_path/'peers.json';cfg.write_text(json.dumps({'name':'source','peers':{'source':'10.42.0.1','target':'10.42.0.2'}}))
    monkeypatch.setattr(pager,'CONFIG',cfg)
    monkeypatch.setattr(pager,'PLAN',tmp_path/'deployment.json')
    monkeypatch.setattr(pager,'discover_aliases',lambda p:p.get('aliases',{}))
    def deploy(host,p):
        if host['host']=='target':raise RuntimeError('Mount unavailable')
        return {'ready':True}
    monkeypatch.setattr(pager,'deploy_node',deploy)
    rows=pager.deploy_fleet(plan())
    assert rows['source']['ready'] and not rows['target']['ready']
    assert rows['target']['fallback']=='verified-complete-cache'


@pytest.mark.parametrize('engine',[{'available':False,'rootless':False},{'available':True,'rootless':True}])
def test_pager_checks_kernel_mount_prerequisite_before_image_transfer(monkeypatch,engine):
    from types import SimpleNamespace
    class Client:
        def __init__(self,host):pass
        def call(self,*args,**kwargs):return {'machine':'a'*64}
        def close(self):pass
    monkeypatch.setattr(pager,'AgentClient',Client)
    monkeypatch.setattr(pager,'host_command',lambda host,code:['node-api-probe'])
    monkeypatch.setattr(pager.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=json.dumps(engine)))
    def unexpected_transfer(*args):raise AssertionError('Image transfer started without kernel mount support')
    monkeypatch.setattr(pager,'ensure_image',unexpected_transfer)
    with pytest.raises(RuntimeError,match='verified RAM-cache fallback'):
        pager.deploy_node({'host':'target'},plan())


@pytest.mark.parametrize('info,expected',[
    (None,{'available':False,'rootless':False}),
    ({'SecurityOptions':None}, {'available':False,'rootless':False}),
    ({'ID':'host','ServerVersion':'29','SecurityOptions':None}, {'available':True,'rootless':False}),
    ({'ID':'host','ServerVersion':'29','SecurityOptions':['name=rootless']}, {'available':True,'rootless':True}),
])
def test_system_docker_probe_requires_server_identity_and_handles_null_security(monkeypatch,capsys,info,expected):
    from types import SimpleNamespace
    monkeypatch.setattr(pager.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=json.dumps(info)))
    exec(pager.SYSTEM_DOCKER_PROBE,{})
    assert json.loads(capsys.readouterr().out)==expected


def test_system_docker_probe_treats_timeout_as_unavailable(monkeypatch,capsys):
    def timeout(*args,**kwargs):raise pager.subprocess.TimeoutExpired('docker info',20)
    monkeypatch.setattr(pager.subprocess,'run',timeout)
    exec(pager.SYSTEM_DOCKER_PROBE,{})
    assert json.loads(capsys.readouterr().out)=={'available':False,'rootless':False}



def test_source_guard_refuses_changed_source_before_starting_export(tmp_path,monkeypatch):
    import subprocess
    import sys
    from unittest.mock import Mock
    export=tmp_path/'export';export.mkdir();blob=export/'index';blob.write_bytes(b'public reference')
    st=blob.stat();identity=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
    code=pager.GUARD.replace("Path('/export')",f'Path({str(export)!r})').replace("Path('/run/ganesha')",f'Path({str(tmp_path/"run")!r})').replace("Path('/var/lib/nfs/ganesha')",f'Path({str(tmp_path/"nfs")!r})').replace("Path('/run/rpcbind')",f'Path({str(tmp_path/"rpcbind")!r})')
    process=Mock(returncode=0);process.poll.return_value=0;start=Mock(return_value=process)
    monkeypatch.setattr(subprocess,'Popen',start)
    monkeypatch.setattr(subprocess,'run',Mock())
    monkeypatch.setattr(sys,'argv',['guard',json.dumps({'index':identity})])
    with pytest.raises(SystemExit) as ended:exec(compile(code,'source-guard','exec'),{})
    assert ended.value.code==0 and start.call_count==1
    start.reset_mock();blob.write_bytes(b'changed reference')
    with pytest.raises(ValueError,match='identity changed'):exec(compile(code,'source-guard','exec'),{})
    start.assert_not_called()


def test_docker_mmap_checks_pages_readonly_and_rejects_bad_checksum(tmp_path):
    import hashlib
    import os
    import subprocess
    result=subprocess.run(['docker','image','inspect','lifetech/fscache-test:bookworm','--format','{{.Id}}'],capture_output=True,text=True,timeout=20)
    if result.returncode:pytest.skip('Local approved helper image is not installed')
    image=result.stdout.strip();data=b'public reference fixture\n'*10000;blob=tmp_path/'index';blob.write_bytes(data)
    probes=[[0,4096,hashlib.sha256(data[:4096]).hexdigest()],[len(data)-4096,4096,hashlib.sha256(data[-4096:]).hexdigest()]]
    argv=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user',str(os.getuid())+':'+str(os.getgid()),'--memory','128m','--memory-swap','128m','--cpus','0.25','--mount','type=bind,src='+str(blob)+',dst=/reference/index,readonly',image,'python3','-c',pager.PROBE]
    good=subprocess.run([*argv,json.dumps({'index':probes})],capture_output=True,text=True,timeout=180)
    assert good.returncode==0,good.stderr
    assert json.loads(good.stdout)=={'verified_bytes':8192,'read_only':True,'mmap':True}
    probes[1][2]='0'*64
    bad=subprocess.run([*argv,json.dumps({'index':probes})],capture_output=True,text=True,timeout=180)
    assert bad.returncode!=0 and 'checksum mismatch' in bad.stderr


def test_public_source_is_verified_through_host_bind_not_owner_namespace(tmp_path):
    import hashlib,os,subprocess
    result=subprocess.run(['docker','image','inspect','python:3.12-bookworm','--format','{{.Id}}'],capture_output=True,text=True,timeout=20)
    if result.returncode:pytest.skip('Local Python fixture image unavailable')
    root=tmp_path/'export';root.mkdir(mode=0o755);root.chmod(0o755);blob=root/'index'
    payload=b'public reference fixture\n'*512;blob.write_bytes(payload);blob.chmod(0o444)
    files={'index':{'path':'/host-only/not-mounted/index','bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}}
    argv=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--user',str(os.getuid())+':'+str(os.getgid()),
          '--memory','128m','--memory-swap','128m','--mount','type=bind,src='+str(root)+',dst=/export,readonly',
          result.stdout.strip(),'python3','-c',pager.PUBLIC_FILES,json.dumps(files)]
    proof=subprocess.run([*argv,'verify','{}'],capture_output=True,text=True,timeout=30)
    assert proof.returncode==0,proof.stderr
    identities=json.loads(proof.stdout);assert identities['index'][2]==len(payload)
    pages=subprocess.run([*argv,'pages','{}'],capture_output=True,text=True,timeout=30)
    assert pages.returncode==0,pages.stderr
    assert json.loads(pages.stdout)['index'][0]==[0,4096,hashlib.sha256(payload[:4096]).hexdigest()]
    blob.chmod(0o644);blob.write_bytes(b'changed'.ljust(len(payload),b'.'))
    changed=subprocess.run([*argv,'verify',proof.stdout],capture_output=True,text=True,timeout=30)
    assert changed.returncode!=0 and 'checksum mismatch' in changed.stderr


def test_cache_warm_is_an_immutable_owner_installed_readonly_node_recipe():
    from scripts.launcher_streams import validate
    p=plan();record={'image':p['image'],'paths':{'index':'/mounted/reference/index'}}
    definition=pager.warm_definition(record,p)
    assert definition['mounts']==[{'source':'/mounted/reference/index','target':'/reference/index','readonly':True}]
    assert len(definition['version'])==64
    spec={'id':'cache-warm','recipe':'warm','image':p['image'],'inputs':{},'resources':{'cpus':1,'memory_mb':6000,'memory_reservation_mb':128}}
    assert validate(spec,{'warm':definition})['inputs']=={}


def test_optional_prefetch_reuses_node_verified_ram_without_gpu_probe(monkeypatch):
    p=plan();calls=[]
    class Client:
        def __init__(self,host):pass
        def call(self,op,**kwargs):
            calls.append(op)
            assert op=='resource.caches'
            return {'caches':[{'key':'native-cache','files':{'index':{'bytes':10,'sha256':'b'*64}}}]}
        def close(self):pass
    monkeypatch.setattr(pager,'AgentClient',Client)
    status={'capabilities':['node-maintenance-recipes-v1','verified-ram-caches-v1']}
    result=pager.warm_node({'host':'target'},{},p,status)
    assert result['state']=='skipped' and result['cache']=='native-cache'
    assert calls==['resource.caches']


def test_optional_prefetch_uses_spare_ram_instead_of_machine_total(monkeypatch):
    p=plan()
    class Client:
        def __init__(self,host):pass
        def call(self,op,**kwargs):
            if op=='resource.caches':return {'caches':[]}
            assert op=='resource.capacity'
            return {'memory_mb':16000,'available_mb':100}
        def close(self):pass
    monkeypatch.setattr(pager,'AgentClient',Client)
    status={'capabilities':['node-maintenance-recipes-v1','verified-ram-caches-v1']}
    result=pager.warm_node({'host':'target'},{},p,status)
    assert result['state']=='skipped' and 'spare RAM' in result['reason']


def test_optional_prefetch_never_competes_with_native_cache_fill(monkeypatch):
    class Client:
        def __init__(self,host):pass
        def call(self,op,**kwargs):
            assert op=='resource.caches'
            return {'caches':[],'pending_caches':['filling-cache']}
        def close(self):pass
    monkeypatch.setattr(pager,'AgentClient',Client)
    status={'capabilities':['node-maintenance-recipes-v1','verified-ram-caches-v1']}
    result=pager.warm_node({'host':'target'},{},plan(),status)
    assert result['state']=='skipped' and 'fill takes priority' in result['reason']


def test_opt_in_ssd_cache_verifies_bytes_and_reuses_same_inode(tmp_path,monkeypatch):
    import hashlib,os,subprocess
    home=tmp_path/'owner';(home/'.config/lifetech-launcher').mkdir(parents=True)
    (home/'.config/lifetech-launcher/public-cache-policy.json').write_text(json.dumps({'storage':'verified-local-ssd'}))
    monkeypatch.setattr(Path,'home',lambda:home)
    source=tmp_path/'source';source.write_bytes(b'verified public reference'*100)
    popen=subprocess.Popen
    def reader(argv,**kw):
        assert argv==['docker','exec','fixture','cat','/state/reference/index']
        return popen(['cat',str(source)],**kw)
    monkeypatch.setattr(subprocess,'Popen',reader)
    def host_metadata(*args):
        assert args[:8]==('run','--rm','--network','none','--pid','host','--privileged','image')
        assert 'nsenter -t 1 -m' in args[10]
        return '0\n'
    root=home/'cache';root.mkdir()
    p={'node':'node'};pl={'source':'other','files':{'index':{'bytes':source.stat().st_size,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}}
    code=pager.BOOTSTRAP.split("mode='local-mmap'",1)[1].split("record={'schema'",1)[0]
    code="mode='local-mmap'"+code
    def run():
        values={'p':p,'plan':pl,'paths':{'index':'/owner-cannot-see-this'},'root':root,'pathlib':__import__('pathlib'),'json':json,'subprocess':subprocess,'hashlib':hashlib,'os':os,'run':host_metadata,'docker':['docker'],'name':'fixture','image':'image'}
        exec(code,values);return Path(values['paths']['index'])
    target=run();inode=target.stat().st_ino
    assert target.read_bytes()==source.read_bytes()
    assert run().stat().st_ino==inode
    target.chmod(0o600);target.write_bytes(b'changed')
    with pytest.raises(ValueError,match='identity differs'):run()


def test_pager_transport_does_not_have_fixed_cpu_quota():
    common=pager.BOOTSTRAP.split('common=',1)[1].split('\n',1)[0]
    assert "'--cpus'" not in common
