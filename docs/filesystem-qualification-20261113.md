# Filesystem qualification — 2026-11-13 EST

Latest result (02:23 UTC): the identified filesystem compatibility blockers are
resolved and bounded containerized pipeline operations pass on release 37.
Whole-genome capacity, reference alignment and throughput admission remain a
separate requirement; no full genome run was launched.

The initial results below are retained as history. See the follow-up at the end.

Executed 2026-11-14 01:59–02:01 UTC against the deployed filesystem, using SSH
and mDNS for remote host execution. No original sequencing data was modified.
The only restart was the exact current Latitude filesystem capsule, identified
by its owner-home label and active gateway script. Docker daemons and scientific
workers were not restarted. This tests a service restart, not a machine reboot,
power loss, origin failover, or concurrent writer recovery.

| Check | Result |
| --- | --- |
| CompAI writes 1 MiB; Latitude, Inspiron, MiniPC, Codecollective read matching SHA-256 | Pass |
| Client local RAM hits | Pass: four blocks |
| Remote RAM hits | Four hits on subsequent check; first check had zero and failed |
| MiniPC overwrite visible on CompAI, including a previously warmed client | Pass |
| Latitude backing file matches acknowledged write | Pass |
| Latitude filesystem capsule restart; durable and mounted reads still match | Pass |
| Host copies real 6,866,727-byte pilot BAM with matching checksum | Pass |
| Host BAM sort, index, sequential and indexed reads | Pass: 1,878 records, 1,555 mapped records fetched |
| Gzip output round trip | Pass |
| Manifest file fsync, atomic replace, directory fsync | Pass individually |
| Actual application manifest sequence including chmod(0600) | Fail: ENOSYS (38) at chmod |
| Docker bind of owner FUSE mount | Fail: daemon stat permission denied before worker starts |
| Read-only mmap | Fail: ENODEV (19) |

Remote cache performance is not reliably qualified: the cold attempt fetched
from origin and its fresh second client recorded no peer hits; the subsequent
check recorded four peer hits. The failure remains in the log. Availability of
correct origin reads does not establish consistent remote-cache utilization.

The first host BAM test incorrectly assumed the contig was named `chr22`; the
artifact uses a different reference identifier. It failed after copy, checksum,
index and sequential reads. The corrected test selects the BAM's first reference
and passes sort, indexing and fetch. Only that affected test was repeated.
Pysam 0.23.3 ran in an isolated host virtual environment under /dev/shm; no VM.
The Docker rejection was retained separately, not counted as a host BAM failure.

## Pipeline release blockers

1. Provide narrowly scoped daemon/worker access to the FUSE mount and test a real
   container bind. Owner host access alone does not establish Docker usability.
2. Implement the permission operation used by atomic JSON publication, or change
   the application contract with equivalent privacy guarantees. Do not report
   chmod success without enforcing it.
3. Retain sequential/seek IO or qualify a deliberate mmap implementation before
   permitting mmap-dependent tools. BAM sort/index already works without mmap.
4. Diagnose intermittent remote cache misses and test cold-start cache warming.
5. The current epigenetic projection stores every original read signature and
   every site in Python dictionaries. Bounded sharding/aggregation and full
   reference alignment resource admission are still required for genome scale.
6. This run does not resolve earlier standard deployment receipt/startup issues,
   FUSE upgrade namespace recovery, multi-writer POSIX semantics, or machine
   restart durability. Full pipeline admission must remain blocked.

Latitude currently has about 1.1 TiB free and /mnt/DNA about 315 GiB free.
These are observed capacities, not a full-run allocation or quota reservation.
The earlier scientific pilot remains 3,000 sampled reads aligned against
chromosome 22; it is not a whole-genome epigenetic analysis.

## Evidence and reproduction

Adjacent log: `tests/filesystem-test-runs.jsonl`. Application tests are in
`../GenomeHarbor/tests/epigenetic-test-runs.jsonl`. Each run records UTC date,
command, result, and relevant source SHA-256 fingerprints. The application log
also records exact executable code. The original failed checks are retained.

Audit root: `/mnt/DNA/migrations/latitude-epigenetic-storage-20261113/`:

- `qualify_fleet_filesystem.py` and `fleet-filesystem-qualification-20261114T015914Z.json` / `20261114T020016Z.json`.
- `qualify_pipeline_filesystem.py`: failed actual Docker bind.
- `qualify_pipeline_host.py`: host operations, including chmod and mmap failures.
- `qualify_pipeline_bam.py`: corrected BAM-only test.
- `pipeline-filesystem-qualification-*.json` and `manifest-operation-qualification.json`.

Failed and application fixtures are retained only under uniquely named
`~/SwarmDNA/workbench/*qualification-*` directories for diagnosis. Successful
fleet fixture was removed. Temporary host environment remains in
`/dev/shm/epigenetic-qualification-venv` and disappears on reboot. No full pipeline
or bulk sequencing transfer was started by this qualification.

## Follow-up: blockers repaired and deployed (02:19–02:24 UTC)

Validated source: JSwarm `87f6c06`, GenomeHarbor `ba616ea`. Release generation 37,
runtime `19fa7940b8d32ff94ae587a6875d6518330b249db431b5fd7453346ae99f1d29`.
The standard frozen-source gate passed **542 tests, 13 skipped, two subtests**
in 159.79 seconds. A previous attempt lacked pytest in the updater environment;
the host test environment was used. An earlier gate was interrupted after a
superseding FUSE fix; its Docker-start timeout and partial results are retained.
Cold cache container provisioning now has a bounded 180-second startup budget.

The same validated node installer was run over explicitly authorized SSH for
recovery from API status timeouts. It verified enrolled identity, fetched pinned
Git sources and matched the published code before staging. All five current
release pointers and new filesystem gateway hashes matched. The published
private-filesystem component then installed through the standard component
mechanism using its SSH plan. All five mounts passed host stat/list checks.
The standard continuous updater was restored from the committed managed source
and independently reported all five nodes current and ready (0.14–0.45 seconds).

| Requalification | Result |
| --- | --- |
| Real system FUSE2 and FUSE3 chmod enforcement and Docker owner bind | Pass |
| Four remote owner mounts: write/fsync/chmod/rename and system-Docker read | Pass, all four |
| Five-node matching reads and MiniPC overwrite invalidation | Pass |
| Local RAM and remote RAM hits | Pass: four blocks each; zero peer errors/rejected admissions |
| Latitude capsule restart, acknowledged data retained and readable | Pass |
| Actual container BAM copy/checksum, sort/index, indexed fetch | Pass: 1,878 records, 6,866,727 bytes |
| Actual container atomic manifest with chmod, replace and directory fsync | Pass |
| Actual container gzip output | Pass |
| mmap of fully verified immutable RAM snapshot | Pass |
| Externally sorted modification projection on network scratch | Pass: exact prior pilot table and counts |

The writable shared mount continues to reject direct mmap. Its versioned read
coherence is preserved through direct IO; mmap consumers use the explicit private
RAM snapshot API with a published digest, unchanged-version check and capacity
admission. Group/world grants remain forbidden. Docker's daemon receives
root-only FUSE access; consumers run with an identity matching the mount owner.

Cache admission now reports whether a peer actually retained a block. The client
tries another peer when admission fails, records errors/misses/rejections, allows
cold RPC startup, and retries failed peers after two seconds. This run establishes
the corrected bounded test behavior, not an indefinite cache reliability soak.

Projection read identity checks and site aggregation now use bounded external
merge sorting (8 MiB encoded chunks, fan-in 32), with sequential output-adjacent
scratch. All nine focused scientific/cache/sort tests passed. Reusing the real
completed pilot alignment produced exactly the prior 12,593-site table with
12,918 calls and 1,000 verified primary reads; decompressed SHA-256
`7fd402b1967513d4baff29c0dcf43738725b7e7cec5c1d8f605d674aeec6d2c8`.
No new alignment or complete epigenetic-state inference was represented by this
regression check. Large scratch still requires storage admission or bounded shards.

Additional audit files under the same audit root:
`blocker-fixes-publication-final.log`, `recover_standard_release_ssh.json`,
`blocker-fixes-filesystem-deployment.json`,
`fleet-filesystem-qualification-20261114T022218Z.json`,
`pipeline-filesystem-qualification-20261114T022219Z.json`,
`qualify_projection_streaming.json`, and `qualify_node_mount_access.json`.
The FUSE3 Docker check initially exceeded a 30-second container startup timeout;
it passed with a 120-second startup budget. The exact abandoned fixture container
was removed; unrelated workers were preserved.

Inspiron's rootless engine additionally passed a read-only bind/read as namespace
UID 0 (mapped host owner), with all capabilities dropped and no-new-privileges.
See `rootless-fuse-qualification.json`. System-Docker reads used host UID 1000.
