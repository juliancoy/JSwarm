# Read-only fleet cache audit

Run from the JSwarm directory:

```sh
python3 -m scripts.swarm_cache_audit --controller compai.local \
  --samples 2 --interval 30 \
  --output .local-reports/cache-audit/latest.json
```

The script discovers every enrolled machine and reads its authenticated
`status`, `resource.capacity`, and `resource.caches` APIs. It never submits jobs,
invokes the command API, removes data, or changes worker policy. `--controller`
uses existing host-pinned access only to bootstrap those API requests where the
controller signing key is installed. When running on that controller, omit the
option. Credentials and enrollment configuration are not exported.

The console table shows shared-cache worker counts, configured memory
reservations, fresh sampled raw RAM, available admission RAM, and other Docker
workers. “Other workers” is deliberately broader than compute: it may include
preparation or service workers. Native job and preparation counts, worker group
counts, pending/unverified caches, verified payload sizes, repeated identical
payloads, package-cache sizes and retained-workspace observations are in JSON.

Reservations, sampled raw RAM, and logical verified payload bytes are distinct.
Stale or absent measurements print `?`, not zero. Package/workspace observations
come from the last node maintenance pass; their timestamps are retained. Cache
inventories can be slow on machines with many owners; `--inventory-timeout 180`
increases only that operation's deadline. Other API attempts use `--timeout`.
Normal safe-read retries in AgentClient still apply. A failed endpoint preserves
successful observations and is explicitly reported as unavailable.

Repeat samples include count, reservation, RAM, payload and admission-headroom
deltas. You can also compare separate runs:

```sh
python3 -m scripts.swarm_cache_audit --controller compai.local \
  --previous .local-reports/cache-audit/latest.json \
  --output .local-reports/cache-audit/next.json
```

Only matching physical machine identities and older observations are compared.
Use `--hosts` to select a subset; the default includes the entire enrolled fleet.
Exit status is 0 for a complete unflagged latest sample, 1 for observed warnings,
and 2 for partial/unavailable API observations. Completed samples are saved even
if a later sample fails. JSON artifacts are ignored by Git.

Low admission headroom, cache pressure without other workers, increasing cache
counts with falling headroom, and workspace/storage budget violations are flags
for investigation. Neither a cache count nor short-term growth proves a leak.
Queued/preparing jobs can legitimately retain caches before any Docker consumer
attaches. These APIs expose neither durable queue ownership nor Docker consumer
bindings, so the script cannot declare caches unnecessary or safe to remove.
Repeated identical bytes across image variants are potential duplication, not a
recommendation to bypass pinned-image validation.

The current cache inventory covers running owners. Stopped/orphan volumes,
arbitrary staging directories in `/dev/shm`, and Docker image/layer caches are
not exposed by these endpoints and cannot be fully audited here. No fallback
remote shell commands are used to fill those gaps.
