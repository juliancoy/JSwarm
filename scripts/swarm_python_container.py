"""Dependency-only Python containers backed by the node's Organization repos."""
import json
from pathlib import Path
import re


def absolute(value):
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts or any(c in str(path) for c in ',\n\r\0'):
        raise ValueError('Use normalized absolute mount paths')
    return str(path)


def command(*, workspace, home, image, name, script, arguments=(),
            writable=(), fuse_mount=None, rootless=False, uid=1000, gid=1000):
    """Render a reproducible service without copying application source.

    Mount the whole workspace so cross-repository imports/symlinks survive.
    FUSE requires propagation to the host; rootless Docker cannot provide it.
    """
    workspace=absolute(workspace);home=absolute(home)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',image):raise ValueError('Pin the verified runtime image ID')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,100}',name):raise ValueError('Invalid service container name')
    source=Path(script)
    if source.is_absolute() or '..' in source.parts or source.suffix!='.py':
        raise ValueError('Python entry point must be inside Organization')
    if any(type(v) is not int or v<0 for v in (uid,gid)):raise ValueError('Invalid service identity')
    if rootless and fuse_mount:
        raise ValueError('Host-visible FUSE requires a system Docker daemon; rootless propagation is unsupported')
    args=['/usr/bin/docker','--host','unix:///var/run/docker.sock','run','--rm','--init','--pull=never','--name',name,
          '--network','host','--user',f'{uid}:{gid}','--read-only',
          '--cap-drop','ALL',
          '--tmpfs','/tmp:rw,nosuid,nodev,size=64m',
          '--env','HOME='+home,'--env','PYTHONDONTWRITEBYTECODE=1',
          '--env','PYTHONUNBUFFERED=1','--env','PYTHONPATH='+workspace,
          '--workdir',workspace]
    if not fuse_mount:args+=['--security-opt','no-new-privileges']
    # FUSE's setuid mount helper must acquire the bounded SYS_ADMIN capability.
    # Keep host paths identical inside: enrolled config, source provenance and
    # absolute symlinks retain their meaning. No controller key is mounted.
    mounts=[(workspace,True),(home+'/.config/lifetech-launcher',False),
            (home+'/.local/state/lifetech-launcher/bulk-transport',False),
            ('/etc/machine-id',True),('/etc/passwd',True),('/etc/group',True),
            ('/run/avahi-daemon',True),
            ('/dev/shm',False)]
    mounts.extend((absolute(path),False) for path in writable)
    seen=set()
    for path,readonly in mounts:
        path=absolute(path)
        if path in seen:continue
        seen.add(path)
        args+=['--mount',f'type=bind,src={path},dst={path}'+(',readonly' if readonly else '')]
    if fuse_mount:
        target=absolute(fuse_mount)
        args+=['--device','/dev/fuse','--cap-add','SYS_ADMIN',
               '--security-opt','apparmor=unconfined',
               '--mount',f'type=bind,src={target},dst={target},bind-propagation=rshared']
    args+=['--entrypoint','python3',image,str(Path(workspace)/source),*map(str,arguments)]
    return args


def unit(**options):
    args=command(**options)
    # systemd command syntax: escape specifiers and quote arguments individually.
    quote=lambda value:json.dumps(str(value).replace('%','%%'))
    return ('[Unit]\nDescription=Repo-mounted swarm Python service\nAfter=network-online.target\n'
            '[Service]\nType=simple\nExecStart='+' '.join(map(quote,args))+'\n'
            'ExecStop=/usr/bin/docker --host unix:///var/run/docker.sock stop --time 20 '+options['name']+'\n'
            'TimeoutStopSec=30\nRestart=on-failure\nRestartSec=5\nUMask=0077\n'
            '[Install]\nWantedBy=default.target\n')
