# Filesystem qualification — 2026-11-13 EST

Verdict: bounded host filesystem operations qualify; the full containerized
 epigenetic pipeline does not qualify for production.

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
