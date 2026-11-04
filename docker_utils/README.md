# LifeTech Docker utilities

One local Python package for LAN-first Docker image resolution, streaming transfers,
Dockerfile base preparation, and container startup. No package has been published.

Install from the Organization workspace:

```sh
python -m pip install -e .
```

```python
import docker_utils
image_id = docker_utils.ensure_image('python:3.12-bookworm')
docker_utils.build_image('example:local', './example')
container = docker_utils.run_container({
    'image': 'nginx:alpine', 'name': 'example', 'detach': True,
})
client = docker_utils.from_env()  # SDK implicit pulls follow the same policy
```

`lan-docker pull IMAGE` and `lan-docker build -t TAG CONTEXT` expose the same policy.
A pull checks the local Docker image cache, then every saved LAN SSH peer, and only
then the registry. A cached peer image streams via `docker save | gzip` and
`gzip -d | docker load`; image archives are never stored on intermediate disk.
Destination image IDs/content metadata are verified. Unreachable peers do not
hide a later complete copy. Digest references use a deterministic local transfer
tag so Docker save/load does not lose their cache association.

By default, peers come from `~/.local/state/orgportal/lan-ssh-devices.json` and
use `~/.ssh/orgportal_lan_ed25519`, with strict known-host checking. Override
`DOCKER_UTILS_INVENTORY`, `DOCKER_UTILS_SSH_KEY`, and
`DOCKER_UTILS_KNOWN_HOSTS` for container mounts or other inventories. A missing
inventory means the local cache is checked before the registry; no arbitrary
network scan or unauthenticated Docker TCP endpoint is used.

Builds resolve external `FROM` and `COPY --from` references first, including
build arguments, stage aliases and digest references. Local bases are pinned in
the generated Dockerfile; CLI builds use the engine builder with `--pull=false`
to avoid a separate BuildKit cache pulling the same base from the WAN. BuildKit
only instructions and custom/remote SDK contexts are not supported by this
policy. Package-manager downloads inside `RUN` instructions are not Docker image
pulls and still use their configured repositories.

The policy applies to the workspace launchers, readiness helpers, recovery image
startup, distributed tests, and the package's SDK client. It cannot intercept
unrelated bare `docker pull`, external Compose invocations, or arbitrary
applications using their own Docker client. Use these entrypoints for LAN-first
behavior. Only explicitly configured SSH peers are trusted image sources;
mutable tags use the first complete local/LAN copy. Use digest references when
immutable upstream identity matters.

Imports do not connect to Docker. The image resolver uses Python's standard
library; SDK helpers load the `docker` dependency only when accessed.

### Existing FS-Cache references

`docker_utils.fscache.ReferenceCache(path)` observes an existing read-only NFS
mount with FS-Cache enabled. It does not create mounts, start helpers, drop RAM
pages, or change CacheFiles budgets. `report()` returns the source, mountpoint,
and before/after read counters; global FS-Cache counters are explicitly separate
from per-mount NFS READ operations. Missing counters remain unknown. Linux's
[FS-Cache documentation](https://www.kernel.org/doc/html/latest/filesystems/caching/fscache.html)
explains the underlying transparent network-filesystem cache.

GenomeHarbor's GPU mapper uses this adapter with immutable, content-identified
packed indexes. `scripts.network_reference_cache.resolve_gpu_index` reuses the
qualified reference helper, preserves RAM-only exclusions, and returns a
read-only artifact bind plus pinned-key mapper options. See the FS-Cache section
of the adjacent GenomeHarbor `genomics/VULKAN_MAPPER.md` for publication and worker
commands. This does not promote the experimental mapper into production or add
GPU-driven paging for a whole human reference.

### Shared GPU mapper clients

Local shard processes can use `scripts.gpu_mapper_client` to submit work to one
already-admitted `scripts.gpu_mapper_worker`. The worker owns the index upload,
GPU arena, staging buffer and one native-I/O job slot. Its bounded queue runs one
mapping job at a time; clients transfer regular-file descriptors over an
owner-only Unix socket and acquire no second GPU lease. Container clients need
only the socket-directory bind, not matching input paths or GPU devices.

`network_reference_cache.resolve_gpu_index(..., worker_socket=PATH)` includes the
socket bind and shared-worker options for that placement. Artifact identity and
alignment options must match the running service. Status distinguishes shared
resident bytes from job transfer counts; initial FS-Cache counters belong to the
service's index load. See GenomeHarbor's `genomics/VULKAN_MAPPER.md` for startup,
submission, memory budgeting and shutdown behavior.


Shared mapper admission defaults to a 0.25-CPU local reservation. `--cpu-burst`
allows a low-priority feeder to use at most 0.25 CPU of additional total pool
reservation when CPU ceilings are otherwise fully booked. It does not relax RAM
or PID checks, alter other containers, or configure a Docker CPU quota. The RAM
model counts staging and one conservative UMA allowance instead of four host
arenas. For a 32 MiB arena and sixteen connections the base request is 372 MiB,
plus manifest allowance; the runtime/driver headroom remains explicit.
