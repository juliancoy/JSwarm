# Private cached filesystem

The swarm node includes `filesystem.rpc`. The standard node publication embeds
`swarm_filesystem.py` and the existing `run_input_cache.py`; its checksummed
`private-filesystem` component installs the mount client and user service.
The default transport is the signed node command API. An explicitly configured
`"transport": "ssh"` uses enrolled mDNS hostnames and the owner account, with
optional `ssh_users` overrides. Controller private signing keys stay on the controller.

A private controller plan at `~/.config/map-master/filesystem-plan.json` selects
one namespace, its enrolled origin, an export inside the origin owner's `~/DNA`,
a 32-byte hexadecimal namespace secret, and a minimum-free-space reserve.
The standard rolling updater installs the corresponding owner-only
`~/.config/lifetech-launcher/filesystem.json` and enables
`swarm-filesystem.service`. With no plan, this component does nothing.
Offline nodes remain pending and are retried by the standard updater.

FUSE is enabled by default for a configured namespace. Set `"fuse": false` in
the controller plan to disable the mount service and skip its dependencies;
the node filesystem API, local gateway and RAM caches remain available. Set it
back to `true` (or omit the setting) to restore the mount on standard deployment.
The adapter uses the system FUSE library. `"fuse_version": "auto"` (the default)
prefers FUSE3 and falls back to FUSE2. Use `"2"` or `"3"` to select an installed
major version explicitly. No private libfuse shared library is distributed.

The configured mount is `~/SwarmDNA`. For epigenetics its durable backing is
Latitude's `~/DNA/epigenetics`. Original DNA-drive files are not automatically
moved or deleted when the mount is installed.

Reads consult origin metadata to establish the current inode/size/time version,
then use a bounded local RAM cache, remote RAM peers, and finally the origin.
Blocks are 256 KiB. Cache identities include namespace, path, origin version and
block offset. The existing block cache checks its stored payload digest and
uses locks and LRU eviction. Each mount and each node filesystem capsule starts
with a 16 MiB cache; payload caching never falls back to worker disk. The
owner capsule uses its existing private `/tmp` tmpfs because its host `/dev`
view is read-only. Origin disk
reads also benefit from the kernel page cache. Failed peers cool down for two
seconds and do not become alternative durable owners.

Writes are write-through: the origin serializes namespace mutations, checks the
caller's file version and available-space reserve, performs the positional write,
and fsyncs before acknowledging it. Newly written complete blocks can enter the
local RAM cache only after that acknowledgement. Old cache entries become
unreachable through their versioned keys. RAM eviction and peer restarts cannot
lose acknowledged writes. Writes are not automatically replayed after ambiguous
transport failure; callers must inspect the origin before retrying.

Filesystem calls use the existing encrypted node protocol plus a namespace MAC,
fresh timestamp and replay nonce. The namespace credential grants that namespace
read/write access to its participating owner nodes; it is not a per-file ACL.
Changing the secret revokes old grants. The owner capsule supplies the existing
home filesystem view; the replaceable API agent does not gain an additional host
root mount. Path traversal and symlinks are rejected using directory-relative
opens with `O_NOFOLLOW`.

The mount uses direct I/O with kernel metadata caching disabled, so the custom
versioned RAM tiers coordinate reads. The host needs `/dev/fuse`, libfuse2 or libfuse3 and
`fusermount`/`fusermount3`; deployment installs only the pure Python mfusepy adapter in an
owner-private directory. A private local Unix socket forwards network calls
through the node capsule, so older host Python versions do not need the node's
cryptography packages. The node restores that gateway on startup. The mount admits the owner and root by default (`allow_root`, never
`allow_other`). Installation enables the system FUSE `user_allow_other` policy
prerequisite through the existing system Docker authority; this lets mount owners
request sharing, while this mount selects root-only sharing and keeps kernel
permission checks. Set `"allow_root": false` to keep a host-only mount without
changing that prerequisite. Docker consumers must run
with a compatible owner identity and explicitly bind the mount as appropriate.
The RAM-only worker policy accepts this enrolled FUSE mount, while continuing
to reject ordinary disk bind mounts. Owner capsules receive host mount events
through a slave bind; they cannot propagate mounts back to the host.

This initial interface supports regular-file random reads/writes, creation,
truncation, directories, removal and rename. It does not provide atomic append,
POSIX distributed locks, symlink creation, hard links, or open-file lifetime
across unlink/rename. Applications requiring those semantics must not use it.
Latitude must be reachable to validate versions and commit writes; RAM caches
do not provide disconnected write availability. The existing 1 TiB placement
ledger is a logical allocation, not a kernel filesystem quota; writes enforce
the configured free-space reserve.

Validation is recorded in `tests/filesystem-test-runs.jsonl` and the standard
publication test log. Live fleet qualification must verify cross-node writes,
local and peer cache hits, overwrite invalidation, and persistence after a
filesystem-capsule restart before large scientific workloads use the mount.

## Pipeline compatibility and bounded scratch

Permission changes are enforced at the origin and fsynced. Only owner permission
bits are allowed: group/world grants and special bits are rejected. This permits
the application atomic JSON protocol (file fsync, chmod 0600, replace, directory
fsync) without pretending permission operations succeeded.

Direct IO intentionally prevents mmap of mutable distributed files. For tools
requiring mmap, `immutable_ram_snapshot(client, path, published_sha256)` admits a
size-bounded private tmpfs snapshot, verifies its complete digest and unchanged
origin version, exposes a read-only file, and removes it on context exit. Its
default maximum is 512 MiB with a 128 MiB free-RAM reserve; larger files require
explicit admission or smaller shards. No disk fallback or stale kernel page-cache
coherence assumption is introduced. Current samtools/pysam operations use seek IO
and do not require mmap of the shared mount.

Cache puts report actual admission. A full peer is skipped in favor of another
peer; origin read availability does not count as a successful cache fill. Client
counters distinguish misses, errors, and rejected admissions. The backend allows cold owner-capsule startup, while FUSE clients bound optional
peer probes and admissions to a shared three-second budget per block fetch before
falling back to the origin. Failed peers are retried after two
seconds rather than bypassed for a minute.

HarborGenetics projection externally sorts read fingerprints and modification-call
rows instead of retaining every read and genomic site in RAM. It uses sequential
scratch alongside its output (at most 8 MiB of encoded rows per run and 32 merge
readers). This scratch must be capacity-admitted: use Latitude-backed output for
large work and bounded shards on RAM-only workers. No SQLite locks or mmap are
required. Full-genome reference indexing, shard sizing, capacity and throughput
still require workload-specific admission; a bounded pilot is not that analysis.

For rootless Docker, UID 0 inside the user namespace maps to the enrolled host
owner. Such consumers use `container_user="0:0"` with all capabilities dropped,
no-new-privileges and a read-only root; using UID 1000 inside that namespace maps
to a different host UID. Inspiron's existing rootless engine passed a real
read-only bind/read under this mapped owner identity. System-Docker consumers
use the actual host owner UID (1000 in this fleet).
