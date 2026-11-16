# Immutable GPU memory owner (opt-in prototype)

One local Vulkan process owns the immutable reference and serves multiple CPU
worker processes. The device-local allocation is uploaded once per owner lifetime;
clients do not create Vulkan devices or allocate duplicate reference VRAM. The
current compute primitive is bounded uint32 indexed gather, groundwork for index
lookup. **Minimap2 alignment and index lookup are not integrated yet.**

Build with `make` (Vulkan development files, glslangValidator, OpenSSL development
files). Run `python ../../scripts/swarm_gpu_memory.py --help` for explicit startup.
Use one private 0700 registry directory for all workers sharing a GPU/reference.
Use `scripts.swarm_gpu_memory.Session` for direct shared input/output views.
The `request(socket_path, sha256, indices)` convenience API uses shared transport
but copies existing Python lists into/out of the mapping. Owner readiness
metadata provides the socket path. There is no auto-deployment.
The caller must obtain fleet resource admission before starting a production owner.

The native upload reads directly into a bounded mapped staging buffer (at most
16 MiB), hashes the exact bytes uploaded, and frees staging after publication.
Reference objects must be read-only regular files, word aligned, and SHA256 pinned.
A device/object lock prevents duplicate uploads within the registry. Four reference
segments support the 2 GiB segment limit and an explicit resident cap; each GPU
allocation checks driver budget and preserves 256 MiB of free headroom. The budget
is a cap on reference bytes; driver allocation overhead and small I/O are additional.

Each session owns 512 KiB of coherent mapped host memory, with a 256 KiB input
region and a 256 KiB output region (up to 65,536 uint32 lookups). The default
`--max-sessions 16` reserves sixteen I/O slots plus the 512 KiB control
arena, 8.5 MiB of logical I/O capacity; imported mapping alignment/driver overhead
may add to that. This does not duplicate reference VRAM.

The default `shared` transport creates a fresh size-sealed Linux memfd per client
session, maps it, and imports those pages into Vulkan using
`VK_EXT_external_memory_host`. The Unix socket passes the FD with SCM_RIGHTS and
then carries only small control/completion messages. Shared sessions default to
a native C++ loop: a four-byte little-endian count and four-byte status per batch.
The Python owner releases the GIL for the whole session loop. The optional
`Session(..., control="json")` path preserves the older Python/JSON protocol for
comparison; socket payload sessions use that protocol. Clients write directly to
`Session.inputs`; the GPU reads those pages and writes results directly to
`Session.outputs`. There is no socket payload, intermediate payload memcpy, or
explicit GPU upload/readback copy on this direct API path. Physical PCIe traffic
still occurs, and the reference is initially uploaded once. This is shared-payload
zero-copy, not an assertion that all data movement or control-message copies vanish.
The import path currently requires Linux x86_64 and coherent cached host-import
support. Select `Session(..., transport='socket')` explicitly for the persistent
socket-payload compatibility path; unsupported imports fail clearly rather than
silently claiming zero-copy. Large alignment payloads remain unbenchmarked.

Up to four GPU submissions run concurrently by default (`--parallelism 1..16`,
not greater than `--max-sessions`). Persistent sessions each reserve a separate
I/O slot, descriptor set, command pool/buffer, and fence; the GPU concurrency gate
allows more connected clients than active submissions. The queue mutex protects
submission only. The four-submission concurrency gate now also lives in C++,
so native requests do not return to Python for admission or completion. A session owns its mapping until disconnection, with a 5-second
idle socket timeout. Clients must not touch buffers during `gather`; completion
makes results readable, and the next gather may overwrite them. Sessions support
one caller at a time. Release any derived memoryviews before closing a session.

A new shared session always gets fresh pages, even if an old client retains its
FD/mapping. Switching a reused slot to socket transport replaces imported pages
as well. New native sessions validate counts and each index; the shader also
checks reference bounds. Invalid input can be corrected within the same session.
Memfds are sealed against shrinking/growing and further seal changes,
so a client cannot truncate GPU-accessible pages. Both peers check socket UID;
registry/socket permissions remain 0700/0600. Shared memory is local; no network
listener is introduced. Failed GPU contexts reject new work and do not recycle
regions that timed-out GPU work might still access.

Immutable reference VRAM is shared through a single VkDevice. Only the host I/O
pages are shared with clients; clients do not import GPU-device allocations.
Different reference versions use different owners. GPU submission failure requires
restart. Driver cleanup reclaims allocations on exit; a subsequent owner holding
the lock removes a stale socket. No automatic restart/retry occurs.

```python
from scripts.swarm_gpu_memory import Session

with Session(socket_path, reference_sha256) as session:
    session.inputs[0] = 123  # Producer writes into the shared request page.
    session.gather(1)
    value = session.outputs[0]  # Consumer reads the GPU-written shared result.
```

## Hardware validation

Run from the workspace root:

```
python JSwarm/run.py --test-source JSwarm/native/gpu-memory-owner --test-source JSwarm/scripts/swarm_gpu_memory.py --test python JSwarm/native/gpu-memory-owner/test_live.py
```

The test obtains a low-priority resource reservation and releases it in `finally`.
It uses an isolated synthetic 8 MiB object, four segments, three client processes,
six batches of 1,009 lookups, and CPU result comparisons. It verifies a single
reference upload, stable allocation size, invalid identity/bounds rejection,
disconnected-client recovery, distinct concurrent request isolation, progress
while another client stalls mid-request, and duplicate-owner rejection. Five
full 65,536-word batches are verified against CPU values, including every output.
Native binary tests cover fragmented controls, excessive counts, invalid indices,
and recovery within a healthy session. The
shared transport checks also cover direct-view results across repeated batches,
zero socket payload bytes, CLOEXEC FDs, resize-seal enforcement, retained-old-FD
isolation after slot reuse, and the explicit socket compatibility path. Adjacent test logs
record command, date, fingerprints, and captured results. No genomic cache or
existing validation service configuration is modified.

## Original serial benchmark — November 15, 2026 (America/New_York)

RTX 3060, driver 580.142. Four clients ran simultaneously, with either one
shared owner or four independent GPU owner processes. A CPU baseline used the
same indices and reference contents in forked processes. Each measured case used
three trials of 100 batches per client, after three warmup batches per client.
The synthetic reference was 64 MiB; repeated batches reuse the same indices, so
this is a warm lookup microbenchmark, not a full-index random-access or alignment
performance result. No CPU affinity or GPU exclusivity was imposed.

For 4,096 uint32 lookups per batch:

| Clients | Shared GPU M lookups/s | Independent GPU M lookups/s | CPU M lookups/s |
| --- | ---: | ---: | ---: |
| 1 | 9.80 | 8.86 | 25.96 |
| 2 | 12.58 | 15.34 | 42.47 |
| 4 | 14.41 | 27.37 | 51.16 |

With four clients, median/p95 complete request latency was 0.995/2.743 ms
(shared), 0.460/0.848 ms (independent), and 0.189/0.243 ms (CPU). Shared reference
allocation/upload was 64 MiB total versus 256 MiB for four independent owners.
These allocation numbers exclude each context's driver overhead. All outputs
were checked against CPU values. In this original implementation, shared requests serialized the socket exchange,
GPU submit, fence wait, and response; the test does not isolate which component
accounts for the speed difference. One-client shared/private cases exercise the
same architecture, so their difference illustrates timing variation rather than
a sharing speed effect.

The shared service saves reference memory but is slower than independent owners
under contention in this benchmark. Tiny 64-word batches were particularly
sensitive to request overhead. The CPU baseline is faster for this primitive;
this does not establish performance for minimap2 chaining/alignment. The subsequent implementation below adds multiple in-flight request regions;
combining requests into larger GPU batches remains future work.

`benchmark-results.json` contains every case, trial duration, throughput, latency,
allocation/upload counts, startup time, and GPU snapshot. The adjacent test log
records the exact source and binary fingerprints and the captured successful run.
The benchmark obtains its own low-priority reservation, creates only synthetic
objects, terminates all owners it starts, and releases the reservation. It does
not change existing validation services or their caches.

## Parallel owner benchmark — November 15, 2026 (America/New_York)

The owner now defaults to four independently reusable slots. The same GPU and
64 MiB synthetic reference were tested with 1, 4, and 8 simultaneous client
processes, 1/4/8 slots in a shared owner, and independent one-slot owners.
Three trials per case, 100 batches per client per trial, and warmups were retained.
Every returned value was checked. The final build includes the failed-context
memory-reuse guard. Results below come from that final build, rather than the
preliminary parallel build.

For 4,096 lookups per batch, median trial throughput (million lookups/sec):

| Clients | Shared: 1 slot | Shared: 4 slots | Shared: 8 slots | Independent owners |
| --- | ---: | ---: | ---: | ---: |
| 1 | 8.50 | 5.56 | 8.78 | 6.68 |
| 4 | 12.33 | 18.30 | 19.47 | 23.86 |
| 8 | 10.22 | 17.69 | 14.70 | 31.25 |

With eight clients, four slots improved throughput 1.73x over one slot and reduced
median/p95 complete request latency from 3.046/6.294 ms to 1.695/3.262 ms.
Four slots retained one 64 MiB reference upload/allocation; eight independent
owners required 512 MiB of reference allocation/upload, excluding context overhead.
The driver-facing counter reached four outstanding submissions for the four-slot
owner and seven for the eight-slot owner. This counter tracks submitted work whose
fence wait has not returned; it does not measure simultaneous GPU kernel execution.

Four slots remain the default because eight performed worse with eight clients.
Timing varies with background GPU activity and host scheduling, including the
one-client cases where extra slots provide no parallelism. This warm gather
microbenchmark establishes working concurrent requests and a memory/throughput
tradeoff, not full-index or minimap2 alignment performance. The CPU baseline was
not rerun; its earlier results are retained in `benchmark-results.serial.json`.

The final successful hardware correctness run is recorded in adjacent test logs
with output `353f24b52df34580b19807f1a6f410ce.log`; it also verifies that a client
stalled before its payload does not block another request. The final benchmark
output is `a2fe5a0847704ca78c4bcecb1946125e.log`, with detailed rows in
`benchmark-results.parallel.json`. Logs contain the tested code/binary fingerprints.
All benchmark owner processes were stopped and their resource reservation released.
Existing alternate validation services were not reconfigured or stopped.

## Shared payload benchmark — November 15, 2026 (America/New_York)

`benchmark_shared.py` compares shared pages with socket payloads using the same
persistent-session control protocol, four active GPU submissions, sixteen session
slots, 64 MiB reference, and the same input/output views. Both modes avoid per-batch
Python payload packing; the socket mode sends/receives those views while shared
mode accesses imported pages directly. Session creation and GPU host import occur
before warmup/timing. Three measured trials each have 100 batches per client,
after three warmup batches; every output is checked, and one input varies each
batch to detect stale completions. Throughput includes output verification;
per-request latency excludes it. GPU activity and host scheduling are not isolated.

For 4,096 lookups per batch:

| Clients | Shared M lookups/s | Socket M lookups/s | Shared median/p95 ms | Socket median/p95 ms |
| --- | ---: | ---: | ---: | ---: |
| 1 | 8.93 | 4.60 | 0.202 / 0.911 | 0.339 / 2.718 |
| 4 | 10.53 | 17.59 | 0.684 / 3.894 | 0.452 / 1.633 |
| 8 | 16.98 | 11.08 | 1.390 / 3.485 | 2.076 / 6.623 |

Shared transport recorded zero socket payload bytes in every case, versus
81,002,496 bytes for the eight-client, 4,096-word socket case including warmups.
Both used one 64 MiB reference allocation/upload. Both retained zero explicit
GPU request-upload/result-readback copies. Shared session setup was more expensive:
median 14.24 ms versus 4.99 ms for socket sessions in that eight-client case.
Persistent sessions amortize setup across many batches.

This demonstrates removal of socket payload copies, with mixed timing results:
shared memory was faster with one and eight clients but slower with four in this
run. It does not establish a universal speedup, and the experiment does not isolate
the causes of the variation. Earlier benchmarks used different control protocols
and returned Python lists, so their throughput is not a direct copy-only comparison.
These remain synthetic warmed lookups, not alignment or full-index qualification.

`benchmark-results.shared.json` contains all counts, trial durations, throughput,
latency, setup time, verified batch counts, copy counters, and GPU snapshots.
The successful correctness run is captured in adjacent test logs as
`0e7b6e5561c84de0a9b096ce140f298a.log`; the successful benchmark is
`f6c1e1cbe14d42d781fceaf542891498.log`. Logs record dates, exact commands,
and source/library/shader fingerprints. The benchmark reserves its own bounded
resources and cleans up its owner processes; alternate validation remains untouched.

## Native request loop and larger batches — November 15, 2026 (America/New_York)

Profiling the earlier Python/JSON path (`profile-json.json`) found that a
single-client 4,096-lookup request averaged 629.6 microseconds wall time, while
Vulkan timestamps measured 16.05 microseconds of GPU command execution. The
native call averaged 234.52 microseconds, including 212.35 microseconds waiting
for its fence. CPU validation, recording, and submission averaged 2.28, 12.82,
and 6.55 microseconds respectively. This measures the host-side overhead and
waiting that a count of GPU lookups alone cannot explain. Fence wait includes
queueing and host rescheduling, not just GPU execution.

Changes following that measurement:

- Shared sessions run their repeated control/dispatch/completion loop in C++,
  avoiding per-request Python JSON processing and GIL contention. The client still
  uses Python for the eight control bytes; payload pages remain shared directly.
- The concurrency gate is native and applies to both control protocols.
- The maximum batch increased from 4,096 to 65,536 lookups, with 512 KiB per-session
  input/output space. Larger batches amortize submission and completion overhead.
- `--profile` enables opt-in native CPU timings and GPU timestamps. Native timing
  includes validation, capacity wait, recording, submit, fence wait, and total;
  GPU timestamps cover the command-buffer span, not occupancy or total latency.
- `--spin-us 0..100` permits bounded fence polling before blocking. A 50-us poll
  was tested but did not consistently improve throughput and used more CPU.
  Blocking waits (`--spin-us 0`) remain the default.

The initial timing harness materialized every output into a Python list after
each batch. Those full-verification results are retained in `profile-fast.json`.
They include substantial verification/allocator overhead and should not be used
to characterize direct-view throughput. The revised harness fully verifies the
warmups and final output, checks changing first/last words after each timed batch,
and records completion before final full verification and teardown. The separate
hardware correctness test still checks every word of five maximum-size batches.
Use `--verification full` to restore full comparison after every timed batch.

All following measurements use the revised sampled-verification timing method,
three trials of 100 batches per client, four active GPU submissions, one 64 MiB
synthetic reference, and opt-in GPU timestamp profiling. They exclude session
setup and do not measure downstream alignment/result processing. Host and GPU
activity were not isolated.

| Clients | Batch lookups | Native control, blocking: M lookups/s | Median / p95 request ms |
| --- | ---: | ---: | ---: |
| 1 | 4,096 | 15.6 | 0.148 / 0.998 |
| 1 | 65,536 | 201.4 | 0.290 / 0.503 |
| 8 | 4,096 | 47.3 | 0.317 / 2.180 |
| 8 | 65,536 | 292.4 | 1.365 / 3.633 |

The native loop with 50-us polling (`profile-direct.json`) reached 49.3 M/s for
4,096-word batches with eight clients versus 26.8 M/s for JSON control using the
same polling configuration and verification method. At 65,536 words it reached
238.2 M/s versus JSON's 244.6 M/s, so native control did not improve that large-batch
case. Disabling polling (`profile-blocking.json`) yielded the table above; the
owner used 0.33 CPU seconds across setup/warmups/three trials in the eight-client,
large-batch case, versus 0.42 CPU seconds in the polling run. These separate runs
are indicative; changing background activity prevents a guarantee of those ratios.
Native control reduces small-request overhead, while batching gives the larger
throughput benefit. Earlier 17 M/s results used different verification and batch
sizes and are not a like-for-like speedup baseline.

The blocking eight-client large-batch GPU command span averaged 144.29 us. Its
native call averaged 1,350.85 us, including 557.38 us waiting for one of four GPU
permits and 644.43 us in fence wait; these host waits explain why aggregate
throughput and per-request latency differ from isolated GPU command time.
Reference allocation/upload remained 64 MiB once, shared socket payload bytes
remained zero, and explicit GPU upload/readback copy counts remained zero.
These are warmed lookup microbenchmarks, not minimap2 alignment results.

A read-only GPU snapshot also observed a current PCIe link width of x4 with x16
maximum capability; link generation varies with idle power state. This warrants
measurement under sustained workload before attributing a bottleneck or changing
hardware settings. `pcie-observation.json` retains a snapshot. No clocks, PCIe,
power, network, or alternate-validation settings were changed.

Benchmark commands and exact code/library fingerprints are recorded in adjacent
logs: initial profile `a03517c86f4a4471a44a4e7c450e6af1.log`, direct-view comparison
`d5edbe795c534a86be598f728678e9b7.log`, and blocking native comparison
`f2ad5966865a4a4bb0fff982fb775363.log`. Results explicitly used `--spin-us 0`
before that setting became the CLI default; the explicit configuration is unchanged.
`benchmark_shared.py --profile --clients 1,8 --counts 4096,65536 --transports shared
--controls native --spin-us 0 --output RESULT.json` reproduces that configuration
when invoked through `JSwarm/run.py --test-source ... --test python ...`, using
its own low-priority bounded reservation. Reuse the logged run for unchanged code,
configuration, and inputs rather than rerunning it.

Final default-configuration hardware correctness passed in
`7376f7253ed040a6bcacc1a404fd36fc.log`, including full maximum-size outputs,
fragmented binary controls, invalid-input recovery, concurrent sessions, sealed
mapping protection, retained-FD isolation, and duplicate-owner rejection. Its
snapshot confirms `spin_us: 0`, `max_queries: 65536`, and zero shared socket payload
bytes. All temporary GPU owners were stopped and resource reservations released.

## Paired CPU vs GPU comparison — November 15, 2026 (America/New_York)

Ryzen 7 5800X (8 cores / 16 threads) versus RTX 3060. CPU gather is native C++
compiled with `-O3 -march=native`, with the same bounds validation and indexed
uint32 read/write operation as the GPU; it is not a Python-loop baseline. CPU
workers share the same 64 MiB reference through fork/COW and never modify it.
GPU workers share one 64 MiB reference allocation and use the complete persistent
native-control shared-memory service, including IPC, admission to four active GPU
slots, submission, and completion. GPU timestamp instrumentation was disabled in
this run, matching the normal service configuration.

Both paths use identical seeded index streams and direct input/output buffers.
The **warm** case repeats its index set (changing the first index each batch to
catch stale results). The **changing** case generates a fresh pseudorandom set
across the full 64 MiB reference before every batch, using the same native producer
on CPU and GPU. This challenges CPU caches more than the repeated-index case.
The CPU baseline uses existing host buffers without an IPC service, reflecting
local CPU execution; GPU service overhead is deliberately included.

Three trials of 100 batches per worker were measured, with full verification of
all warmup/final values and changing first/last output checks per timed batch.
Throughput includes index generation and those lightweight checks; request latency
excludes index generation/checks. Full verification and session/process startup
and teardown are outside the timing window. CPU invalid indices were also checked
for safe rejection. All correctness assertions passed.

Median-trial aggregate throughput, million lookups per second:

| Workers | Index pattern | Batch lookups | CPU M/s | GPU M/s | Faster in this run |
| --- | --- | ---: | ---: | ---: | --- |
| 1 | Warm/repeated | 4,096 | 227.4 | 24.4 | CPU 9.34x |
| 1 | Warm/repeated | 65,536 | 297.3 | 207.9 | CPU 1.43x |
| 1 | Changing | 4,096 | 50.6 | 25.5 | CPU 1.98x |
| 1 | Changing | 65,536 | 89.7 | 158.9 | GPU 1.77x |
| 8 | Warm/repeated | 4,096 | 243.4 | 56.2 | CPU 4.33x |
| 8 | Warm/repeated | 65,536 | 374.3 | 279.9 | CPU 1.34x |
| 8 | Changing | 4,096 | 90.2 | 59.2 | CPU 1.52x |
| 8 | Changing | 65,536 | 152.1 | 274.2 | GPU 1.80x |

Thus the optimized CPU wins on small requests and the cache-friendly repeated
sets. GPU aggregate throughput wins for large changing sets in this experiment.
With eight workers in that large/changing case, median individual request latency
was 0.959 ms on CPU versus 1.304 ms on GPU: aggregate throughput and median latency
are different measures, especially with producer work and scheduling variation.
A GPU throughput win does not imply every GPU request has lower latency.

**Contention limitation:** the machine was not isolated. A post-run read-only
snapshot recorded load averages 55.0 / 39.3 / 33.7 on 16 logical CPUs. The process
had nice 0, access to CPUs 0–15, and no CPU quota in its cgroup ancestry. The run
received an eight-CPU fleet reservation, but this does not give exclusive physical
cores or remove unrelated host activity. These are contended-host observations,
not peak or idle-machine hardware limits. CPU/GPU mode order alternated by case;
that cannot eliminate changing background load. No other jobs were stopped or
reconfigured, including alternate validation. No full-index or minimap2 alignment
speed conclusion follows from this synthetic 64 MiB benchmark.

`benchmark-results.cpu-gpu.json` retains all sixteen cases, three trial durations
each, latencies, hardware/compiler details, admitted CPU budget, affinity, and
final GPU counters. `cpu-constraints.json` records the post-run scheduling/load
snapshot. The GPU reference uploaded once; shared socket payload bytes and
explicit GPU upload/readback copy counts remained zero. The temporary owner was
stopped and the reservation released.

Build the CPU baseline with `make -C JSwarm/native/gpu-memory-owner cpu-gather.so`.
`benchmark_cpu_gpu.py` is run through the workspace test logger with its source,
CPU source/binary, Makefile, GPU source/library/shader, and Python service specified
as test sources. The successful captured output is
`12c57ca33e0b4350a9a87dde160e9ad5.log`; adjacent logs record command, date, and
exact code/binary fingerprints. Reuse this run for unchanged code/configuration/
inputs instead of rerunning it.

## Command reuse and measured I/O optimization

The default now reuses each slot's executable Vulkan command buffer when its
batch count is unchanged. The cached command includes the host/GPU barriers,
descriptors, dispatch, and optional profiling timestamps. It is submitted again
only after that slot's prior fence has completed. A changed count or replacement
session buffer invalidates the cache. Input contents may change every request;
CPU index validation and the shader bounds check remain in place. This does not
cache results or assume repeated reference indices. `--command-reuse off` keeps
an uncached control path for reproducible measurement.

Mapped I/O remains the default (`--device-io mapped`) and retains zero GPU payload
copies. An explicit experimental `--device-io staged` path copies the active
input range to a per-slot device-local buffer, computes there, and copies the
active output range back. The client still uses shared pages and no socket payload
copies, but GPU upload/readback copies are real and counted. The session handshake
reports `device_io` and `gpu_payload_zero_copy`; callers can distinguish them.
Staging adds 8 MiB of VRAM for sixteen 512 KiB slots, apart from the reference and
driver overhead. It is not selected automatically because it lost the multi-client
comparison on this RTX 3060.

### Interleaved A/B results

`benchmark_optimization.py` rotated configuration order across three trials of
100 batches per client, with the same source, seeded producer, validation, and
persistent native control path as the paired CPU/GPU comparison. It measured one
and eight workers, warm and changing index sets, and 4,096/65,536-word batches.
Each configuration had a private 64 MiB reference owner for isolation, 192 MiB
of total test reference VRAM; only one configuration ran requests at a time.
A normal deployment still has one reference allocation per shared owner.

Eight-worker median-trial throughput, million lookups/sec, using the same original
divide/modulo shader for all three configurations:

| Pattern | Batch lookups | Caching off, mapped | Caching on, mapped | Caching on, staged |
| --- | ---: | ---: | ---: | ---: |
| Warm | 4,096 | 81.2 | 81.3 | 54.2 |
| Warm | 65,536 | 367.9 | 374.8 | 265.4 |
| Changing | 4,096 | 90.5 | 103.0 | 72.7 |
| Changing | 65,536 | 339.4 | 355.3 | 259.1 |

Caching improved changing-index throughput about 14% for small batches and 5%
for large batches in this controlled run. Per eight-worker case, recording count
fell from 2,472 to 24, with 2,448 command reuses (including warmups). Cache hits
were asserted, not inferred from throughput. Every configuration's output was
checked, staged copy counts matched the exact active byte ranges, and mapped copy
counts stayed zero. The caching benefit was not universal: the single-worker,
changing large-batch case measured 142.7 M/s cached versus 150.0 M/s uncached.
Other single-worker cases improved 8–12%. Host load averaged about 30 before and
29 after; timings remain subject to unrelated activity.

The full results and the tested binary/source fingerprints are retained in
`benchmark-results.optimization.json` and adjacent logged run
`1aa5718ac11245e890304dd560d7054f.log`. The uncached control uses the same new
recording/barrier implementation with caching disabled, isolating reuse rather
than treating an older run on a different host load as a baseline.

### Rejected shader experiment

A power-of-two segment-addressing experiment replaced divide/modulo with shifts
and masks, retaining division for other segment sizes. It was correct on both
power-of-two and non-power-of-two test segments, but the profiled interleaved
comparison showed no convincing GPU execution-time benefit. Eight-worker changing
large batches averaged 145.88 us of GPU command span with the baseline shader and
144.33 us with the experiment, while throughput regressed from 366.7 to 314.4 M/s
in that run. Single-worker warm small-batch throughput also regressed; other cases
varied. This did not justify keeping the shader change.

The production `gather.comp` and `gather.spv` therefore retain the original shader.
The rejected source remains explicitly experimental in
`gather_experiment_shift.comp` / `gather-shift.spv`; the baseline is retained as
`gather_baseline.comp` / `gather-baseline.spv`. Build those optional artifacts with
`make gather-baseline.spv gather-shift.spv`. `benchmark-results.shader.json` and
logged run `77910edf78cd438dada768daef84335e.log` retain the measurements. The
benchmark's `cached-optimized-shader` label denotes the experimental candidate,
not a production recommendation.

### Final paired CPU/GPU check

The kept configuration (command reuse on, mapped I/O, original shader, blocking
fence waits) was compared again with the optimized native Ryzen 7 5800X baseline.
The same workload, three-trial method, and eight-worker admission were retained.
These values are a new contended-host comparison, not a claimed speedup against
an earlier run with different background load:

| Eight workers | Batch lookups | CPU M lookups/s | GPU M lookups/s |
| --- | ---: | ---: | ---: |
| Warm | 4,096 | 215.7 | 42.0 |
| Warm | 65,536 | 305.9 | 210.2 |
| Changing | 4,096 | 102.6 | 28.4 |
| Changing | 65,536 | 119.0 | 323.3 |

The large/changing case favored the GPU by 2.72x in this run; CPU execution still
won the other eight-worker cases. The wide differences from the short interleaved
A/B run show why these are not peak throughput guarantees. The pure cache effect
is supported by the matched A/B comparison above, not by cross-run ratios.
`benchmark-results.cpu-gpu.optimized.json` contains all one/eight-worker cases,
latencies, trial durations, and final owner statistics. The owner recorded 108
command recordings and 11,016 reuses across 11,124 successful batches; reference
upload happened once and both GPU copy counters and shared socket payload bytes
stayed zero. The successful logged comparison is
`edefc54b9fea4802b67dbaa810739db3.log`.

Final kept-backend correctness tests passed in
`6e7ccafead5c41da9bff5cbc10fbb03a.log` (mapped) and
`b7869903b54b4f99840fae201d5cf636.log` (staged). Coverage includes full maximum-size
outputs, changing data with cache hits, sizes around workgroup/batch boundaries,
cache invalidation on count and session changes, invalid inputs, fragmented
controls, concurrent clients, stale retained FDs, and resize seals. A separate
non-power-of-two segment run is recorded in
`380e351fe7854608942e5122a879410b.log`. Adjacent logs associate each run with the
actual code, library, and shader fingerprints; the post-run host snapshot is in
`optimization-host-context.json`. All temporary owners were stopped and their
reservations released. Other jobs and alternate validation were not reconfigured
or stopped. These optimizations apply to synthetic index gather; minimap2 alignment
integration remains pending.


## Device-resident ceiling probe — 2026-11-16

`vram_benchmark.cpp` is an isolated Vulkan benchmark reusing the production gather
shader and owner allocation helpers. It does not change the running owner or
implement minimap2 alignment. Build with `make vram-benchmark resident_setup.spv`.
`benchmark_vram.py` acquires a low-priority local resource reservation and releases
it in `finally`. It uses 256 MiB of device-local reference plus 256 MiB of
device-local I/O, with a separate 16 MiB host readback buffer. Reference and query
initialization run on the GPU. Setup and correctness readbacks are outside timing.
Each sample submits 32 operations in one command buffer, with GPU timestamps and
no host payload transfer or CPU round trip between operations. Five samples per
case follow warmup. These are repeated, fixed-query batches, not changing-query
end-to-end service measurements. The device was shared with existing workloads.

Results in `benchmark-results.vram.json` (medians):

| Operation | Batch | GPU time/operation | Throughput |
| --- | ---: | ---: | ---: |
| Transfer copy | 256 MiB | 1.890 ms | 284.0 GB/s read + write |
| Compute copy | 256 MiB | 1.800 ms | 298.2 GB/s read + write |
| Sequential gather | 2,097,152 | 91.55 us | 22.91 billion lookups/s |
| Random gather | 65,536 | 23.29 us | 2.81 billion lookups/s |
| Random gather | 1,048,576 | 357.63 us | 2.93 billion lookups/s |
| Random gather | 2,097,152 | 707.85 us | 2.96 billion lookups/s |

All gather outputs were checked after warmup and after the timed samples. Copy
correctness checks sampled eight 4 KiB windows covering both ends of every
reference segment. The logged test passed:
`.test-output/90d6bb8d9a664d13bbc296ad1a48209f.log`; adjacent test logs contain
the command, date and source/binary/shader fingerprints.

The large random gather requests 35.55 GB/s of logical index/reference/result
bytes. This is **not physical DRAM bandwidth**: scattered 4-byte loads fetch
larger cache sectors, and repeated queries may benefit from caching. Therefore
35.55 / 298.2 does not measure VRAM utilization or available optimization headroom.
Small sequential batches also benefit from caching. The copy tests provide an
empirical streaming reference, not a proven random-access ceiling. The existing
host-mapped service still has PCIe, IPC and synchronization overhead absent here;
its earlier results have different reference sizes and timing boundaries.

Hardware-counter preflight found `RmProfilingAdminOnly: 1` in
`/proc/driver/nvidia/params`. `nsys profile --gpu-metrics-device=help` recognizes
the RTX 3060, but counters are restricted to administrator access. The available
Vulkan device extensions do not expose `VK_KHR_performance_query`; Nsight Compute
is a CUDA profiler, and Nsight Graphics is unavailable. No hardware stall counters
were collected and no profiling permissions, driver settings, clocks or other
jobs were changed. Thus this run demonstrates device-resident throughput but does
not establish VRAM saturation or identify specific shader stall reasons.


## Cache-locality experiment — 2026-11-16

`cache_group.comp`, `cache_benchmark.cpp`, and `benchmark_cache.py` add an
experimental device-resident bucketed gather. This is not a software cache or
proof of improved hardware cache hit rates: it reorganizes queries so nearby
reference locations are processed together, to encourage hardware cache reuse.
Production `owner.cpp`, `gather.comp`, and service defaults are unchanged.

The 256 MiB reference is split into 256 one-MiB locality regions. GPU histogram,
prefix and scatter passes group the query indices and retain an inverse position
map. The gather writes contiguous grouped results. A separate GPU pass restores
original request order. Index bounds remain checked, including UINT32_MAX. All
scratch resides inside the existing 256 MiB device I/O allocation (512 MiB total
device reference + I/O); the largest batch uses about 40 MiB of I/O space. No host
payload copies occur between timed operations. Grouping uses additional scratch
traffic and atomics; it does not eliminate memory costs.

Build: `make cache-benchmark cache_group.spv`. Execute the admitted harness via
`JSwarm/run.py --test ...`, fingerprinting the harness, cache C++/shader/binary,
resident benchmark helper, owner and input SPIR-V files as in the adjacent logs.
The harness uses a low-priority local capacity lease and releases it in `finally`.
It runs eight operations per sample, five samples per mode, with original uniform
random queries retained across samples. The direct baseline is rerun here because
this comparison has different repetition counts and adjacent grouping work.

Two candidates were measured. The first scattered gathered results directly to
original positions and was slower even with preprocessing excluded. Its results
are retained in `benchmark-results.cache.json.scatter-output`, with test log
`c8f22c54344947d98d0f4bff416e9247.log`. The final candidate adds a separate order
restoration pass, improving the large reused batch:

| Queries | Direct | Group every batch, all GPU passes | Reuse grouped queries, gather + restore |
| --- | ---: | ---: | ---: |
| 65,536 | 24.17 us | 103.17 us | 29.60 us |
| 1,048,576 | 326.52 us | 1,027.07 us | 326.82 us |
| 2,097,152 | 648.48 us | 1,779.08 us | 520.17 us |

For the largest batch that is 3.23 billion direct lookups/s versus 4.03 billion
with reused grouping, but only 1.18 billion when grouping is charged every time.
The estimated preprocessing cost is 1,258.91 us (total minus reused median),
yielding an estimated break-even near ten uses of an unchanged query batch. This
is an estimate from separate median timings, not a measured repeated-use service
speedup. Uniform random inputs have little reuse; real alignment query locality
and repetition must be measured before deciding whether to integrate this path.
Small batches regress, and no automatic production selection is enabled.

Final results: `benchmark-results.cache.json`; passing test:
`.test-output/084ebd939f5d4638a97bac7d0776d2b8.log`. Adjacent test logs record date,
command and content fingerprints. Every output was checked in original request
order after each mode for sizes 1, 63, 64, 65, 4095, 65536, 1048576 and 2097152.
Separate full checks at sizes 1, 65 and 4097 cover repeated identical indices,
reverse reference order and mixed valid/out-of-range indices. Existing correctness
readbacks are outside timing. No cache-hit or detailed stall counters were
collected, so the timing gain does not itself prove a particular cache mechanism.
The alternate validation and other GPU jobs were not stopped or reconfigured.
