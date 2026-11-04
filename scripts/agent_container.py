"""Render a user service that runs the enrolled agent entirely in Python Docker."""
import argparse
import json
from pathlib import Path
import re
import shlex


def service(*, source, config, jobs, socket, image, uid, gid, socket_gid, hostname, gpus=False, worker_socket=None, fixed_release=False):
    paths=[Path(value) for value in (source,config,jobs,socket)]
    if any(not path.is_absolute() or '..' in path.parts for path in paths):
        raise ValueError('Agent paths must be absolute and normalized')
    source,config,jobs,socket=map(str,paths)
    if socket!='/var/run/docker.sock':
        raise ValueError('Host networking requires the system Docker socket; rootless Docker needs a separate migration')
    if not re.fullmatch('sha256:[a-f0-9]{64}',image):
        raise ValueError('Use a locally verified immutable Python 3.12 image ID')
    if any(type(value) is not int or value<0 for value in (uid,gid,socket_gid)):
        raise ValueError('Invalid service identity')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*',hostname):
        raise ValueError('Invalid node hostname')
    worker_socket=worker_socket or socket
    if not Path(worker_socket).is_absolute() or '..' in Path(worker_socket).parts:raise ValueError('Worker Docker socket must be an absolute normalized path')
    home=str(Path(config).parent.parent.parent)
    args=['/usr/bin/env','DOCKER_HOST=unix://'+socket,'/usr/bin/docker','run','--rm','--init','--pull=never','--name','organization-test-agent',
          '--network','host','--pid','host','--hostname',hostname,'--user',f'{uid}:{gid}',
          '--group-add',str(socket_gid),'--cap-drop','ALL','--security-opt','no-new-privileges',
          '--read-only','--tmpfs','/tmp:rw,nosuid,nodev,size=128m']
    health=shlex.join(['python','-c',"from pathlib import Path; import json,urllib.request; assert Path('/tmp/launcher-agent-ready.json').is_file(); json.load(urllib.request.urlopen('http://127.0.0.1:8791/launcher/status',timeout=2))"])
    args+=['--health-cmd',health,'--health-interval','10s','--health-timeout','5s','--health-start-period','180s','--health-retries','3']
    for path,readonly in ((source,True),(str(Path(config).parent),False),(jobs,False),(worker_socket,False),('/etc/machine-id',True)):
        args+=['--mount',f'type=bind,src={path},dst={path}'+(',readonly' if readonly else '')]
    for value in ('HOME='+home,'DOCKER_HOST=unix://'+worker_socket,'LIFETECH_PEER_CONFIG='+config,
                  'LIFETECH_JOB_ROOT='+jobs,'PYTHONUNBUFFERED=1','PYTHONDONTWRITEBYTECODE=1'):
        args+=['--env',value]
    if gpus:args+=['--gpus','all']
    entry='launcher_peers.py' if fixed_release else 'launcher_peer_supervisor.py'
    args+=['--entrypoint','python',image,str(Path(source)/entry)]
    quote=lambda arg:json.dumps(str(arg).replace('%','%%'))
    return ('[Unit]\nDescription=Containerized enrolled swarm agent (Python 3.12)\nAfter=network-online.target\n\n'
            '[Service]\nType=simple\nExecStart='+' '.join(map(quote,args))+'\n'
            'ExecStop=/usr/bin/env DOCKER_HOST=unix://'+socket+' /usr/bin/docker stop --time 30 organization-test-agent\n'
            'TimeoutStopSec=45\nRestart=on-failure\nRestartSec=5\nUMask=0077\n\n'
            '[Install]\nWantedBy=default.target\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','config','jobs','socket','image','hostname'):
        parser.add_argument('--'+name,required=True)
    for name in ('uid','gid','socket-gid'):
        parser.add_argument('--'+name,type=int,required=True)
    parser.add_argument('--worker-socket',help='Keep worker jobs on their existing Docker daemon')
    parser.add_argument('--gpus',action='store_true')
    parser.add_argument('--fixed-release',action='store_true',help='Run the mounted source release directly; change it by replacing this service')
    print(service(**vars(parser.parse_args())),end='')


if __name__=='__main__':main()
