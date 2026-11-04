# ramtop

A full-screen Linux performance monitor with **Memory, CPU, GPU, Disk, Network and LAN**
tabs. Textual, Rich and psutil provide the interface and conventional metrics;
Linux perf PMUs provide measured hardware counters. Missing measurements show
N/A, never a guessed replacement.

## Install and run

From the Organization workspace, using the shared Python environment:

```bash
~/global/bin/python -m pip install -e ./ramtop
~/global/bin/ramtop
~/global/bin/ramtop --interval 0.5
~/global/bin/ramtop --diagnose > ramtop-diagnostics.json
```

Requires Linux and Python 3.10+. `perf` is optional for diagnostics and required
for address sampling. `ss` from Ubuntu's `iproute2` package enables connection
rates; interface counters work without it. iotop/iftop do not need to be installed.
No modules, mounts, permission policies or hardware registers are changed.

## Navigation

Click the six tabs, or use **1–6** for Memory, CPU, GPU, Disk, Network and LAN.
**C** switches between CPU and Memory; **G** selects GPU. Tables scroll with
keyboard or mouse. Click any table header to sort, then click again to reverse.
Numeric columns sort by raw values; missing values remain last. Sort arrows and
choices persist across refreshes. Narrow terminals retain scrolling tables.

| Key | Action |
| --- | --- |
| 1 / 2 / 3 / 4 / 5 / 6 | Memory / CPU / GPU / Disk / Network / LAN |
| F1 | Measurement guide |
| D | PMU discovery and access errors |
| Space | Freeze display; counter baselines continue updating |
| + / - | Faster/slower refresh, 0.25–10 seconds |
| M / P | Sort the active Memory/CPU process table by RSS / CPU |
| / | Focus the current tab's process/connection filter |
| Escape | Clear current filter or close a modal |
| B / Process samples button | Capture per-process CPU load/store samples |
| A | Capture address hotspots |
| S (sampling view) | Switch processes / addresses |
| R (sampling view) | Capture again |
| Q / F10 | Quit |

GPU, disk and network detail polling starts when its tab is selected. A newly
selected or resumed detail view warms its baselines before showing rates.
Refresh changes apply to all tabs. Collection runs outside UI rendering for disk,
network and GPU details. No reverse-DNS queries or packet capture are performed. The explicit LAN discovery
button sends bounded ICMP probes and optional mDNS discovery queries.

## Memory

Separate green RAM READ and amber RAM WRITE graphs show measured IMC byte rates,
current bandwidth and rolling peaks. Each holds up to 120 samples and scales
independently. Missing intervals clear the affected history instead of drawing a
false zero or bridging gaps. RAM allocation has its own separate graph.

The backend priority is:

1. Kernel-exposed `uncore_imc*` CAS read/write PMUs with explicit byte scaling.
   Show each controller/channel counter domain by its kernel identifier and
   representative CPU; these names do not establish physical DIMM slot topology.
2. Aggregate/free-running IMC `data_reads`, `data_writes`, `data_read`, `data_write`
   aliases, requiring kernel scale and byte units.
3. Existing root-group Intel RDT/resctrl MBM: LLC-to-memory traffic, not individual
   DRAM directions or channels. Local MBM overlaps total MBM; never sum them.
   Administrator event filters apply; tasks in other assigned groups are outside
   root-group scope.
4. Generic cache reference/miss event rates and conventional OS memory statistics.
   Perf-denied systems still display RAM, cache, buffers, swap, faults and RSS.

No guessed raw bandwidth event codes, equal channel splits, cache-miss-to-byte
conversions, or inferred saturation percentages are used. If only some IMC
domains/directions are available, graphs show observed domains, not guaranteed
whole-system coverage. Rates use monotonic time and interval enabled/running
counter deltas to compensate for multiplexing. Initial, unscheduled, reset and
failed reads show N/A. Kernel perf handles hardware wraparound.

CPU (`ia_requests`), integrated GPU (`gt_requests`) and I/O (`io_requests`) graphs
show raw IMC **requests/s**, combining reads/writes across channels. Partial writes
can merge, so multiplying these request counters by 64 does not give accurate
DRAM bytes. The kernel's MiB scaling on these aliases is deliberately ignored.

`--diagnose` reports CPU identity, PMUs/aliases, selected counters, access errors,
perf_event_paranoid and `perf list`. On the Latitude i5-8350U, aggregate IMC
read/write and CPU/GPU/I/O request counters have been verified live, despite PCM
202201-1 `pcm-memory` rejecting processor model 0x8E.

If counters are absent, an administrator can inspect the kernel's `intel_uncore`
module and perf access policy. Do not grant capabilities to a shared Python
interpreter just for this application. ramtop does not change access policy.

## Local AMD Ryzen / fallback layout

The local `compai` host has a Ryzen 7 5800X, 16 logical CPUs and 64 GB of RAM.
Its current kernel exposes generic cache counters but no supported IMC byte
aliases, and resctrl is not mounted. The Memory tab adapts to this situation:

- Aggregate cache-reference and cache-miss event histories replace empty Intel
  agent graphs. Their ratio is explicitly a generic cache event ratio, not DRAM
  utilization or guaranteed LLC hit rate. Missing samples keep the ratio unknown.
- A short backend summary replaces dozens of per-thread counter lines. Raw
  per-thread readings remain available through **D**.
- Memory PSI some/full 10-second stall averages, dirty/writeback bytes and
  reclaimable slab show pressure and reclaimable kernel memory. Missing fields
  remain N/A. None are presented as bandwidth.
- Compact terminals reclaim empty counter space and reduce chart height so the
  sortable process table remains visible at 80×24.
- CPU identity is cached rather than rereading full CPU identification every tick.

On systems already exposing resctrl MBM, separate total/local LLC-to-memory
histories appear instead; those overlap and are never summed into a combined
bandwidth. Diagnostics identify AMD/Intel vendor and report advertised MBM when
its resctrl interface is absent. No kernel modules are loaded and no resctrl
mount or global permission change is made. Intel IMC directional graphs remain
available on the Latitude.

## CPU

Htop-style meters show each logical CPU, their average, and load averages. Bar
colors distinguish user (green), system/IRQ (red), nice (blue), steal (yellow) and
I/O wait (cyan). Busy percentages exclude idle and I/O wait; guest time is not
double-counted. The Latitude has eight logical CPU meters. Narrow terminals show
one meter per row. A separate sortable process table defaults to CPU usage and
supports name/PID filtering. Multithreaded process CPU can exceed 100%.

## GPU

The Intel i915 GPU tab has three detail tabs:

- **Overview**: GPU-only read/write availability, combined GT memory requests,
  render/copy/video/video-enhancement busy/wait/semaphore activity bars, interrupts,
  PMU clock averages, RC6 residency, software awake time, GPU and package watts.
- **GPU clients / memory**: readable DRM client owners, device/client IDs,
  allocated/resident/shared/active/purgeable memory and engine utilization. Engine
  sorting uses the busiest reported engine. Repeated descriptors for the same
  device/client ID are deduplicated, including clients shared between processes.
- **Sensors / access**: GPU sysfs identity, frequency limits/current clocks,
  exposed device hwmon sensors, access failures and coverage notes.

The Latitude exposes no separate GPU-only read/write byte counters through these
interfaces; those panels show N/A. GT requests remain requests/s. The integrated
GPU uses system RAM. Client memory is allocation/residency, not traffic, and shared
buffers can appear in several clients. No unique physical-memory total is claimed.
PMU frequency averages include idle time and may fall below active clock minimums.
GPU/package power overlap; engine activity may overlap too. Do not sum either.
Other GPU drivers report unsupported rather than fabricated telemetry.

## Disk — iotop-style monitoring

Choose a block device for separate live read/write graphs. **Devices** shows
throughput, read/write IOPS, mean completed read/write time (ms/op), busy time and
whether the row is a device, partition or stacked layer. Graphs use one selected
layer: parent/partition/device-mapper counters are never summed. BUSY is the
fraction of time with outstanding I/O, not a percent of the device's maximum
throughput. Concurrent I/O means completion times can exceed wall time.

**Processes** ranks readable per-process kernel storage read/write accounting,
with PID, user, command, cached/other read/write character rates, and cumulative
cancelled write bytes. Process identity includes start time so PID reuse does not
inherit old rates. New/reset counters show N/A. Buffered writes, cancellation,
filesystem effects, short-lived tasks and permissions mean process totals need
not match physical device traffic. Character rates are not storage bandwidth.
Unavailable task-delay I/O-wait percentages are not invented. **Filesystems**
shows mounted filesystem used/free capacity and usage percentage.

## Network — iftop-style monitoring

Choose an interface for live receive/transmit graphs. **Interfaces** shows bytes/s,
packets/s, errors/s, drops/s, link status, reported link speed and MTU. Virtual
interfaces, bridges and loopback can overlap; no misleading aggregate is made.

**Connections** ranks numeric local/remote TCP and UDP endpoints, readable process
owners, queues, state and TCP RTT. For persistent TCP sockets, `ss` supplies
`bytes_received` and `bytes_acked`; interval deltas show received payload/s and
acknowledged transmit payload/s. These are not packet-capture wire rates or TCP
capacity estimates. Graphs remain interface counters. UDP byte rates are N/A.
The `ss send ... bps` estimate is deliberately not interpreted as throughput.
Stable socket inode/cookie plus endpoints identifies each rate baseline. Missing
counter fields and new/reset sockets show N/A. Polling can miss short-lived
connections. Socket/process coverage follows existing kernel permissions; it is
not restricted to the currently selected interface, and does not represent all
forwarded traffic. Filter by address, port, protocol, PID or process.

## LAN — local nodes and TX/RX

Select **LAN** or press **6**. Choose an interface to list known local addresses,
hostnames when available, MAC addresses, kernel neighbor states, observed TCP flow
counts and **TX → NODE/s / RX ← NODE/s**, from this laptop's perspective. The two
graphs sum measured TCP payload rates for local peers on that interface. Click
headers to sort, or filter by IP, name, MAC or state. `/` focuses the filter.

Inventory combines this host's interface addresses, IPv4 ARP / IPv6 neighbor cache,
and sockets whose remote addresses are in the selected interface's attached
subnets. Internet traffic is not attributed to the gateway. Failed/incomplete
ARP entries without a MAC are not invented nodes. A device may have several
addresses, and cached STALE entries are not proof that it is online.

**Discover nodes on this interface** performs one bounded ICMP probe per eligible
IPv4 address (at most 512 addresses, 32 concurrent probes), plus Avahi mDNS service
discovery where available. It can take several seconds while the UI stays
responsive. Larger subnets are skipped explicitly; IPv6 uses neighbor/mDNS
inventory rather than sweeping its enormous address space. Results are retained
as discovery-cache entries when tabs change. Discovery is on demand, not an
automatic periodic scan. `ip` and `ping` come from Ubuntu's iproute2/iputils-ping;
avahi-browse is optional. Static names from `/etc/hosts` also appear.

TX/RX uses the same kernel TCP received/acknowledged payload counters as Network;
no complete baseline means N/A, including nodes with no observed TCP connections.
These are not each node's total interface counters, UDP rates, packet-capture
bytes, or traffic exchanged between other machines. Summed graphs show N/A when
any observed flow lacks the required directional baseline. Packet privileges
are not required. Seeing an entire LAN requires router/switch telemetry or agents
on the other nodes; discovery cannot guarantee every silent/offline/isolated
host is found. The UI reports this scope explicitly.

## CPU memory samples and address hotspots

**B** captures a three-second per-process summary; click LOAD SAMPLES or STORE
SAMPLES to sort. Other columns include PID, command, mean sampled load use latency,
distinct virtual 4-KiB ranges and total samples. Every retained range contributes
before the 500-row display limit. Mean latency is weighted by load sample count.
**A** opens address ranges instead; **S** switches views without recapturing.

Sampling is opt-in, system-wide user-space `perf mem`, with period 200003 and a
30-cycle load latency threshold. Stores have no comparable latency estimate.
Counts and heat are biased CPU samples, not bytes, complete access counts, DRAM
bandwidth or load/store traffic proportions. Use latency includes pipeline delay.
Virtual addresses are grouped by PID; no DIMM/channel/bank/chip mapping is inferred.
GPU/DMA and kernel accesses are outside the configured CPU sampling scope.

Captures use private temporary files, bounded perf buffers, timeouts and automatic
cleanup. Perf warnings/errors are shown. At most 100000 ranges are aggregated;
unknown/zero addresses, unparsed and capped records are reported as ignored.

## Validation and interfaces

```bash
~/global/bin/python -m unittest discover -s ramtop/tests -v
```

Tests cover PMU scaling, sparse encoding, unavailable/reset/multiplexed counters,
permissions, guest-time CPU accounting, DRM deduplication, sample aggregation,
disk and socket interval rates, table sorting, tab navigation, filtering and
narrow terminals. Target-host live checks supplement fixture tests.

- [Linux client IMC aliases](https://github.com/torvalds/linux/blob/master/arch/x86/events/intel/uncore_snb.c)
- [Intel IMC request semantics, Table 3-3](https://www.intel.com/content/dam/www/public/us/en/documents/manuals/6th-gen-core-family-uncore-performance-monitoring-manual.pdf)
- [perf_event_open](https://man7.org/linux/man-pages/man2/perf_event_open.2.html)
- [resctrl monitoring](https://docs.kernel.org/filesystems/resctrl.html)
- [DRM client stats](https://www.kernel.org/doc/html/latest/gpu/drm-usage-stats.html)
- [Linux process I/O accounting](https://docs.kernel.org/filesystems/proc.html)
- [psutil API](https://psutil.io/api/)
- [ss TCP information](https://man7.org/linux/man-pages/man8/ss.8.html)
- [perf memory sampling](https://github.com/torvalds/linux/blob/master/tools/perf/Documentation/perf-mem.txt)

## Built into Swarm Node

The monitor is now a Swarm Node component. `./swarm-node monitor` starts this TUI;
`./swarm-node telemetry` prints a local headless snapshot. `./swarm-node dashboard`
prints the shared dashboard URL, **http://map.local/monitor.html**. The shared view
has Memory, CPU, GPU, Disk, Network and LAN tabs, a node selector, sortable tables,
per-record freshness and aggregate five-minute memory histories.

`./swarm-node build` builds the node image with the terminal monitor, dependency-free
headless runtime, map backend and dashboard assets included. The existing atomic
agent release embeds a deterministic source ZIP inside the checksum-covered agent
source, so a monitor update changes the agent revision. No separate repository or
runtime download is required to collect metrics. Textual/Rich/psutil are required
only for interactive TUI components; the headless collector runs without them.

Busy agents do not need a restart: `swarm-performance.service` is the independently
supervised native monitor component, using the node's existing enrollment identity
and encrypted acknowledged `metrics.publish` transport. Its replay queue is
bounded to 64 snapshots / five minutes, with existing batch-size limits. The normal
agent also carries extended monitor data in its compatible CPU envelope. The
master owns node identity; reported host fields cannot select a different node.

Latest snapshots include bounded process RSS/storage accounting, sockets, GPU
client details, filesystem capacity and passive LAN neighbor inventory. Process
names are shared, not command-line arguments or file contents. Coverage follows
local `/proc` namespace and permissions; limits are reported in each snapshot.
Aggregate memory data enters history; process/address details do not. No address
profiling, active LAN sweeps, permission changes or benchmarks run automatically.
