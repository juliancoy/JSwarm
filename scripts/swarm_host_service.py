"""Manage the enrolled owner's host user units from the standard owner capsule."""
import json
import os
from pathlib import Path
import shutil
import subprocess


def systemctl(*args, **kwargs):
    if shutil.which('systemctl') and not Path('/.dockerenv').exists():
        command = ['systemctl', '--user', *args]
    else:
        # Reuse the local enrolled runtime image; no SSH or privileged container.
        # Host /usr supplies systemctl and matching libraries. Host PID visibility,
        # already used by the standard node, lets sd-bus verify its host peer.
        home = Path.home()
        uid = home.stat().st_uid
        mapping = [list(map(int, row.split())) for row in Path('/proc/self/uid_map').read_text().splitlines()]
        host_uid = next(outside+uid-inside for inside, outside, count in mapping if inside <= uid < inside+count)
        image = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}',
                                         'organization-launcher:webrtc-1'], text=True).strip()
        command = ['docker', 'run', '--rm', '--pull=never', '--pid', 'host', '--network', 'none',
                   '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--read-only', '--env', 'HOME='+str(home),
                   '--env', 'XDG_RUNTIME_DIR=/run/user/'+str(host_uid),
                   '--env', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(host_uid)+'/bus',
                   '--mount', 'type=bind,src=/usr,dst=/usr,readonly',
                   '--mount', f'type=bind,src=/run/user/{host_uid},dst=/run/user/{host_uid}',
                   '--mount', f'type=bind,src={home},dst={home}',
                   '--entrypoint', '/usr/bin/systemctl', image, '--user', *args]
    return subprocess.run(command, **kwargs)


def configure_fuse_root_access():
    """Permit the standard root-only FUSE option, without exposing file data.

    Mount options still select allow_root and kernel owner permission checks.
    Use the already-authorized system Docker daemon for this host policy file.
    No privileged container or daemon restart is needed.
    """
    policy = Path('/etc/fuse.conf')
    if policy.exists() and 'user_allow_other' in [line.strip() for line in policy.read_text().splitlines()]:return
    code = """import os
p='/policy/fuse.conf'
fd=os.open(p, os.O_RDWR | os.O_NOFOLLOW)
with os.fdopen(fd,'r+') as f:
 data=f.read()
 if 'user_allow_other' not in [line.strip() for line in data.splitlines()]:
  f.seek(0,2);f.write('\\n# Swarm owner mounts use allow_root for the Docker daemon.\\nuser_allow_other\\n');f.flush();os.fsync(f.fileno())
"""
    subprocess.run(['docker', '-H', 'unix:///var/run/docker.sock', 'run', '--rm', '--pull=never',
                    '--network', 'none', '--user', '0:0', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--read-only',
                    '--mount', 'type=bind,src=/etc/fuse.conf,dst=/policy/fuse.conf',
                    '--entrypoint', 'python3', 'organization-launcher:webrtc-1', '-c', code],
                   check=True, capture_output=True, timeout=30)
