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
ordinary work defaults to 0. GenomeHarbor node qualification jobs request 100.
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


GPU recipes can opt into concurrent execution with `max_concurrency` (1–32) and
`vram_mb` (private VRAM startup budget). Defaults retain exclusive execution.
GPU workers share the device lock and take separate process-owned slot locks;
VRAM headroom checks and slot assignment serialize under a short admission lock.
Admission keeps the requested free-VRAM floor plus private commitments for all
occupied slots. These are conservative admission budgets, not hardware-enforced
per-process VRAM limits. Slot locks release on exit; stale unlocked records do not
consume slots. Older exclusive jobs retain their lock and finish before new
shared jobs enter. Device concurrency and private VRAM budgets are Docker labels.

The CompAI BWA Vulkan lane supports four simultaneous jobs with a 512 MiB private
VRAM budget each. GPU jobs mount the same verified read-only host-RAM reference
volume as CPU jobs, avoiding one reference copy per GPU process. Each process
still owns its Vulkan context and alignment buffers in VRAM. Concurrent GPU
contexts do not imply a shared full-genome VRAM index. Four concurrent Docker
jobs using that same reference were compared against CPU SAM semantics on 5,000
paired reads, three repetitions each, with positive GPU-extension evidence.


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
