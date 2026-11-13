# Private cached filesystem

The swarm node includes `filesystem.rpc`. The standard node publication embeds
`swarm_filesystem.py` and the existing `run_input_cache.py`; its checksummed
`private-filesystem` component installs the mount client and user service.
No SSH deployment or controller private signing key is used on workers.

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
with a 16 MiB cache; payload caching never falls back to worker disk. Origin disk
reads also benefit from the kernel page cache. Failed peers cool down for one
minute and do not become alternative durable owners.

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
cryptography packages. The node restores that gateway on startup. The mount is owner-only. Docker consumers must run
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
