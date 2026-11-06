#!/usr/bin/env python3
"""One node runtime for discovery, dispatch, Docker workers, and fleet maintenance.

The existing modules remain implementation components and compatibility imports;
this command is the public entry point. Enrollment and protocol identities stay
stable so existing workers and controllers can reconnect during an upgrade.
"""
import argparse
import json
import os
import time
from urllib.request import urlopen
from pathlib import Path
import sys

ROOT=(Path(__file__).resolve().parents[2] if Path(__file__).resolve().parents[1].name in ('GenomeHarbor', 'JSwarm') else Path(__file__).resolve().parents[1])


RUNTIME_LABEL='organization.node-runtime'


def runtime_requirements():
    import tomllib
    project=Path(__file__).resolve().parents[1]
    monitor=tomllib.loads((project/'ramtop/pyproject.toml').read_text())['project']['dependencies']
    return (project/'requirements-node.txt').read_text().rstrip()+'\n'+'\n'.join(monitor)+'\n'


def runtime_fingerprint():
    """Only OS/Python dependency inputs invalidate the runtime image."""
    import hashlib
    project=Path(__file__).resolve().parents[1]
    return hashlib.sha256((project/'Dockerfile.swarm-node').read_bytes()+runtime_requirements().encode()).hexdigest()


def build_node_image(image='organization-swarm-node:1'):
    """Build the dependency image; source is always mounted at runtime."""
    from tempfile import TemporaryDirectory
    from docker_utils import build_image
    project=Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix='organization-swarm-build-') as folder:
        destination=Path(folder)
        dockerfile=(project/'Dockerfile.swarm-node').read_text()
        (destination/'Dockerfile').write_text(dockerfile+'\nLABEL '+RUNTIME_LABEL+'="'+runtime_fingerprint()+'"\n')
        (destination/'requirements-node.txt').write_text(runtime_requirements())
        build_image(image,folder)
    return image


def ensure_node_image(image='organization-swarm-node:1'):
    """Reuse the dependency image, building through build.sh only when needed."""
    import subprocess
    result=subprocess.run(['docker','image','inspect','--format',
                           '{{index .Config.Labels "'+RUNTIME_LABEL+'"}}',image],
                          capture_output=True,text=True,check=False)
    if result.returncode or result.stdout.strip()!=runtime_fingerprint():
        project=Path(__file__).resolve().parents[1]
        subprocess.run(['./build.sh'],cwd=project,check=True)
    return image


def main(argv=None):
    parser=argparse.ArgumentParser(prog='swarm-node',description=__doc__)
    parser.add_argument('command',nargs='?',default='run',choices=('run','status','drain','resume','update','enroll','join','monitor','telemetry','dashboard','performance','build'))
    parser.add_argument('--serve',action='store_true',help='Run the built-in map backend in the foreground (dashboard command)')
    parser.add_argument('--prefix',default='lifetech-')
    parser.add_argument('--all',action='store_true',help='Apply a maintenance command to the enrolled fleet')
    parser.add_argument('--node',action='append',help='Select an enrolled node for maintenance; default is the fleet')
    parser.add_argument('--reason',default='Owner-requested maintenance')
    parser.add_argument('--parallel',action='store_true',help='Update all selected nodes concurrently')
    parser.add_argument('--watch',action='store_true',help='Retry an update when a node is offline or busy')
    parser.add_argument('--address',help='Verified enrollment SSH address')
    parser.add_argument('--target',help='Enrollment target: user@hostname.local')
    parser.add_argument('--origin',default='https://compai.lifetech.fyi',help='Trusted HTTPS swarm console for browser sign-in')
    parser.add_argument('--name',help='New node name for browser enrollment (hostname.local)')
    args=parser.parse_args(argv)
    sys.path.insert(0,str(ROOT/'scripts'))
    if args.command=='join':
        from node_signon import join
        join(args.origin,args.name)
        return main(['run','--prefix',args.prefix])
    if args.command=='build':
        print('Built swarm node image: '+build_node_image())
        return
    if args.command=='monitor':
        package=ROOT/'ramtop'
        if (package/'ramtop').is_dir():sys.path.insert(0,str(package))
        else:
            from launcher_agent import load_performance_runtime
            load_performance_runtime()
        from ramtop.__main__ import main as monitor
        previous=sys.argv
        try:
            sys.argv=['ramtop'];return monitor()
        finally:sys.argv=previous
    if args.command=='performance':
        from launcher_agent import load_performance_runtime
        load_performance_runtime()
        from ramtop.swarm import main as publish
        return publish([])
    if args.command=='telemetry':
        from launcher_agent import PerformanceCollector
        collector=PerformanceCollector()
        try:
            collector.sample();time.sleep(1);print(json.dumps(collector.sample(),indent=2))
        finally:collector.close()
        return
    if args.command=='dashboard':
        config=Path(os.getenv('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
        values=json.loads(config.read_text())
        if args.serve:
            if values['name']!=values.get('master'):raise SystemExit('Only the enrolled master runs the shared map backend')
            import runpy
            folder=ROOT/'swarm-control/server'
            sys.path.insert(0,str(folder))
            import socket,threading
            try:
                with socket.create_connection(('127.0.0.1',8795),timeout=1):pass
            except OSError:
                from resource_server import main as resources
                threading.Thread(target=resources,daemon=True).start()
            return runpy.run_path(str(folder/'server.py'),run_name='__main__')
        print('Swarm Node dashboard: http://map.local/monitor.html')
        print('Native owner service: swarm-control.target; packaged backend: swarm-node dashboard --serve')
        return
    if args.command in ('status','drain','resume') and not args.all and not args.node:
        config=Path(os.getenv('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
        if args.command!='status':
            marker=config.parent/'maintenance.json';policy=config.parent/'worker-policy.json'
            if args.command=='drain' and not marker.exists():
                value={'started':time.time(),'reason':args.reason,'previous_policy':json.loads(policy.read_text()) if policy.exists() else {'mode':'auto'}}
                temp=marker.with_suffix('.tmp');temp.write_text(json.dumps(value));temp.chmod(0o600);temp.replace(marker)
            elif args.command=='resume' and marker.exists():
                previous=json.loads(marker.read_text())['previous_policy']
                if previous.get('mode') not in ('auto','always','paused'):raise ValueError('Invalid saved policy')
                temp=policy.with_suffix('.tmp');temp.write_text(json.dumps(previous));temp.chmod(0o600);temp.replace(policy)
                marker.rename(marker.with_name('maintenance-completed-'+str(time.time_ns())+'.json'))
        with urlopen('http://127.0.0.1:8791/launcher/status',timeout=10) as response:
            print(json.dumps(json.load(response),indent=2))
        return
    if args.command=='run':
        from launcher_peer_supervisor import main as run
        forwarded=['--prefix',args.prefix]
    elif args.command in ('status','drain','resume'):
        from worker_maintenance import main as run
        forwarded=[args.command,'--reason',args.reason]
        for node in args.node or []:forwarded+=['--node',node]
    elif args.command=='update':
        from deploy_swarm_inputs import main as run
        forwarded=['--watch'] if args.watch else []
        if args.parallel:forwarded+=['--parallel']
        for node in args.node or []:forwarded+=['--node',node]
    else:
        if not args.target:parser.error('enroll requires --target user@hostname.local')
        from onboard_node import main as run
        forwarded=[args.target]+(['--address',args.address] if args.address else [])
    previous=sys.argv
    try:
        sys.argv=['swarm-node',*forwarded]
        return run()
    finally:sys.argv=previous


if __name__=='__main__':main()
