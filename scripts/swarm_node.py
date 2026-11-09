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


def build_node_image(image='organization-swarm-node:1'):
    """Build all public node components into the image using the shared image policy."""
    import shutil
    from tempfile import TemporaryDirectory
    from docker_utils import build_image
    with TemporaryDirectory(prefix='organization-swarm-build-') as folder:
        destination=Path(folder)
        shutil.copyfile(ROOT/'Dockerfile.swarm-node',destination/'Dockerfile')
        ignored=shutil.ignore_patterns('__pycache__','*.egg-info','.runtime','.git','node_modules','tests','*.pyc')
        for relative in ('scripts','ramtop','swarm-control/server','docker_utils'):
            shutil.copytree(ROOT/relative,destination/relative,ignore=ignored)
        build_image(image,folder)
    return image


def main(argv=None):
    parser=argparse.ArgumentParser(prog='swarm-node',description=__doc__)
    parser.add_argument('command',nargs='?',default='run',choices=('run','status','drain','resume','update','enroll','join','monitor','telemetry','dashboard','performance','build','speed-test'))
    parser.add_argument('--serve',action='store_true',help='Run the built-in map backend in the foreground (dashboard command)')
    parser.add_argument('--prefix',default='lifetech-')
    parser.add_argument('--all',action='store_true',help='Apply a maintenance command to the enrolled fleet')
    parser.add_argument('--node',action='append',help='Select an enrolled node for maintenance; default is the fleet')
    parser.add_argument('--reason',default='Owner-requested maintenance')
    parser.add_argument('--watch',action='store_true',help='Retry an update when a node is offline or busy')
    parser.add_argument('--address',help='Enrollment SSH address or explicit speed-test destination IP')
    parser.add_argument('--target',help='Enrollment target: user@hostname.local')
    parser.add_argument('--origin',default='https://compai.lifetech.fyi',help='Trusted HTTPS swarm console for browser sign-in')
    parser.add_argument('--name',help='New node name for browser enrollment (hostname.local)')
    parser.add_argument('--bytes',type=int,default=64*1024*1024,help='Bytes per speed-test direction (maximum 256 MiB)')
    parser.add_argument('--transport',choices=('ssh','tcp','mptcp'),default='ssh',help='Speed-test data transport')
    parser.add_argument('--rounds',type=int,default=3,help='Speed-test rounds (1–5)')
    args=parser.parse_args(argv)
    sys.path.insert(0,str(ROOT/'scripts'))
    if args.command=='speed-test':
        if not args.node or len(args.node)!=1:parser.error('speed-test requires one --node enrolled-host.local')
        from scripts.swarm_speed_test import run
        options={}
        if args.address:options['address']=args.address
        if args.transport!='ssh':options['transport']=args.transport
        print(json.dumps(run(args.node[0],args.bytes,args.rounds,**options),indent=2))
        return
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
