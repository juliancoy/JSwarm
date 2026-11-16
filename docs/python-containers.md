# Repo-mounted Python services

`Dockerfile.python-services` builds a shared dependency image with Python 3.12,
TLS dependencies and both FUSE libraries. It contains no application source.
Each machine keeps Organization and its child Git repositories under
`~/Documents/Organization`. Copy working files directly over SSH when requested;
keep credentials, payload data, virtual environments and generated caches out
of that copy. Preserve existing files with rsync backups and do not use deletion.

`scripts/swarm_python_container.py` renders a systemd user unit that runs the
verified image ID and mounts the Organization directory read-only at its original
absolute path. Cross-repository symlinks and imports retain their layout. Node
configuration and the shared RAM cache are separate mounts. The image's Python
and installed dependencies run the service; host Python packages are not used.

For host-visible FUSE, use the system Docker daemon, `/dev/fuse`, a shared mount
and the FUSE mount helper's bounded SYS_ADMIN capability. The container runs as
the owner UID/GID; read-only host passwd/group files resolve that identity.
Rootless Docker cannot propagate a FUSE mount into the host mount namespace.
Inspiron already has an accessible system daemon, even though its compute
workers use rootless Docker; the renderer explicitly selects the system socket.
Ordinary services keep no-new-privileges. FUSE permits its setuid mount helper
inside the container. No privileged container or Docker socket bind is used.

`tests/test_python_container.py` verifies real container mounts visible to the
host, mapped immutable reads and ordinary writes under both FUSE 2 and FUSE 3.
A passing container fixture is separate from full pipeline qualification and
from switching existing production service units.
