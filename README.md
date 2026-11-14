# JSwarm

Swarm discovery, scheduling, transport, Docker execution, launcher supervision and fleet control.

For node and controller updates, follow the [no-drain replacement procedure](docs/REPLACEMENT.md).

The task and pipeline APIs share background observation refreshes across clients.
Responses reuse compact JSON and gzip encodings; request threads do not wait for
queue scans or node polling. `sampled_at` and `X-Observation-Age` describe the
retained observation, including during refresh. An initial cold snapshot returns
503 with `Retry-After: 1`; a failed refresh retains the previous snapshot with a
Warning header. Machine controls continue to use the current node API directly.

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
- `Dockerfile.swarm-node`: dependency-only node image, built with `./build.sh`.
- `run.py`, `swarm-node`: launcher and node entry points.

Keep this repository beside `GenomeHarbor` in the Organization workspace. Compatibility links preserve deployed paths and shared `scripts.*` imports. Genome-specific scripts and UI assets are owned by GenomeHarbor. Existing image tags and enrollment identities are preserved.

Install from the workspace: `python -m pip install -e ./JSwarm`. Run host tests from here: `python -m pytest tests`. Build the node image from the workspace: `./swarm-node build`.

This is an independent Git repository: https://github.com/juliancoy/JSwarm.

Committed-snapshot deployment pins this repository alongside Organization and
GenomeHarbor. Legacy links rely on the adjacent workspace layout.

## Shared node memory

Each updated node has one bounded RAM cache keeper, managed by its agent through
controller-signed `resource.cache.*` operations. Immutable object sets live in
separate directories of one Docker tmpfs volume. Jobs mount only their required
object directories read-only using Docker `volume-subpath`; they never see the
whole cache. Docker Engine must support volume subpaths. The keeper charges its
payload commitment once, with one 128 MiB allowance for the node instead of one
allowance and sleeping container per object. Configured limits are not measured
physical RAM usage.

The default payload budget is 25% of effective node RAM. The node owner can set
`SWARM_CACHE_BUDGET_MB` before agent startup. Admission also checks actual node
headroom; a cache budget does not reserve permission to exhaust host RAM. Idle
objects are evicted in least-recently-used order to make room. Explicit cleanup
removes all unpinned managed objects without consulting controller queue state.
Queued jobs retain immutable preparation instructions, not permanent RAM pins.
An input miss returns to preparation and rebuilds or transfers the same verified
bytes before launch. Derived references are regenerated from their original
source; cached lookup metadata alone never proves residency.

Loading, execution and transfers hold renewable 120-second pins. A lost
controller's pins expire. Running and stopped Docker consumers independently
protect their exact object directories even after agent replacement; unknown
whole-volume readers or untracked keeper execs conservatively block eviction.
The RAM manifest survives agent replacement with the keeper, and disappears with
the payload on reboot. Results and private scratch remain outside this cache.

Applications call `swarm_shared_cache.ensure(..., load_path=True)`. The loader
accepts `(owner_container, object_directory)` and streams files there. For older
nodes it receives just `owner_container` and must default its directory to
`/dev/shm`. Descriptors continue to contain pinned image IDs, byte counts and
SHA-256 hashes. Generic `swarm_runtime.run_job` pins managed inputs through Docker
cleanup. Peer transfers pin the source through completion. Loader paths, seals,
instance identities and read-only file identities are checked before reuse.

Legacy per-object owners remain readable during migration and retain their
previous terminal-queue and Docker-reference cleanup checks. Existing immutable
node recipe bindings continue using their legacy owners until replaced; no
accepted worker is stopped or rebound by this change. New managed genomics
staging and recipe bundles share the node keeper. The pipeline shows one cache
summary per machine, including overlapping legacy owners during migration.

Validation: `python -m pytest tests/test_node_cache_manager.py
 tests/test_swarm_shared_cache.py tests/test_swarm_runtime.py` (on one line).
The manager tests use uniquely named local Docker volumes and never enumerate or
remove production cache holders. Build the runtime with `./build.sh`; publish and
roll out through the standard validated release path.

## Fleet releases

Node application code comes from host-mounted Git source; the Docker image
contains runtime dependencies and is reused for code-only updates. See
[Git source deployment](docs/node-source-deployment.md) for mount layout, Git
access requirements, initial provisioning and dependency changes.

Commit and push Organization, JSwarm and GenomeHarbor to origins reachable by all
nodes. From the Organization workspace:

```bash
python -m scripts.deploy_swarm_inputs --publish
python -m scripts.deploy_swarm_inputs --status
```

`--publish` checks the committed Organization, JSwarm, and GenomeHarbor sources
for sensitive filenames, private keys, common provider tokens, literal secret
assignments, and credentials in URLs. A finding or scan error blocks publication;
reports contain paths, line numbers, and rule names, never credential values.
Reviewed test examples have exceptions bound to their repository, file, rule, and
exact matched-content SHA-256 in `scripts/release-secrets-allowlist.json`; a
changed value must be reviewed again.
The scan includes tracked ignored and export-ignored files, then rechecks the
exported source before validation. These pattern checks cover the selected source
commits, not Git history, and cannot identify every kind of sensitive information.
Use `python -m scripts.release_secrets /path/to/repository --revision HEAD` to
check a commit independently before pushing it.

After validation, `--publish` publishes and rolls out that exact release in one
command, honoring `--node`, `--wait-timeout`, and `--parallel`. It fails if another
publication supersedes it or selected nodes cannot become ready. Use
`--publish-only` to validate and publish without rollout. Running the command
without either flag still updates nodes to the already published release.

Publication validates committed snapshots and atomically selects the checksummed
artifact at `~/.local/state/swarm-control/node-releases/desired`. The updater uses
each enrolled node's signed owner-command API to fetch those exact commits into
dedicated detached worktrees. Nodes regenerate the runtime from Git and verify it
against the publication before activation. Local edits are excluded. Deployment
requires Git provenance and does not fall back to SSH or source-only staging.

The existing `swarm-input-deployment.service` follows the same publication and
updates one enrolled machine at a time. Each node must report the target revision,
readiness and enrolled machine identity before rollout continues. `--node NAME`
selects a machine. An offline node remains pending until its API is available.
Admission briefly closes while the supervisor replaces the agent process;
Docker-owned workers continue under their existing container IDs.

Validation, publication, rollback and deployment share the coordinator's
`~/.local/state/swarm-control/input-deployment.lock`. Competing commands report the
lock holder. Never delete the lock file to bypass a running deployment. Changed
publications have increasing `generation` numbers; repeating the current release
is a no-op. Stale publication attempts cannot replace a newer selected release.

To select and deploy the previous Git-backed release:

```bash
python -m scripts.deploy_swarm_inputs --rollback
python -m scripts.swarm_node update
python -m scripts.deploy_swarm_inputs --status
```

Publications lacking Git provenance cannot be deployed or selected for rollback.
Republish committed source to replace an old desired publication. Historical
receipts remain available for auditing and generation ordering. Unsupported old
supervisors require recovery through the normal local bootstrap; the updater no
longer bridges draining supervisors or restarts legacy containers.

Use `./build.sh` from JSwarm for provisioning or changed runtime dependencies,
then recreate the affected node container. Code-only updates do not build images
or install Python packages. The optional public-pager component uses the same
publication; `--paged-cache-only` provisions it without replacing launchers.
Application controller images have their own releases.

The rollout receipt is `~/.local/state/swarm-control/input-deployment.json`;
inspect logs with `journalctl --user -u swarm-input-deployment.service`.


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
that validated Git-backed publication. Deployment never imports an unpublished
pager from the working directory.

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
Deployment checks system Docker access before distributing a paging helper image.
A rootless API owner without that socket keeps the verified shared RAM-cache
fallback; deployment does not grant it system Docker access or enable payload
disk fallback. Its optional paging result records the missing prerequisite.

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
The existing node endpoint serves public UI files directly from its mounted
Organization checkout when `SWARM_WORKSPACE` is set (as in the standard local
runner). It reads files on every request and sends `Cache-Control: no-store`, so
HTML, CSS and JavaScript edits appear on refresh without a build or node restart.
`SWARM_WEB_WORKSPACE` or the owner setting `web_workspace` can select another
absolute, mounted checkout. Only `JSwarm/swarm-control/server/view` and its public
GenomeHarbor web aliases are served; API authentication and proxying are unchanged.
A missing source file returns 404 rather than silently serving old bundled code.
Nodes without a source mount retain the checksum-covered publication assets.
There is no separate node web container or web deployment registry.
Use the standard Git-backed publication/update procedure; rebuild with
`./build.sh` only when runtime dependencies change.

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


### Node-to-node speed test

From an enrolled node, run:

```sh
./swarm-node speed-test --node julian-latitude.local
```

The test uses existing batch-mode SSH access and remote Python 3. It transfers
64 MiB in each direction for three rounds, verifies SHA-256 at each receiver,
and prints JSON with explicit endpoints, elapsed seconds, and decimal Mbps.
SSH compression is disabled. Results include encryption and receiver acknowledgement; SSH connection setup
is reported separately. They measure application throughput rather than
raw Ethernet capacity. SSH mode needs no remote installation, listener, or
service restart. Each transfer has a 30-second deadline. Override `--bytes` (up to
268435456) and `--rounds` (1–5) to change the bounded workload.

To isolate the wired path from SSH encryption and hostname address selection:

```sh
./swarm-node speed-test --node julian-latitude.local --address 10.42.0.222 --transport tcp --bytes 268435456
```

Use the peer's current wired IP. `--address` retains SSH host-key verification
against the enrolled node name. The JSON records the destination IP.
TCP mode starts a temporary single-connection listener bound to that address,
with a random token delivered over verified SSH and a 30-second remote timeout.
It sends only synthetic test data without encryption, verifies SHA-256, and
removes the listener when the connection finishes. SSH remains the default.
On November 8, 2026, Latitude's `.local` name resolved to Wi-Fi (`10.42.0.71`),
while its USB Ethernet address was `10.42.0.222`; the old enrolled `.137` address
was unreachable. Explicit wired TCP measured median 2.309 Gbps CompAI → Latitude
and 2.299 Gbps Latitude → CompAI across three 256 MiB transfers per direction.
CompAI's Ethernet negotiated at 2500 Mb/s. Application throughput includes
protocol and verification costs and is lower than physical link speed.

Use the built-in swarm MPTCP socket factory for both ends of the benchmark:

```sh
./swarm-node speed-test --node julian-latitude.local --address 10.42.0.222 --transport mptcp --bytes 268435456
```

This mode reuses `launcher_registry.transport_socket`, including its remote
implementation streamed over verified SSH, and reports Linux `MPTCP_INFO` for
each connection. `negotiated` and `tcp_fallback` distinguish genuine MPTCP from
TCP fallback. Kernel subflow counts can include connection attempts; they do
not prove that every subflow is established or carrying data. No endpoint or
routing configuration is changed by the benchmark.
A November 8 repeat comparison measured median MPTCP 2.091/2.245 Gbps versus
TCP 2.201/2.343 Gbps (CompAI → Latitude / Latitude → CompAI). Live `ss -tni`
inspection showed the primary wired subflow transferring data and an extra
subflow from CompAI's `172.20.20.20` Wi-Fi address to Latitude's wired address
in `SYN-SENT`. These measurements establish MPTCP negotiation, not bandwidth
aggregation across working wired and wireless paths.
# Logged host tests

From the Organization directory, run tests through the master launcher:

```sh
python3 run.py --test python3 -m unittest discover -s JSwarm/tests -p test_logged_runner.py
```

Source discovery is automatic: the current Git repository and its immediate
child repositories are fingerprinted using tracked files and nonignored new
files. Content edits, additions, deletions, and renames invalidate the recorded
result without requiring a commit. Git-ignored files and generated test logs
are excluded. Without Git, the current directory is scanned instead.
This conservatively reruns the command when any discovered repository input
changes; it does not infer which individual tests cover a changed file.
For narrower scope or ignored/external dependencies, repeat `--test-source` for
all relevant code, tests, configuration, and fixtures (files or directories).
Explicit sources replace automatic discovery. Put `--test` and its command last.
Each input or discovered repository gets an adjacent `*.test-log.jsonl` (directories use `.test-log.jsonl`)
with the command, UTC date, input fingerprint, duration, exit code, and output
path. Output is retained in an adjacent `.test-output/` directory.
Matching inputs, command, Python runtime, and environment reuse the recorded exit
code, including failures. Changed inputs run again; interrupted runs and inputs
modified during a test are not reused. This applies to commands invoked through
`--test`; direct test commands do not pass through the launcher.


### Browser sign-in and WAN node enrollment

On a new Linux machine with JSwarm and its runtime dependencies installed, run:

```sh
./swarm-node join --origin https://compai.lifetech.fyi
```

The command opens the owner's browser sign-in, which already uses PIdP
Authorization Code with S256 PKCE. After sign-in, compare the node name and
machine fingerprint shown by the command and approve that computer. A second
S256 grant binds provisioning delivery to the native node process; the browser
never receives fleet keys or node registration credentials. Grants expire after
ten minutes, and revoked or disallowed browser sessions cannot finish them.
Provisioning delivery can safely retry after an interrupted response. Existing
fleet membership is never overwritten by `join`.

The node stores enrollment material with owner-only permissions, registers with
the existing signaling service, and starts JSwarm. Approved WAN nodes use WebRTC
explicitly, including when their private address overlaps the coordinator's LAN.
The scheduler checks the approved machine fingerprint before accepting them.
Enrollment and channel readiness are separate; check launcher status to confirm
an open connection. TURN configuration is intentionally deferred.

The HTTPS console serves `/pipeline.html` and its read/control endpoints behind
the existing PIdP session checks. LAN `map.local` retains its existing network
access policy. Both use the same pipeline backend: queue definitions and verified
acceptance receipts belong to the coordinator, while each worker supplies its
execution journal, timing receipts, resource measurements, and cache-worker
status through authenticated JSwarm RPC. Unreachable nodes are marked unavailable;
retained measurements are marked stale rather than replaced by coordinator
telemetry. Historical node journals warm up in bounded pages.

Deploy the web integration with `./run.py --pipeline-view authenticated-nodes`
on the coordinator. It backs up replaced files, rolls back on service restart
failure, and restarts only the task feed and authenticated web API. Build the
updated node package with `./build.sh`.

## Fast prerelease swarm experiments

[The five-machine simulator](docs/prerelease-simulation.md) calibrates node resources
and execution stages from each machine's authenticated API history, then runs
seeded cache-pressure and scheduling experiments with a virtual clock. Offline
runs need only a saved calibration. Reports compare round-robin and cache-aware
scheduling, expose cache accumulation/admission stalls, and include reproducible
seeds plus deliberate fault controls. No live jobs or cache changes are made.

## Audit caches across enrolled machines

Use the [read-only cache audit](docs/cache-audit.md) to check every machine's
JSwarm APIs for shared-cache counts, measured RAM, admission headroom, pending
cache preparation, and retained package/workspace data. Repeat samples or a saved
previous report expose growth without assuming that retained data is unowned.

## Clean terminal shared RAM caches

The [cache cleanup API and script](docs/cache-cleanup.md) provide signed
`resource.caches.inspect` and `resource.caches.cleanup` operations. The script
checks controller queue ownership, defaults to a preview, and supports `--apply`
and an optional admission-headroom target. Nodes preserve attached consumers and
active transfers and refuse stale owner identities. Updated nodes advertise
`cache-cleanup-v1`; standard publication and node update are required.

Node source is volume-mounted; code-only updates reuse the dependency image.
The standard publisher pins Git commits and the fleet updater fetches them through
its signed node API before activating the verified runtime. See
[Git source deployment](docs/node-source-deployment.md) for the update and migration steps.


Source-workspace web responses disable storage and revalidation with
`Cache-Control: no-store, no-cache, max-age=0, must-revalidate`, `Pragma: no-cache`,
and `Expires: 0`. Static HTML automatically loads `/jswarm-live.js`. The browser
checks `/api/jswarm-web/source-revision` every 1.5 seconds while visible and reloads
after a changed public-source fingerprint stabilizes over two checks. Imported JS,
CSS, JSON, additions and deletions are included. The fingerprint endpoint returns
only a digest, follows the existing enrolled-client/origin checks, and excludes
private state and symlinks outside the two public asset trees. Read-only genome
selectors are restored after reload. Focused text entry delays reload until the
user leaves it. Background tabs check when visible again. Immutable bundled
releases do not inject a live watcher. An already-open page needs one refresh to
load the watcher after its first installation.
