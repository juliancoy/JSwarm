"""Roll input runtimes and launcher updates through the standard machine deployer.

Replace and verify one enrolled node at a time. Docker workers retain their identities.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'swarm-control/server')]
from agent_client import AgentClient,CONFIG
from agent_updates import LauncherUpdates,deployment_lock,publication_token,check_publication
from input_runtime_release import InputRuntimeRelease


RELEASES=Path.home()/'.local/state/swarm-control/node-releases'


def sweep(nodes=None,wait_timeout=3600,published=None):
    config=json.loads(CONFIG.read_text())
    updater=LauncherUpdates.published(published,lambda:False,lambda *args:None) if published else LauncherUpdates.desired()
    rows=[]
    selected=nodes or list(config['peers'])
    if any(name not in config['peers'] for name in selected):raise ValueError('Use an enrolled node name')
    pending=None
    for name,address in config['peers'].items():
        if name not in selected:continue
        if pending:
            row={'node':name,'status':'pending','reason':'Waiting for '+pending+' to become ready'}
            rows.append(row);print(json.dumps(row),flush=True);continue
        host={'host':name,'local':name==config['name'],'lan_address':address,'agent_endpoint':'http://'+address+':8790'}
        client=AgentClient(host);row={'node':name}
        try:
            try:status=client.call('status',timeout=4)
            except (OSError,TimeoutError):
                client.close()
                status=updater.owner_status(host)
            if status['node']!=name:raise ValueError('Agent identity mismatch')
            options={'wait_timeout':wait_timeout}
            status=updater.ensure(host,client,status,**options)
            if status.get('source_revision')!=updater.revision or not status.get('ready'):
                raise RuntimeError('Desired release is not ready: '+host.get('launcher_update',{}).get('reason',''))
            row.update(status='current',launcher_revision=status['source_revision'],ready=status['ready'])
        except Exception as error:
            row.update(status='pending',reason=str(error)[:180])
            pending=name
        finally:client.close()
        rows.append(row);print(json.dumps(row),flush=True)
    # Optional public-artifact paging is pinned in the fleet publication.
    components=getattr(updater,'components',None)
    if isinstance(components,dict) and 'public-pager' in components:
        from deployment_components import deploy as deploy_component
        paged=deploy_component(components['public-pager'],selected)
        for row in rows:
            if row['node'] in paged:row['paged_cache']=paged[row['node']]
    report={'updated':time.time(),'launcher_revision':updater.revision,'generation':updater.generation,'input_revision':None,'nodes':rows}
    folder=Path.home()/'.local/state/swarm-control';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=folder/'input-deployment.tmp';temp.write_text(json.dumps(report,indent=2));temp.chmod(0o600);temp.replace(folder/'input-deployment.json')
    return all(row['status']=='current' for row in rows)


def freeze_source(root,target):
    """Export committed adjacent repositories; exclude dirty and untracked data."""
    import subprocess,tarfile,tempfile
    root=Path(root).resolve();target=Path(target);target.mkdir(parents=True,exist_ok=True)
    if root.name=='JSwarm':root=root.parent
    if not all((root/name).is_dir() for name in ('JSwarm','GenomeHarbor')):
        raise RuntimeError('Expected the enrolled workspace source layout')
    from scripts.node_git_source import snapshot
    manifest=snapshot(root)
    with tempfile.TemporaryDirectory(prefix='swarm-git-archives-') as temporary:
        for name in ('Organization','JSwarm','GenomeHarbor'):
            project=root if name=='Organization' else root/name
            destination=target if name=='Organization' else target/name
            destination.mkdir(exist_ok=True)
            archive=Path(temporary)/(name+'.tar')
            subprocess.run(['git','-C',str(project),'archive','--format=tar','--output='+str(archive),manifest[name]['commit']],check=True)
            # Trusted local git archives preserve the versioned sibling aliases.
            with tarfile.open(archive) as bundle:bundle.extractall(destination,filter='fully_trusted')
    (target/'JSwarm/.node-git-source.json').write_text(json.dumps(manifest,sort_keys=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen-source',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--expected-publication',help=argparse.SUPPRESS)
    parser.add_argument('--watch',action='store_true')
    parser.add_argument('--continuous',action='store_true',help='Keep following validated publications after fleet convergence')
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--status',action='store_true',help='Verify selected nodes against the published release without changes')
    actions.add_argument('--rollback',action='store_true',help='Restore the previous verified fleet publication')
    actions.add_argument('--publish',action='store_true',help='Validate this source snapshot and publish it for the existing updater')
    parser.add_argument('--paged-cache-only',action='store_true',help='Provision the demand-paged artifact component without replacing launchers')
    parser.add_argument('--node',action='append',help='Existing enrolled node name; default is the fleet')
    parser.add_argument('--wait-timeout',type=float,default=3600,help='Seconds to wait for verified agent startup')
    args=parser.parse_args()
    if not 0<args.wait_timeout<float('inf'):parser.error('--wait-timeout must be positive and finite')
    os.umask(0o077)
    if args.status:
        release=LauncherUpdates.desired();config=json.loads(CONFIG.read_text());rows=[]
        selected=args.node or list(config['peers'])
        if any(name not in config['peers'] for name in selected):parser.error('Use enrolled node names')
        for name in selected:
            client=AgentClient({'host':name,'agent_endpoint':'http://'+config['peers'][name]+':8790'})
            try:
                value=client.call('status',timeout=10)
                rows.append({'node':name,'ready':value.get('ready') is True,
                             'current':value.get('node')==name and value.get('source_revision')==release.revision})
            except Exception as error:rows.append({'node':name,'ready':False,'current':False,'error':type(error).__name__})
            finally:client.close()
        print(json.dumps({'revision':release.revision,'generation':release.generation,'nodes':rows}),flush=True)
        if not all(row['ready'] and row['current'] for row in rows):raise SystemExit(1)
        return
    if args.rollback:
        with deployment_lock(operation='rollback release',folder=RELEASES.resolve().parent):release=LauncherUpdates.rollback(RELEASES)
        print(json.dumps({'revision':release.revision,'status':'published','deployment':'standard rolling updater'}),flush=True)
        return
    if args.paged_cache_only:
        from scripts.swarm_paged_cache import deploy_fleet
        with deployment_lock():rows=deploy_fleet(names=args.node)
        print(json.dumps({'paged_cache':rows}),flush=True)
        if not all(r.get('ready') for r in rows.values()):raise SystemExit(1)
        return
    if args.publish and not args.frozen_source:
        import subprocess,tempfile
        expected=publication_token(RELEASES)
        with tempfile.TemporaryDirectory(prefix='jswarm-source-release-') as temporary:
            snapshot=Path(temporary)/'Organization'
            workspace=ROOT.parent if ROOT.name=='JSwarm' else ROOT
            freeze_source(workspace,snapshot)
            project=snapshot/'JSwarm'
            env={**os.environ,'PYTHONPATH':os.pathsep.join(map(str,(project/'swarm-control/server',project,snapshot,snapshot/'GenomeHarbor/tests')))}
            subprocess.run([sys.executable,'-m','scripts.deploy_swarm_inputs','--publish','--frozen-source','--expected-publication',expected],cwd=project,env=env,check=True)
        return
    if args.publish:
        with deployment_lock(operation='validate and publish',folder=RELEASES.resolve().parent):
            expected=args.expected_publication if args.expected_publication is not None else publication_token(RELEASES)
            check_publication(RELEASES,expected)
            import subprocess
            updater=LauncherUpdates(ROOT,lambda:False,lambda *args:None)
            suites=['scripts/tests/test_launcher_routing.py','tests/test_launcher_routing_cleanup.py','tests/test_node_web.py','tests/test_worker_storage.py','tests/test_cache_lifetime.py','tests/test_cache_pressure_reclamation.py','tests/test_execution_accounting.py','tests/test_launcher_streams.py','tests/test_swarm_stream_work.py','scripts/tests/test_launcher_telemetry.py','tests/test_swarm_paged_cache.py','tests/test_swarm_shared_cache.py','tests/test_agent_cpu_overhead.py','tests/test_deployment_lock.py','tests/test_node_releases.py','tests/test_node_git_source.py','tests/test_swarm_node.py','tests/test_worker_journal.py','tests/test_ram_cache_inventory.py','tests/test_node_cache_manager.py','tests/test_ram_cache_cleanup_api.py','tests/test_swarm_cache_cleanup.py','tests/test_swarm_pressure_guard.py','tests/test_launcher_commands.py','tests/test_launcher_peer_supervisor.py','tests/test_resource_renewal.py','tests/test_swarm_runtime.py','tests/test_gpu_inventory.py','tests/test_node_execution.py','tests/test_swarm_gpu_runtime.py','tests/test_rolling_deployment.py','tests/test_docker_worker_restart.py','swarm-control/server/tests/test_agent_updates.py','swarm-control/server/tests/test_agent_sessions.py','swarm-control/server/tests/test_fleet_backups.py','tests/test_worker_maintenance.py']
            project=Path(__file__).resolve().parents[1]
            env={**os.environ,'PYTHONPATH':os.pathsep.join(map(str,(project/'swarm-control/server',ROOT,project.parent/'GenomeHarbor/tests')))}
            subprocess.run([sys.executable,'-m','pytest','-q',*suites],cwd=project,env=env,check=True)
            checked=LauncherUpdates(ROOT,lambda:False,lambda *args:None)
            if updater.files!=checked.files or updater.supervisor_source!=checked.supervisor_source or updater.components!=checked.components or updater.git_source!=checked.git_source:
                raise RuntimeError('Sources changed during validation; publish again after edits finish')
            published=updater.publish(RELEASES,expected=expected)
            print(json.dumps({'published':str(published),'revision':updater.revision,'generation':updater.generation}),flush=True)
            return
    # Every operational deployment follows the immutable publication.
    while True:
        if not (RELEASES/'desired').exists():
            if not args.continuous:raise SystemExit('No validated agent release has been published')
            time.sleep(30);continue
        try:
            with deployment_lock(operation='fleet rollout',folder=RELEASES.resolve().parent):done=sweep(args.node,args.wait_timeout,RELEASES/'desired')
        except RuntimeError:
            if not args.continuous:raise
            time.sleep(30);continue
        if done and not args.continuous:return
        if not args.watch and not args.continuous:raise SystemExit(1)
        time.sleep(30)

if __name__=='__main__':main()
