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
