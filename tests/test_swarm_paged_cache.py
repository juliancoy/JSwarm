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
