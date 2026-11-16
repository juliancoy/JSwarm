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
