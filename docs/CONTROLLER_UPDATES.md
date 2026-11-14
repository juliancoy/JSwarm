# Alignment controller updates

Follow [the standard no-drain replacement procedure](REPLACEMENT.md).
`python run.py --vulkan-alignment deploy-controller` builds and checks an
immutable controller image, fences fresh claims using the existing SQLite
epoch, and starts the successor before removing its predecessor.

Accepted node-owned jobs continue with their existing Docker IDs and durable
contracts. The successor recovers them through the node journal. Legacy
controller-owned FIFO jobs use explicit bounded checkpoint/replay: verified
results survive, unfinished legacy workers are confirmed absent before their
same shards become claimable again. Deployment never waits for job counts to
reach zero. This legacy migration can repeat unfinished computation; it does
not provide zero-restart execution for controller-owned pipes.

Rollout records under `recall/venter/rollouts/` identify preserved node jobs,
verified receipts and replayed legacy shards. Retirement services implement
immediate replacement cleanup, not a drain loop. Unknown worker identity or
uncertain cleanup leaves the claim held for recovery. Only the obsolete
controller's own claims are considered; unrelated controller cohorts are not
signalled or reset.

## Reference memory measurements

Use `python -m scripts.alignment_memory_report --root WGS_ROOT --output REPORT`
to group completed BWA cgroup peaks by host, backend and reference layout, and
to read live shared cache owner cgroup current/peak memory. This is read-only.

A node RAM cache has one admitted reference owner; consumers reserve their
private buffers and scratch. FS-CACHE/file-mapped reference peaks can include
reference pages charged to a consumer and cannot be treated as private memory.
The report never changes limits. Experimental Vulkan mapper memory calibration
and quarter-CPU worker reservations do not calibrate BWA containers.

Pipeline execution labels distinguish BWA GPU alignment with CPU seeding,
shared BWA RAM versus file mappings, and FS-CACHE reference transport. A
transport label does not establish cache hits or shared VRAM.

Queue priorities are integers from -1000 to 1000; higher values run first and
ordinary work defaults to 0. HarborGenetics node qualification jobs request 100.
Both coordinator claims and node admission use priority order, with FIFO ties.
Jobs that do not fit remain queued while other eligible work can proceed.
Priority does not interrupt running jobs or override resource limits, GPU locks,
worker pause policy, or work admitted through legacy controllers.

For an already accepted node job, use
`StreamClient(host).prioritize(job_id, 100)`. This durable scheduling metadata
survives launcher reloads and does not change the immutable execution contract.
Coordinator submissions expose `SubmissionQueue.prioritize(id, priority)`.
The node's `stream.status` and `stream.list` replies include current priority.


CPU admission defaults to `timeshare`. Host utilization and CPU PSI remain
telemetry and do not veto new jobs, including at 100% CPU utilization. Aggregate
CPU quotas and unstarted CPU promises do not consume admission slots. Each job
must still request no more CPUs than the node has, and its Docker CPU quota
remains enforced. Linux time-shares runnable work. RAM, PID, device exclusivity,
VRAM and owner-pause checks continue to gate admission. Operators can explicitly
restore the previous policy with `SWARM_CPU_ADMISSION=pressure` in the node.
`available_cpus` in timeshare mode means the maximum per-job CPU request, not
idle cores. `reserved_cpus` and `cpu_measured_busy` show quotas and actual load.

Memory admission starts with actual `MemAvailable` (bounded by any visible shared
cgroup), keeps 5% of total RAM or 256 MiB for the system, and protects worker
growth. A newly admitted or unmeasured worker retains its full RAM commitment.
For measured workers after ten seconds of sampling, growth protection covers
25% of current working set or 64 MiB, plus observed peak above current usage and
twice the most recent positive growth. Each allowance is bounded by that
container's memory limit. Measurements older than ten seconds cannot lend RAM.
Memory PSI `some` at 1% or `full` at 0.1%, or I/O PSI `full` at 20%, stops new
memory admission. Missing pressure measurements use the previous strict policy.
PID limits remain strict. Running jobs are not stopped by pressure admission.

`resource.capacity` reports `reserved_cpus` (quotas), `charged_cpus`,
`cpu_measured_busy`, `cpu_startup_commitment`, `available_cpus`,
`cpu_admission_mode`, `memory_admission_mode`, `memory_growth_mb`,
`memory_safety_mb`, and kernel pressure measurements. `available_mb` is admission
headroom after safety/growth allowances, not raw host free RAM. Existing Docker
statistics supply working sets, peaks, growth and CPU deltas; there is no added
Docker polling loop. Memory startup commitments are counted under the same pool
lock, so simultaneous launchers cannot all spend the same measured free RAM.

Prelaunch permits expire after 60 seconds without an acquire/renew refresh,
instead of five minutes. Generic launch clients reacquire immediately before
starting Docker after mount/device preparation. Running Docker workers never
expire because a permit or controller disappears. Physical resource arbitration
also honors job priorities, including changes to pending node jobs.


GPU execution uses measured resource admission, without a fixed job-count cap.
Legacy `max_concurrency` fields remain readable for immutable recipe compatibility
but do not limit execution. The previous exclusive device lock and the former
32-slot ceiling are not admission gates. Controller GPU feeder capacity follows
physical host CPU capacity rather than qualification-profile job counts; actual
execution still passes current host-RAM, PID and GPU-memory checks.

Concurrent GPU starts serialize a short admission transaction, recording private
VRAM commitments in process-owned locks. Admission keeps the measured free-VRAM
floor plus startup commitments; it refuses unknown or stale driver measurements.
Locks release when GPU compute completes or the worker exits, and unlocked stale
records consume no memory commitment. Existing legacy lock holders do not impose
a count cap on new jobs. Job labels identify `gpu-admission=pressure`.

Reference mappings remain shared and read-only. Vulkan contexts, private GPU
buffers and scratch remain per process; concurrent contexts do not imply a shared
full-genome VRAM index. Previous CompAI qualification compared four concurrent
jobs with CPU SAM semantics; that evidence is not a four-job scheduling limit.

AMD Vulkan alignment uses an owner-installed DRM binding rather than an NVIDIA
UUID/runtime. It pins the render node, PCI identity, render group, Radeon ICD,
and SHA-256 digests of the executable/shader runtime. Workers verify those
artifacts and the physical PCI device before executing. AMD admission reads
`mem_info_vram_total` and `mem_info_vram_used` from the selected DRM device;
it does not substitute host `MemAvailable` for reserved VRAM. CPU staging memory
and the mapped reference remain subject to normal host memory admission.
MiniPC reports 8 GiB reserved VRAM and about 8 GiB usable host RAM separately.
GPU lanes are selected from machine-bound qualified profiles, rather than a
CompAI-only host name. MiniPC's reference remains shared through FS-CACHE.


JSwarm owns dynamic GPU inventory through the signed `resource.gpus` operation
and optional `resource.capacity` argument `include_gpus: true`. Inventory reports
host `MemTotal`/`MemAvailable`, GPU-local total/used/free VRAM, and host-backed GTT
as separate fields. Unknown driver counters stay unknown and cannot justify GPU
admission. GTT is not added to VRAM, and GPU VRAM is not added to CPU RAM.

Physical DRM identities bind machine, PCI address, vendor and model. The node
refreshes measurements, detects render node/group changes, and counts existing
GPU worker commitments and older exclusive workers. Render group discovery uses
a short isolated read-only metadata container when the agent's own device
namespace hides `/dev/dri`; it has no GPU device permission. Application profiles
qualify the algorithm for a physical GPU; they do not replace device discovery.
Both installed node recipes and application adapters resolve qualified bindings
against this node inventory before admitting new work. Stale inventory, unknown
VRAM, changed identity, unavailable permissions and occupied device slots are
explicit admission failures. Existing workers reattach before any new admission
check. Worker-side locks and fresh driver checks still serialize actual starts.

Intel integrated GPUs use an explicit `shared-system-memory` binding. A bounded
Vulkan metadata probe verifies the physical device is integrated and matches the
PCI vendor/model. Intel discrete GPUs and unknown devices cannot use this path.
The node reports fresh kernel host-RAM headroom; dedicated VRAM fields remain
unknown. The GPU workspace is added to the worker's private host-RAM admission
budget and Docker memory limits. It is never added to host memory capacity.
Integrated GPU bindings require node inventory and retain the same runtime hashes,
physical identity checks and atomic per-device memory commitments as other GPUs.
Discovery alone does not enable an alignment profile: correctness and aggregate
CPU-plus-GPU throughput must be qualified for the workload first.

GPU job measurements remain separate from CPU job measurements. `resource.capacity`
reports fresh measured host-memory usage and coverage per `job_sinks.cpu` and
`job_sinks.gpu`; missing or stale observations are excluded from measured totals,
with coverage counts showing incomplete sampling. These are host-RAM counters,
including GPU feeder buffers, not VRAM. `resource.gpus` reports physical-device
VRAM separately. Memory profiles never combine CPU and GPU samples or different
GPU UUIDs; legacy samples without a GPU UUID stay separate from identified GPUs.
The recorded execution class takes precedence over a queue lane when present.

Memory admission correlates moderate memory/I/O PSI with low measured RAM
headroom after startup, growth and system margins. I/O stalls alone do not zero
an otherwise roomy RAM budget. Severe memory stalls still pause new admission;
CPU timesharing and atomic startup commitments remain in force.

Public reference cache warm-up is an owner-installed immutable maintenance recipe
submitted by the standard pager deployment component. The node journal owns its
low-priority execution, retry and receipt; the submitting controller may exit.
It validates the complete artifact SHA-256 and reads into shared reclaimable
kernel pages, without pinning a private copy. Small-RAM nodes retain demand
paging. The request separates `memory_mb` (mapping ceiling) from
`memory_reservation_mb` (private startup/admission commitment). Mounted maintenance
recipes may omit input streams; clients cannot introduce arbitrary mounts.

GPU runtime bundles are installed by `scripts.swarm_gpu_runtime` through the
signed enrolled-node channel. The workload declares immutable source-image paths
and qualified SHA-256 digests. Installation stages and verifies an atomic,
content-addressed directory under the node owner's `gpu-runtimes` state; existing
bundles are reverified rather than modified. Genomics deployment calls this via
`run.py --vulkan-alignment prepare-nodes` and before controller deployment.
`run.py --vulkan-alignment reconcile` retries retirement of stopped legacy
controllers; a failed node's cleanup does not abort healthy nodes' reconciliation.

The generic `swarm_runtime.run_job` API accepts an optional
`gpu_compute_complete_file` beneath `/work`. An owner-installed command may
publish this marker only after every GPU-using subprocess has exited and must
never use the device afterward. The admission supervisor releases its device
and slot locks while retaining the container, RAM outputs and host-resource
lease for verified export. Missing markers retain admission until process exit;
exit status is preserved. The legacy
RAM alignment adapter publishes the marker after the successful BWA/sort
pipeline. Node-owned recipes retain their existing `compute_complete` progress
protocol and durable GPU-release receipt.

Release publication freezes checkout sources before running host tests, then
publishes that exact checked snapshot through the existing deployment lock and
immutable release format. Ignored runtime data is excluded. Independent edits
in the shared workspace cannot change the artifact halfway through validation.

NVIDIA inventory probes use unique container names and always request cleanup,
including on CLI timeout. A leftover probe cannot block subsequent allocation
measurements through a fixed-name collision. Unknown measurements still decline
new GPU admission.

Owner command cleanup now cancels the remote exec process group before closing
its Docker CLI pipe. A per-command random token and wrapper process start time
bind cancellation to the original process; reused PIDs are not signalled.
Completed wrappers remove their control records. The owner helper uses a new
runtime identity and bounded 1 GiB/no-swap memory and 1,024 PID limits to support
concurrent feeder commands and their supervisors. Existing older helper limits
are upgraded on attachment. This prevents abandoned readers from exhausting the
helper and starving otherwise admissible GPU work.

A checksum-bound DRM runtime may select `runtime_mode: image-native` for the
Vulkan follow-up library and shader. This retains the immutable container's
Python, scientific tools and Mesa dependencies instead of mounting the host
`/usr`. The node still resolves the physical render device/group and memory
domain, verifies the private runtime bundle, and charges integrated GPU buffers
to host RAM. Legacy BWA host-runtime bindings keep their existing behavior.

### Verify a rebuild from committed sources

Keep Organization, JSwarm and HarborGenetics as adjacent checkouts in the documented
workspace layout. After committing a change, run from Organization:

```
PYTHONPATH=JSwarm/swarm-control/server:JSwarm:. /home/julian/global/bin/python \
  JSwarm/scripts/verify_release_sources.py --tests --output /tmp/swarm-source-verification.json
```

This exports each repository's HEAD into an isolated temporary workspace, imports
the dispatch, recovery and dashboard code, rebuilds the sealed launcher input,
and runs focused host/local Docker regression checks. Untracked files and working
copy edits cannot satisfy a missing dependency. The output records all three
commits and release source hashes; it contains no job payload or credentials.
Scientific images, enrolled identities, route qualifications and verified results
remain separate operator state; this check does not provision or change them.
Use the existing `deploy_swarm_inputs --publish` and rolling updater for launcher
releases and `genomics_followup_controller --deploy-orchestration` for a controller
handoff. A controller service restart alone keeps its pinned release.

Worker-owned execution receipts retain cgroup v2 CPU totals and lifetime RAM peaks
before normal exit, including failed commands. The journal validates the attempt
identity and retains these counters before Docker cleanup. RAM current is sampled
after the command exits; the lifetime peak includes file cache and shared memory.
Do not add file and shared-memory counters together. Receipt flush and teardown
are outside the measurement. SIGKILL/OOM can prevent the receipt: last observed
counters survive journal restart as `sampled-partial`, or coverage is `unavailable`
when no sample exists. Historical counters cannot be reconstructed.

RAM-cache pressure eviction requires explicit candidate keys. An unattached volume
may belong to a prepared queue attempt waiting for admission. Candidate eviction
still checks attached containers and live readers; normal owner cleanup after
verified completion remains available. When no candidates are identified, new
work waits for memory rather than evicting uncertain inputs. The follow-up release
seals this cache policy alongside its runtime.
