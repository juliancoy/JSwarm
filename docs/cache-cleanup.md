# Shared RAM cache cleanup

Updated nodes advertise `node-cache-v1`. Their singleton cache is controlled by
signed `resource.cache.begin`, `seal`, `check`, `pin`, `renew`, `release`,
`inventory`, `drop` and `prune` operations. Every mutation validates exact object
keys; loading and reader pins expire after 120 seconds and renew every 30 seconds.
The node also checks all Docker consumers, including stopped containers, before
eviction. Queued or legacy controller jobs do not block managed cache cleanup.
`prune {}` removes every currently unpinned object; `prune {"required_mb": N}`
selects idle objects in LRU order until their recorded allocation reaches N.
That allocation is a selection budget, not proof of physical RAM reclaimed.
Callers recheck actual admission headroom after each pass.

## Health and counters

`cache-metrics-v1` nodes return `metrics` and `control_metrics` from
`resource.cache.inventory`; the latest sample also appears in `status.cache_summary`.
Cache counters live beside the objects in RAM and survive agent replacement.
They reset when the keeper's RAM volume is recreated. Control-method timings
reset with the agent; both sections include their `since` timestamp.

- `begin_hits`, `begin_misses`, `load_starts`, `load_waits`, `capacity_waits`,
  `loads_sealed`, `bytes_loaded`, and `reuse_bytes` distinguish lookup attempts,
  actual loads and reused logical payload bytes. A waiting/rejected begin is a
  miss, not an additional completed load. `check_hits`/`check_misses` count seal
  checks separately so a warm begin followed by a check is not counted twice.
- `evictions`, `evicted_allocated_mb`, and `evictions_*` distinguish capacity
  pressure, explicit cleanup and expired loading objects.
- `peer_bytes_sent`/`peer_bytes_received` and `peer_transfers_*` count verified
  direct TLS payloads reported by the transfer helper. The signed
  `resource.cache.transfer` mutation takes `key`, `transfer_id` (64 hex),
  `direction` (`sent` or `received`), `bytes`, and the normal `machine` identity.
  Duplicate event IDs are ignored within a bounded 2,048-event replay window.
  Missing telemetry acknowledgements never cause payload transfers to repeat.
- `operations` exposes call counts, total and maximum seconds. Keeper timings
  cover metadata execution; `control_metrics.operations` also covers manager
  lock wait and Docker commands, with a separate error count.

An expired loading pin no longer strands its object. Inventories and the
agent's 30-second maintenance sweep remove abandoned loading entries only when
no pins, Docker mounts or in-flight keeper execs protect them. Ready objects
retain ordinary bounded LRU behaviour; near-full cache allocation alone is not
evidence of thrashing. Budget-pressure tests verify that hot objects survive.

Cache inventories use effective host/shared-cgroup RAM limits without scanning
the admission pool. Growth still obtains the shared admission lock and uses
fresh worker observations. Helpers have one CPU of burst allowance even when
idle; the sleeping keeper does not consume that CPU continuously. Memory and
swap limits still move together, so container swap remains disabled. Warm hits
and unchanged inventories avoid repeated Docker updates and inventory execs.

Node `status.worker_inventory` reports the freshness/error of its background
Docker observation. Status does not wait for a stuck Docker inspection. A cold
or stale observation makes `ready` false; actual admission continues to use
synchronous, authoritative worker state. Running jobs are left intact.

The fleet cleanup script below handles managed objects and legacy owners in one
pass. Managed cleanup works without a controller queue file. Legacy objects still
require readable terminal ownership and retain the safeguards described below.

## Legacy per-object cleanup

JSwarm exposes controller-signed RPCs over the existing encrypted `/agent/v1`
transport. Updated nodes advertise `cache-cleanup-v1` in `status.capabilities`.
Both operations require the enrolled controller signature; ordinary peer access
cannot inspect or delete caches through them.

- `resource.caches.inspect` takes `{}` and returns `items` describing labeled
  cache owners, ownerless labeled tmpfs volumes, attached consumers (including
  stopped containers), active exec/transfer counts, and validated backing type.
- `resource.caches.cleanup` handles **one exact cache instance**. Required fields
  are `key` (64 hex characters), `owner_id` (the inspected 64-character Docker ID,
  or `null` for an ownerless volume), `machine` (the status machine fingerprint),
  `ownership: "terminal-only"`, and boolean `dry_run`.

Queue ownership lives on the controller. The controller must check all queue
namespaces immediately before calling cleanup; `ownership` is its authenticated
attestation of that decision, not a node-local queue scan. The node independently
validates its machine identity, exact owner instance, cache labels, expected
volume mount and tmpfs volume identity. A replacement loader, attached consumer
or active transfer prevents removal. Volume removal is never forced, so Docker's
reference check protects attachments that race with cleanup. An ownerless-volume
request cannot remove a newly created owner container.

`dry_run: true` performs the node checks without removal. Replies include
`removed`, `owner_removed` and `reason`. For example, a volume can become attached
after its old sleeping owner is removed; the reply then reports `owner_removed`
while leaving `removed` false. Unknown/failed mutation outcomes require fresh
inspection rather than blindly replaying a saved request. Cleanup requests are
not automatically retried by the client.

Use the controller-side script to make those ownership checks correctly. From
the JSwarm directory, preview all enrolled machines:

```sh
python3 -m scripts.swarm_cache_cleanup --controller compai.local \
  --output .local-reports/cache-cleanup/preview.json
```

Remove the entries still eligible at execution time:

```sh
python3 -m scripts.swarm_cache_cleanup --controller compai.local --apply \
  --output .local-reports/cache-cleanup/result.json
```

To stop each machine once admission headroom reaches 2 GiB, add
`--min-free-mb 2048`. It prefers objects with fewer recorded consumers and checks
actual headroom rather than assuming configured limits equal freed RAM. Without
that option, it removes all eligible terminal-only entries in the inspected set.
Use `--hosts` for a subset. The default includes every enrolled machine. Running
on the signing controller itself does not need `--controller`; the option uses
existing host-pinned access only to bootstrap the API orchestrator there.

The script checks the existing controller queue without creating a claims ledger
or altering job state. Missing, corrupt, unreadable, unfinished or unknown queue
ownership prevents removal. It rechecks eligibility before each deletion and
never applies a saved preview as an authorization manifest. Both running and
stopped attached consumers retain their volumes. Candidates created after the
inspection are outside that cleanup pass. JSON reports retain candidates,
skipped reasons, completed removals, errors and before/after worker/headroom
observations. No package cache, arbitrary staging directory, scientific result,
Docker image or global prune is included.

The script requires the new node capability and fails explicitly on older nodes;
there is no command-API or node-SSH fallback. Build with `./build.sh` and publish
and update through the standard validated node release procedure before fleet
use. The new API and controller-script tests are included in that release gate.

## Migrating legacy holders into the singleton

`python3 -m scripts.swarm_cache_migrate --output migration.json` previews the
migration on the signing controller. Add `--apply` to perform it. This is a
separate maintenance operation from terminal-only cleanup: it preserves unfinished
jobs instead of treating their old bindings as eviction permission.

Apply holds the standard deployment lock, stops `genomics-followup.service`,
and acquires the existing plan controller locks. It restores the service even
if migration fails. It scans every unfinished
queue namespace and the reusable-object lookup records. Needed legacy payloads
stream directly between Docker containers on the same node; the receiver and
managed-cache seal both verify their immutable sizes and SHA-256 checksums. RAM
budgets remain enforced, and the operation fails if retained objects exceed a
node's budget or a pending input has no verified source.

Verified target objects stay pinned during migration. Queue requests, reusable
lookup records, and active attempt transport manifests are rebound to the managed
volume subpaths without changing job state, attempt identity, or executor image.
A metadata backup is written next to the migration report. Only after durable
rebinding can a needed legacy holder be retired. Other unclaimed holders can be
retired first to provide migration headroom.

Retirement uses the enrolled signed command channel, validates the node identity,
exact Docker owner ID, cache labels, tmpfs volume and expected mount, and refuses
running or stopped readers and active execs. Queue writers are fenced during each
node's retirement batch. No payload files are staged to disk and no global Docker
prune runs. Interrupted migrations can be rerun: legacy sources remain until the
verified replacement is published, and already sealed targets are reused.
