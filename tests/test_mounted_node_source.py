"""Mounted Git activation, rollback and controller-only source delivery."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from scripts import node_git_source as source
from scripts import launcher_peer_supervisor as supervisor
from scripts.node_source_delivery import bundle

NAMES=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
PROJECT=Path(__file__).resolve().parents[1]


def repositories(tmp_path):
    root=tmp_path/'Organization'
    for name in ('Organization','JSwarm','HarborGenetics'):
        path=root if name=='Organization' else root/name
        path.mkdir(parents=True,exist_ok=True)
        subprocess.run(['git','init','-q',str(path)],check=True)
        source.git(path,'config','user.name','Test');source.git(path,'config','user.email','test@localhost')
        source.git(path,'remote','add','origin','https://offline.invalid/'+name+'.git')
        (path/'README').write_text(name)
        if name=='Organization':(path/'.gitignore').write_text('/JSwarm/\n/HarborGenetics/\n')
        if name=='JSwarm':
            scripts=path/'scripts';scripts.mkdir()
            for filename in (*NAMES,'launcher_peer_supervisor.py','node_git_source.py','node_source_install.py','launcher_update.py'):
                (scripts/filename).write_text((PROJECT/'scripts'/filename).read_text())
        source.git(path,'add','.');source.git(path,'commit','-qm','source')
    return root


def delivered(root,config):
    manifest=source.snapshot(root)
    base=config.parent/'node-source';base.mkdir(parents=True,exist_ok=True)
    for name,spec in manifest.items():
        repository=root if name=='Organization' else root/name
        receipt=bundle(repository,spec['commit'],root.parent/(name+'-'+spec['commit']+'.bundle'))
        source.import_bundle(base,name,spec['commit'],receipt['path'],receipt['sha256'])
    files={name:(root/'JSwarm/scripts'/name).read_text() for name in NAMES}
    from agent_updates import source_revision
    revision=source_revision(files,{'mode':'mounted-v1','repositories':manifest})
    return manifest,revision


def config_at(tmp_path):
    config=tmp_path/'config/peers.json';config.parent.mkdir()
    config.write_text(json.dumps({'name':'worker.local','controller_public_key':'ab'*32}))
    return config


def test_delivered_git_activates_offline_without_generating_runtime(tmp_path,monkeypatch):
    root=repositories(tmp_path);config=config_at(tmp_path)
    manifest,revision=delivered(root,config)
    workspace=source.stage_source(manifest,config,revision)
    release=(config.parent/'agent-releases/current').resolve()
    assert {p.name for p in release.iterdir()}=={'source.json'}
    assert supervisor.verify_release(release,release.parent)==release
    spawn=Mock();real_popen=subprocess.Popen
    monkeypatch.setattr(supervisor.subprocess,'Popen',lambda argv,**kwargs:
        real_popen(argv,**kwargs) if argv[0]=='git' else spawn(argv,**kwargs))
    supervisor.launch_source(release,release.parent,'python3','lifetech-')
    argv=spawn.call_args.args[0];env=spawn.call_args.kwargs['env']
    assert Path(argv[2])==workspace/'JSwarm/scripts/launcher_peers.py'
    assert env['JSWARM_SOURCE_REVISION']==revision
    assert env['SWARM_WEB_WORKSPACE']==str(workspace)
    assert env['PYTHONPATH'].split(os.pathsep)[0]==str(workspace/'JSwarm')
    assert not any((release/name).exists() for name in NAMES)
    assert (root/'JSwarm/scripts/launcher_peers.py').read_bytes()==Path(argv[2]).read_bytes()


def test_update_retains_previous_checkout_and_rejects_dirty_source(tmp_path):
    root=repositories(tmp_path);config=config_at(tmp_path)
    manifest,revision=delivered(root,config)
    first=source.stage_source(manifest,config,revision)
    module=root/'JSwarm/scripts/launcher_cache.py';module.write_text('# changed supporting module\n')
    source.git(root/'JSwarm','add','.');source.git(root/'JSwarm','commit','-qm','supporting code')
    newer,new_revision=delivered(root,config)
    assert new_revision!=revision  # Supporting modules are part of Git identity.
    second=source.stage_source(newer,config,new_revision)
    releases=config.parent/'agent-releases'
    assert (releases/'previous').resolve().name==revision
    assert first!=second and first.exists()
    (second/'JSwarm/scripts/launcher_agent.py').write_text('# modified\n')
    with pytest.raises(ValueError,match='checkout changed'):
        supervisor.verify_release((releases/'current').resolve(),releases)
    source.stage_source(manifest,config,revision)
    assert (releases/'current').resolve().name==revision


def test_missing_or_corrupt_delivery_never_changes_active_pointer(tmp_path):
    root=repositories(tmp_path);config=config_at(tmp_path)
    manifest,revision=delivered(root,config)
    source.stage_source(manifest,config,revision)
    missing=json.loads(json.dumps(manifest));missing['JSwarm']['commit']='a'*40
    with pytest.raises(RuntimeError,match='not been delivered'):
        source.stage_source(missing,config,'b'*64)
    assert (config.parent/'agent-releases/current').resolve().name==revision
    path=tmp_path/'corrupt.bundle';path.write_bytes(b'bad')
    with pytest.raises(ValueError,match='checksum'):
        source.import_bundle(config.parent/'node-source','JSwarm',manifest['JSwarm']['commit'],path,'0'*64)


def test_bundle_import_supports_python310_command_owners(tmp_path,monkeypatch):
    root=repositories(tmp_path);commit=source.git(root/'JSwarm','rev-parse','HEAD')
    receipt=bundle(root/'JSwarm',commit,tmp_path/'source.bundle')
    # Older host command services have Python 3.10 even when the node image
    # uses Python 3.12. file_digest was only added in Python 3.11.
    monkeypatch.delattr(hashlib,'file_digest')
    cache=tmp_path/'node-source';cache.mkdir()
    source.import_bundle(cache,'JSwarm',commit,receipt['path'],receipt['sha256'])
    assert source.git(cache/'JSwarm.git','rev-parse','refs/jswarm-source/'+commit)==commit


def test_interrupted_checkout_can_be_retried_without_repair(tmp_path,monkeypatch):
    root=repositories(tmp_path);config=config_at(tmp_path)
    manifest,revision=delivered(root,config)
    real_git=source.git;interrupted=False
    def git(folder,*args):
        nonlocal interrupted
        if 'checkout' in args and not interrupted:
            interrupted=True
            raise OSError('Interrupted checkout')
        return real_git(folder,*args)
    monkeypatch.setattr(source,'git',git)
    with pytest.raises(OSError,match='Interrupted'):
        source.stage_source(manifest,config,revision)
    assert not (config.parent/'agent-releases/current').exists()
    source.stage_source(manifest,config,revision)
    release=(config.parent/'agent-releases/current').resolve()
    assert supervisor.verify_release(release,release.parent)==release


def test_source_descriptor_cannot_escape_configuration_volume(tmp_path):
    root=repositories(tmp_path);config=config_at(tmp_path)
    manifest,revision=delivered(root,config)
    workspace=source.stage_source(manifest,config,revision)
    outside=tmp_path/'outside';workspace.rename(outside);workspace.symlink_to(outside)
    with pytest.raises(ValueError,match='escapes'):
        supervisor.verify_release((config.parent/'agent-releases/current').resolve(),config.parent/'agent-releases')


def test_checkouts_survive_bind_path_changes_and_cache_garbage_collection(tmp_path):
    root=repositories(tmp_path);config=config_at(tmp_path)
    manifest,revision=delivered(root,config)
    source.stage_source(manifest,config,revision)
    for name in manifest:source.git(config.parent/'node-source'/(name+'.git'),'gc','--prune=now')
    relocated=tmp_path/'container-config';config.parent.rename(relocated)
    releases=relocated/'agent-releases';release=(releases/'current').resolve()
    assert supervisor.verify_release(release,releases)==release
    workspace=supervisor.source_workspace(release,releases)
    assert (workspace/'JSwarm/.git').is_dir()
    for name in manifest:
        repository=workspace if name=='Organization' else workspace/name
        assert source.git(repository,'rev-parse','HEAD')==manifest[name]['commit']


@pytest.mark.parametrize('modern',[False,True])
def test_installer_migrates_only_node_and_preserves_enrollment(tmp_path,monkeypatch,modern):
    from scripts import node_source_install as installer
    root=repositories(tmp_path);config=config_at(tmp_path)
    manifest,revision=delivered(root,config)
    scripts=root/'JSwarm/scripts'
    bootstrap=tmp_path/'bootstrap.py';bootstrap.write_text('# previous bootstrap\n')
    payload={'node':'worker.local','machine':hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest(),
             'controller_public_key':'ab'*32,'revision':revision,
             'supervisor_source':(scripts/'launcher_peer_supervisor.py').read_text(),
             'files':{name:(scripts/name).read_text() for name in (*NAMES,'launcher_update.py')},
             'supervisor_supports_source':modern,
             'git_source':{'mode':'mounted-v1','repositories':manifest,
                           'installer':(scripts/'node_git_source.py').read_text(),
                           'activation':(scripts/'node_source_install.py').read_text()}}
    payload['supervisor_sha256']=hashlib.sha256(payload['supervisor_source'].encode()).hexdigest()
    monkeypatch.setenv('LIFETECH_PEER_CONFIG',str(config))
    monkeypatch.setattr(installer,'owned_node',lambda c:(['docker'],'node-container-id',bootstrap))
    original=subprocess.run;calls=[]
    def run(argv,**kwargs):
        if argv[0]=='docker':calls.append(argv);return Mock(returncode=0)
        return original(argv,**kwargs)
    monkeypatch.setattr(installer.subprocess,'run',run)
    settings=config.read_bytes()
    installer.install(payload)
    assert calls==([] if modern else [['docker','restart','node-container-id']])
    assert config.read_bytes()==settings
    assert (config.parent/'agent-releases/current').resolve().name==revision
    assert bootstrap.read_text()==payload['supervisor_source']
    assert not any('build' in call for call in calls)


def test_owned_node_requires_actual_bind_mounted_supervisor(tmp_path,monkeypatch):
    from scripts import node_source_install as installer
    config=config_at(tmp_path);bootstrap=tmp_path/'checkout/scripts/launcher_peer_supervisor.py'
    bootstrap.parent.mkdir(parents=True);bootstrap.write_text('# bootstrap')
    container={'Id':'owned','Mounts':[
        {'Type':'bind','Source':str(config.parent),'Destination':'/run/launcher-config','RW':True},
        {'Type':'bind','Source':str(tmp_path/'checkout'),'Destination':'/workspace','RW':True}]}
    def run(argv,**kwargs):
        return Mock(returncode=0,stdout=json.dumps([container] if 'inspect' in argv else ['/workspace/scripts/launcher_peer_supervisor.py']))
    monkeypatch.setattr(installer.subprocess,'run',run)
    assert installer.owned_node(config)==(['docker'],'owned',bootstrap)
    container['Mounts'][1]['RW']=False
    with pytest.raises(RuntimeError,match='writable host source mount'):installer.owned_node(config)
    container['Mounts'][0]['Source']=str(tmp_path/'different-owner')
    with pytest.raises(RuntimeError,match='No node container'):installer.owned_node(config)


def test_publication_uses_raw_source_and_binds_all_repository_commits(tmp_path,monkeypatch):
    from agent_updates import LauncherUpdates
    root=repositories(tmp_path);manifest=source.snapshot(root)
    (root/'JSwarm/.node-git-source.json').write_text(json.dumps(manifest))
    monkeypatch.setattr('deployment_components.snapshot',lambda root:{})
    release=LauncherUpdates(root,lambda:False,Mock())
    assert release.git_source['mode']=='mounted-v1'
    assert release.files['launcher_agent.py']==(root/'JSwarm/scripts/launcher_agent.py').read_text()
    path=release.publish(tmp_path/'publications')
    loaded=LauncherUpdates.published(path,lambda:False,Mock())
    assert loaded.revision==release.revision and loaded.git_source==release.git_source
    # A helper-only change must still produce a new running revision.
    manifest['JSwarm']['commit']='b'*40
    (root/'JSwarm/.node-git-source.json').write_text(json.dumps(manifest))
    assert LauncherUpdates(root,lambda:False,Mock()).revision!=release.revision


def test_direct_source_process_observes_selected_checkout(tmp_path):
    root=repositories(tmp_path);config=config_at(tmp_path)
    output=tmp_path/'observed.json'
    script=root/'JSwarm/scripts/launcher_peers.py'
    script.write_text('import json,os,pathlib\npathlib.Path('+repr(str(output))+').write_text(json.dumps({"file":__file__,"cwd":os.getcwd(),"revision":os.environ["JSWARM_SOURCE_REVISION"]}))\n')
    source.git(root/'JSwarm','add','.');source.git(root/'JSwarm','commit','-qm','executable probe')
    manifest,revision=delivered(root,config)
    workspace=source.stage_source(manifest,config,revision)
    release=(config.parent/'agent-releases/current').resolve()
    import sys
    process=supervisor.launch_source(release,release.parent,sys.executable,'lifetech-')
    assert process.wait(timeout=15)==0
    observed=json.loads(output.read_text())
    assert observed=={'file':str(workspace/'JSwarm/scripts/launcher_peers.py'),
                      'cwd':str(workspace),'revision':revision}


def test_mounted_release_web_assets_override_old_developer_workspace(tmp_path,monkeypatch):
    from scripts.launcher_peers import pipeline_web_root
    config=config_at(tmp_path)
    config.write_text(json.dumps({'web_workspace':'/old/developer/checkout'}))
    root=tmp_path/'selected';(root/'JSwarm/swarm-control/server/view').mkdir(parents=True)
    monkeypatch.setenv('JSWARM_SOURCE_REVISION','a'*64)
    monkeypatch.setenv('SWARM_WEB_WORKSPACE',str(root))
    assert pipeline_web_root(config)==root


def test_controller_delivery_prefers_encrypted_bulk_and_skips_cached_sources(tmp_path,monkeypatch):
    from scripts import node_source_delivery as delivery
    from agent_client import CONFIG
    root=repositories(tmp_path);manifest=source.snapshot(root)
    updater=Mock(git_source={'repositories':manifest,'installer':source.__doc__},stopped=lambda:False)
    updater.command.side_effect=lambda host,code,status:['owner-command',code]
    config=tmp_path/'controller.json';config.write_text('{"name":"controller.local"}')
    monkeypatch.setattr('agent_client.CONFIG',config)
    monkeypatch.setattr('scripts.launcher_discovery.resolve_endpoint',lambda name:('https://'+name+':8790',[]))
    transferred=[]
    def copy(src,dst,receipt,**kw):
        assert src['host']=='controller.local' and dst['host']=='worker.local'
        assert receipt['prefer_bulk'] and kw['resume']
        transferred.append(receipt)
        return {**receipt,'path':'/remote/source.bundle'}
    monkeypatch.setattr('scripts.swarm_runtime.copy_verified_file',copy)
    real_run=subprocess.run;commands=[]
    def run(argv,**kwargs):
        if argv[0]=='owner-command':
            commands.append(argv[1])
            return Mock(returncode=0,stdout=json.dumps({'base':'/remote/node-source','cached':{
                'Organization':True,'JSwarm':False,'HarborGenetics':True}}))
        return real_run(argv,**kwargs)
    monkeypatch.setattr(delivery.subprocess,'run',run)
    monkeypatch.setattr(delivery.Path,'home',lambda:tmp_path)
    delivery.deliver(updater,{'host':'worker.local'},{'machine':'a'*64},root=root)
    assert len(transferred)==1 and len(commands)==2
    assert "namespace['import_bundle']" in commands[-1]
    assert 'https://offline.invalid' not in commands[-1]


@pytest.mark.parametrize('healthy',[True,False])
def test_supervisor_activates_real_mounted_process_or_restores_previous(tmp_path,monkeypatch,healthy):
    """Real child processes, real Git and the actual supervisor readiness loop."""
    import time
    root=repositories(tmp_path);config=config_at(tmp_path)
    ready=tmp_path/'launcher-agent-ready.json'
    script=root/'JSwarm/scripts/launcher_peers.py'
    script.write_text('import json,os,pathlib,time\n'+(
        'pathlib.Path('+repr(str(ready))+').write_text(json.dumps({"folder":os.environ["JSWARM_RELEASE_DIR"],"time":time.time()}))\ntime.sleep(60)\n'
        if healthy else 'raise SystemExit(1)\n'))
    source.git(root/'JSwarm','add','.');source.git(root/'JSwarm','commit','-qm','readiness probe')
    manifest,revision=delivered(root,config)
    source.stage_source(manifest,config,revision)
    releases=config.parent/'agent-releases';current=releases/'current';current.unlink()
    bootstrap=tmp_path/'bootstrap';bootstrap.mkdir()
    (bootstrap/'launcher_peers.py').write_text('import time\ntime.sleep(60)\n')
    def path(value):
        p=Path(value)
        return tmp_path/p.name if str(p).startswith('/tmp/launcher-agent-') else p
    path.home=Path.home
    update=tmp_path/'launcher-agent-update.json'
    phases=[]
    class Stop:
        ticks=0
        complete=False
        def is_set(self):return self.complete or self.ticks>=60
        def set(self):self.complete=True
        def wait(self,seconds):
            self.ticks+=1
            state=json.loads(update.read_text());phases.append(state['phase'])
            if self.ticks==1:current.symlink_to(revision)
            self.complete=(self.ticks>2 and state['phase']=='ready' and
                           (state['active']==str(releases/revision) if healthy else bool(state['last_failure'])))
            time.sleep(.05)
    monkeypatch.setattr(supervisor,'__file__',str(bootstrap/'supervisor.py'))
    monkeypatch.setattr(supervisor,'Path',path)
    monkeypatch.setenv('LIFETECH_PEER_CONFIG',str(config))
    monkeypatch.setattr(supervisor.sys,'argv',['supervisor'])
    monkeypatch.setattr(supervisor.signal,'signal',lambda *args:None)
    monkeypatch.setattr(supervisor.threading,'Event',Stop)
    supervisor.main()
    state=json.loads(update.read_text())
    assert phases[:3]==['ready','updating','starting']
    assert state['phase']=='ready'
    assert state['active']==str(releases/revision if healthy else bootstrap)
    assert bool(state['last_failure']) is not healthy
    assert current.is_symlink() is healthy
