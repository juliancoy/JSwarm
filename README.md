# JSwarm

Swarm discovery, scheduling, transport, Docker execution, launcher supervision and fleet control.

For node and controller updates, follow the [no-drain replacement procedure](docs/REPLACEMENT.md).

## Worker storage policy

RAM-only operation is a node container setting. Set
`JSWARM_WORKER_STORAGE=ram-only` in the container environment to pin it, or run
`python run.py --worker-storage ram-only` on the enrolled owner to persist
`worker-storage.json` in the mounted launcher configuration. The default is
`durable`. An environment setting takes precedence over the configuration file;
invalid settings fail closed. Both survive normal immutable node releases.

The authenticated node API reports the effective setting in
`status.storage_policy` and `worker.storage`. The controller-signed
`worker.storage.set` operation accepts `{"mode":"ram-only"}` or
`{"mode":"durable"}` and updates the mounted configuration without restarting
the container. It cannot override an environment-pinned setting.

In RAM-only mode, streaming jobs must explicitly declare bounded RAM scratch.
Managed Docker workers require a read-only root, equal memory and memory-swap
limits, and no Docker log file. Payload bind mounts are checked in the enrolled
owner's filesystem namespace, including resolved symlinks; disk-backed mounts
are refused. Shared payload volumes must be native Docker tmpfs volumes. The
host must have swap disabled. Read-only GPU driver/runtime code may remain on
disk, just like image layers; GPU scratch and payloads stay in RAM. The
shared worker runtime checks mounts before reserving capacity. Existing running
jobs finish; the policy applies to new launches and queued work. Privileged owner
commands remain administrative access, rather than a workload sandbox.

This controls worker payloads, inputs, outputs and scratch. Enrollment, image
layers, queue metadata and execution receipts remain persistent control state.
Full host I/O pressure can still delay RAM-only jobs when other processes use a
disk. Apply the policy through the standard validated node release before
configuring an older container; old releases do not enforce this setting.

Build the node image with `PYTHON=/path/to/python ./build.sh` from this directory.

- `scripts/`: swarm node runtime, launcher, enrollment, Docker workers and deployment tools.
- `docker_utils/`: Docker image resolution, streaming and container management (`lan-docker`).
- `swarm-control/`: controller, dashboard and Android client.
- `ramtop/`: node resource monitoring.
- `peer-worker/`: peer transport service.
- `Dockerfile.swarm-node`: current swarm node image, formerly the lifetech launcher.
- `Dockerfile.launcher`: legacy launcher base image.
- `run.py`, `swarm-node`: launcher and node entry points.

Keep this repository beside `GenomeHarbor` in the Organization workspace. Compatibility links preserve deployed paths and shared `scripts.*` imports. Genome-specific scripts and UI assets are owned by GenomeHarbor. Existing image tags and enrollment identities are preserved.

Install from the workspace: `python -m pip install -e ./JSwarm`. Run host tests from here: `python -m pytest tests`. Build the node image from the workspace: `./swarm-node build`.

This is an independent Git repository: https://github.com/juliancoy/JSwarm.

Committed-snapshot deployment pins this repository alongside Organization and
GenomeHarbor. Legacy links rely on the adjacent workspace layout.

## Shared node memory

JSwarm owns the reusable shared RAM cache in
[`scripts/swarm_shared_cache.py`](scripts/swarm_shared_cache.py).
Jobs declare an immutable set of files and use `ensure(host, image, files, load)`
to prepare one verified cache per node. `image` is a pinned Docker image ID;
each file declares its byte count and SHA-256. The application supplies the
`load(owner_container)` callback that streams those files into `/dev/shm`.
JSwarm verifies the payload before publishing the cache for reuse.

Pass the returned descriptor to `swarm_runtime.run_job(..., shared_cache=cache)`.
Concurrent jobs mount the same native Docker tmpfs volume read-only at
`/dev/shm`; keep mutable scratch in a separate private mount, such as `/work`.
The existing scheduler charges the cache owner's RAM once, while each job's
`memory_mb` covers its private working memory and scratch. The cache owner
also reserves one CPU slot. Job reports include the cache descriptor.

JSwarm controls identity, admission, concurrent loading, verification and
safe cleanup. Cache ownership comes from input bindings in the existing durable
job requests, across namespaces and machines; there is no separate cache claims
ledger. Prepared, queued, paused and retrying jobs retain their inputs. Completion
or confirmed cancellation allows cleanup only after no unfinished job needs the
entry and Docker confirms that no consumer or transfer uses it. Incomplete
preparation and unknown queue state preserve caches. Capacity pressure can retry
cleanup of terminal job data, but cannot evict inputs owned by unfinished jobs.
External shared volumes remain owned by their coordinator queue; the node's
stream journal cleans only its own streamed input entries. Cache preparation
honors the node's worker policy and live capacity. There is no cache-wide or
derived-object file lock: Docker's unique container name elects the loader;
competing requests retry until its verified seal is available. Terminal cleanup
sweeps all eligible entries rather than stopping at a pressure budget.

Applications choose cache contents, transfer method, private memory budget and
fallback policy. GenomeHarbor's BWA adapter uses this interface; BWA formats,
reference selection and alignment validation remain in GenomeHarbor. Across
nodes, files are transferred into independent local caches. This interface
provides shared host RAM, not shared VRAM or remote pointer access.

Validation: `python -m pytest tests/test_swarm_shared_cache.py tests/test_swarm_runtime.py`.
The Docker integration check exercises concurrent consumers, read-only mounts,
cache reuse, payload rejection and alignment output parity.

## Fleet releases

The authoritative launcher release is the immutable, checksummed artifact selected
by `~/.local/state/swarm-control/node-releases/desired` on the coordinator.
Manual deployment, streaming deployment, readiness checks, backups and maintenance
use this publication. Workspace edits become deployable only after validation and
publication; do not patch installed node releases or exclude nodes to retain a fix.

Run these commands from the Organization workspace with the configured Python environment:

```bash
python JSwarm/scripts/deploy_swarm_inputs.py --publish
python JSwarm/scripts/deploy_swarm_inputs.py --status
```

Publication runs the required release checks, verifies that bundled sources did not
change during validation, and atomically selects the artifact. The existing
`swarm-input-deployment.service` follows that publication and replaces one enrolled
node at a time. It requires the exact published revision and readiness before
continuing. An unavailable agent can be recovered through the existing pinned
owner SSH transport after checking its enrolled node identity. Legacy supervisors
stuck draining are restarted into the installed supervisor; worker containers
remain independently owned. Docker workers retain their own lifecycle. Status is read-only and
exits unsuccessfully if a selected node is unreachable, unready or on another
revision. `--node NAME` selects an enrolled node for status or a manual rollout.

To run the same rollout manually, use `python JSwarm/scripts/deploy_swarm_inputs.py`.
The legacy `--published` flag remains compatible; published deployment is now the
default. Use `--paged-cache-only` explicitly for paging-cache provisioning; it does not
run during launcher releases or establish launcher readiness. Native resource monitors and application controller images
have their own deployments; this artifact covers the launcher, supervisor and
bundled command, streaming and performance runtimes.

```bash
python JSwarm/scripts/deploy_swarm_inputs.py --rollback
python JSwarm/scripts/deploy_swarm_inputs.py --status
```

Rollback verifies the previous immutable publication before selecting it for the
same rolling updater. Republishing the current release preserves the previous
release. Publication, rollback and deployment share a lock. The deployment report
is `~/.local/state/swarm-control/input-deployment.json`; service logs are available
with `journalctl --user -u swarm-input-deployment.service`.


## Independent private source catalogue

`scripts/deploy_node_source.py` installs `swarm-node-source` on an existing
enrolled source machine. It serves shard receipts, destination-specific input
offers and bounded output grants over mTLS on bulk transport port + 2 (9450).
Controllers use `scripts.swarm_source_client.SourceClient`; catalogue access
does not use the node command channel. Input bytes continue through the existing
bulk transport and outputs through the existing result sink.

The container reuses the machine's certificates and enrolled identity. It has
no Docker socket, a read-only root, read-only input mounts, and write access
only to capability manifests and configured output directories. Replacing a
node does not replace this service or require running workers to finish.

To provision or update its immutable release, run from the workspace root:

```sh
python -m scripts.deploy_node_source --inventory /path/to/recall-hosts.json \
  --host julian-latitude.local --input /path/to/source/generations \
  --output /path/to/approved/results
```

Only approved input/output roots are exposed. New dataset roots must be
explicitly provisioned. Installation uses administrative access; steady-state
requests use existing certificates. Check `/health` with `SourceClient` or
inspect the container's Docker health status on its source machine.

## Demand-paged public references

JSwarm's standard fleet publication includes the checksummed `public-pager`
component. The normal deployer provisions it for every enrolled machine from
that immutable snapshot. Legacy publications continue their existing behavior;
deployment never imports an unpublished pager from the working directory.

An application explicitly publishes a public immutable artifact set through
`scripts.swarm_paged_cache.publish`. Deployment verifies source checksums, sets
up one read-only NFS export, discovers enrolled LAN address aliases, and creates
bounded node mount helpers. Each artifact generation has its own server identity
and names, so an updated export can coexist with readers of an earlier one.
Private inputs and results continue through their authenticated transfer path.

Workers map the files directly. Linux fetches missing pages into reclaimable
RAM; startup performs bounded checksum and read-only mmap checks instead of a
complete destination download. The source node maps its original verified files
locally. System and existing rootless Docker engines are checked during node
provisioning. Compute containers receive read-only file mounts and no mount
privileges. Only the dedicated deployment helpers manage kernel mounts.

For reclaimable mappings, `run_job(memory_reservation_mb=...)` separates the
private RAM admission reservation from the larger no-swap container hard limit.
The reservation must cover private tmpfs; applications must also budget their
private heap and buffers. Existing callers reserve their entire hard limit.
Docker remains authoritative for running limits, and measured host memory/I/O
pressure gates further admission. This lets shared cached pages stay resident
without treating their whole virtual mapping as a fresh private allocation.

The owner-only dataset plan is under
`~/.local/state/swarm-control/submission/paged-cache/deployment.json`.
Per-node verification records live alongside it. Unavailable nodes retain the
verified complete-cache fallback. GenomeHarbor owns BWA reference selection,
alignment parity qualification, and private job memory budgets; it keeps an
existing verified warm RAM cache in preference to a cold network mapping.


## Pipeline device controls

Select a machine in the pipeline inspector to Pause, Update, Resume, or use Auto.
Pause closes new-job admission while existing workers continue. Resume restores
the previous policy; Auto explicitly selects battery-aware scheduling.

The dashboard sends fixed commands through the selected node’s signed JSwarm
command API. Update uses the checksummed published release and the standard fleet
deployment lock, then verifies that the same machine reports that revision ready.
Node status is polled independently of resource and transfer diagnostics about
once a second. State bubbles show observed admission, policy, power and update
states; unavailable observations are marked stale instead of treated as success.

## Pipeline UI in the node

The existing `swarm-node` process serves `pipeline.html` and its machine links.
There is no separate node web container or web deployment registry. Public assets
are bundled into the immutable launcher publication and covered by its checksum.
Use `./build.sh` and the standard validated fleet publication/update procedure.

`http://jswarm.localhost/pipeline.html` opens the local node's page on
port 80. Machine links such as
`http://compai.jswarm.localhost/pipeline.html` route through that same node
process to the selected enrolled node. The existing routing helper maintains
port 80 forwarding to the main node’s unprivileged listeners; it adds no web server. Direct links
such as `http://compai.local/pipeline.html` also work. The page identifies
its serving node and links to the other machines. Shared queue APIs use the
existing coordinator. Host and Origin checks remain enforced; private keys and
enrollment configuration are never returned to the browser.

For a node whose jobs use rootless Docker, the existing node service runs a
short-lived host network helper before startup. Its only capability is
`NET_ADMIN`; it maintains the same port 80 rules without exposing the system
Docker socket to the node or running another HTTP process.
