# Cache locality and direct reference transfers

`swarm_cache_topology.py` reads the existing signed `status` and
`resource.caches` APIs in the background. Inventory is advisory, checked against
the enrolled machine identity, refreshed every 60 seconds after each completed observation, and ignored after
180 seconds. Slow or offline peers do not block dispatch.

The genomics adapter matches reference provenance and contigs to the existing
`shared-objects` derivation metadata. A worker receives affinity only for a
complete immutable object set in a verified cache owner. Placement preserves
job priority, GPU eligibility, fair rounds, and live CPU/RAM admission. Affinity
orders equal-priority queued jobs; it never moves an accepted job.

Destination RAM cache hits do not materialize another origin copy. On a miss,
up to two warm enrolled peers are tried before the origin. Peer choice considers
observed transfer cost and in-flight source load; failed routes cool down for
60 seconds. The origin can use the same direct path, with the existing signed
command stream as a compatibility fallback.

`swarm_cache_transfer.py` starts bounded, one-use streams through the existing
signed owner-command API. Payload bytes travel directly over TLS between the
source and destination owners, using existing kernel multipath configuration
where available and falling back to TCP where unsupported. Each transfer has an ephemeral pinned certificate
and receiver token; no controller signing key leaves the coordinator. Source
keys exist only in RAM scratch. The source owner and immutable seal are checked
again immediately before reading. The destination checks every filename, byte
count, and SHA-256 before the ordinary cache seal can publish it. Payloads never
use a temporary disk archive or a public export.

Reference transport receipts record cache-hit status, loaded bytes, selected
source, and transport. Topology observations and throughput estimates are
rebuildable hints, not ownership records. Cache retention and cleanup continue
to use the existing job lifecycle independently.

The standard orchestration snapshot pins the topology, transfer, and queue
modules with the application adapter. A scientific staging qualification remains
required when the adapter changes. The executor image and scientific recipe
binding are unchanged.
