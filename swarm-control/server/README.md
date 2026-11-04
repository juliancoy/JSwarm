# LAN ecosystem

Open **http://map.local** from the shared LAN. The view starts with the visiting device at the center. Clicking any particle or device-list button switches perspective; **My device** returns to the visitor.

The **Link map** draws ASUS Wi-Fi associations, ASUS LAN membership, this machine's negotiated Ethernet link, and its upstream gateway. Neighbor tables alone do not imply direct physical cables. The **IP hops** layer draws the selected device's UDP TTL trace, preserving unanswered intermediate hops; ASUS is a Layer 2 bridge and is not counted as an IP router. Devices without SSH observations stay selectable. Their attachment may still be verified through the ASUS association table; their own interface and routes remain unmeasured. Dim particles are shared inventory. Source, observation time, access failures, and measured interface are visible in Details. Browser identity comes from its actual connection IP, not a selected device or a client-supplied forwarding header. A proxy or SSH tunnel is identified as the proxy endpoint; select a device explicitly in that case.

## Run

The host collector uses Python's standard library plus `ip`, `ping`, `busctl`, and existing trusted SSH logins. It listens only on `127.0.0.1:8089`. Install and enable its user unit:

```bash
cp swarm-control.service ~/.config/systemd/user/swarm-control.service
systemctl --user daemon-reload
systemctl --user enable --now swarm-control/server
cp lan-map-mdns.service ~/.config/systemd/user/lan-map-mdns.service
systemctl --user daemon-reload
systemctl --user enable --now lan-map-mdns
```

The unit path and network configuration target this machine. In this directory, start the Docker view:

```bash
python3 docker_services.py up
```

The nginx container uses host networking and **read-only bind mounts**: `./view` for HTML/CSS/JS, `GenomeHarbor/web` for genome assets, and `./nginx.conf` for its configuration. Editing view files takes effect on reload without rebuilding. No SSH keys, Docker socket, or credentials are mounted in the container. It has only the `NET_BIND_SERVICE` capability needed for port 80, with a read-only root filesystem. IPv4 binds only `10.42.0.1:80`; IPv6 listens on port 80 with access restricted to link-local sources. nginx overwrites the viewer-address header before proxying to the loopback collector.

Dashboard JSON, scripts, styles, and SVGs use negotiated gzip compression; response fields and retained history are unchanged. Network-map, mobile-card, and website polling pauses while the page is hidden, keeps the last displayed snapshot, and refreshes immediately when visible again. Reload existing pages to load the polling changes. After editing nginx configuration, validate and reload it with `docker exec swarm-control-view nginx -t` and `docker exec swarm-control-view nginx -s reload`.

Docker restarts the view unless stopped. The host collector starts with the user's systemd session; unattended boot before user login is not configured. Health:

```bash
python3 docker_services.py status
systemctl --user status swarm-control/server
curl http://map.local/api/health
```

## Observations and tests

The browser polls every second (five seconds when hidden). Host neighbor scans run every 30 seconds. SSH-capable devices are inspected concurrently every five minutes, or after **Scan network**, using existing batch authentication for `julian` and `wolf` and strict known-host checking. Remote scripts inspect the device's default interface, scan its connected IPv4 subnet (/24 or smaller), sample interface byte counters, and probe an internet IP. They do not change routes, DHCP, DNS, or device settings. Inaccessible devices are explicitly unmeasured. IPv4 and IPv6 observations merge by MAC, preserving IPv6 SSH when IPv4 access is blocked.

The selected observer's RX/TX appears in the inspector. Host counters update continuously; remote counter measurements show a sample timestamp. Moving activity markers are limited to continuously measured host counters. Blue rings show the latest HTTP download benchmark on a 0–1000 Mbps scale, not physical link capacity or internet speed.

**Run web tests** runs HTTP/API and installed headless Chrome checks across accessible devices. Each accessible remote downloads three 4 MiB payloads directly over scoped LAN IPv6; the Chrome smoke test uses a temporary SSH reverse tunnel. Test results and observations live in memory until collector restart. No packages are installed remotely.

Run the perspective evidence tests with `npm test`. No dependencies or build step are required. The UI uses polling rather than P2P: a single collector already owns discovery and SSH observations, so peer signaling would add no useful data path.

`map.local` is advertised by `mdns.py` through the host Avahi daemon on `enp7s0` only. This requires the system Python D-Bus and GLib bindings (present on this machine). Clients need mDNS support; the direct address is http://10.42.0.1. The alias publisher restarts with the user session.


## Edge metrics and hop evidence

The inspector is the panel on the right and follows the selected node. It shows addresses, ports, router association, band, negotiated rates, route observations, and each incident edge's capacity/traffic provenance and timestamp.

An edge's outer width uses a logarithmic scale for its reported link rate. This is a capacity ceiling, not an available-bandwidth estimate. The inner stroke scales with observed throughput and changes from cyan to amber/red as the rate approaches the ceiling. Unknown capacity stays dashed; unknown traffic has no colored fill. Public transit links do not inherit access-link traffic or capacity.

`packet_monitor.py` runs in the `traffic` Docker service with host networking and only `NET_RAW`. It counts frame lengths by peer MAC on `enp7s0`, retains no payload, and writes one-second counters to the ignored `.runtime/` bind mount. These counters cover traffic crossing this gateway, including dashboard activity. They exclude peer-to-peer traffic that remains inside ASUS and are not a physical wire-rate measurement. The frame collector has no SSH keys or router credentials. A stale or missing sample is unknown, not zero.

Router membership and PHY rates refresh every 15 seconds using the read-only ASUS client-list endpoint. Credentials are kept only in `~/.config/swarm-control/server/router.json` (mode 0600), outside the repository and outside Docker mounts. `measurements.py` reads Ethernet/Wi-Fi link rates and runs bounded UDP TTL probes with the Linux ICMP error queue. Trace gaps remain explicit. Remote SSH observations include the same link and route measurements; no remote packages are installed.

The alias publisher also sends scoped unicast IPv4 answers for `map.local` queries received on Ethernet. This avoids relying on downstream Wi-Fi multicast delivery; Avahi continues the normal multicast publication. No other DNS names are answered by that helper.

The default link map includes the gateway’s full observed upstream trace. Route segments are IP-hop evidence, not physical cable claims; unanswered hops stay explicit and transit capacities stay unknown. The canvas starts with minimal node labels; use **Labels** for all labels. Perspective, legend, traffic samples, and link details are in the sidebar.

## On-demand browser benchmark and selection motion

**Run network benchmark** tests this browser's HTTP path to compai, with five warm-connection RTT samples and three 4 MiB transfers per direction. The UI reports median/min/max latency (ms), aggregate application throughput (decimal Mbps), elapsed time, endpoints and timestamp. It includes nginx/browser/server overhead and does not measure internet speed or physical capacity. Running on compai measures a local path. Cancel or the 30-second deadline stops browser requests. The collector admits one session per 45 seconds and caps each session at six pings and three transfers per direction; uploads have size and read deadlines. Reloading loses the displayed result. A busy, unavailable, interrupted or expired test is explicitly reported. No benchmark runs automatically.

Node selection retains existing coordinates, pan and zoom. The selected node gently approaches the center through bounded physics steps; rapid clicks use current positions. Fit is explicit after initial loading. Pause and reduced-motion preferences keep positions still, including when selecting nodes; changing the system preference is handled live.

There is no `build.sh`: the bind-mounted UI is served directly. Validate with `npm test`, `python3 -m unittest discover -s tests`, syntax checks and browser checks; no alternate build is needed.

## Passive outbound destination map

The `traffic` service now listens on the discovered LAN and default-route Ethernet/Wi-Fi interfaces using host networking and `NET_RAW` only. It takes bounded 256-byte header snapshots, immediately discards frame data, and retains only source MAC/IP, destination IP, capture interface, byte counts, timestamps and attribution basis. `recvmsg(MSG_TRUNC)` supplies the original packet length, so snapshot truncation does not undercount accepted IP packets or existing peer frame counters. IPv4 options, IPv6 extension/fragment headers and up to two 802.1Q/802.1ad VLAN tags are parsed defensively. Malformed/truncated headers and IPv6 jumbograms are skipped. No application payload, DNS query, URL, port history or reverse-DNS lookup is retained or performed.

LAN traffic is counted **once on ingress before NAT**, only for unicast frames addressed to the gateway MAC with a source IP on its LAN prefixes. The observed source MAC identifies the LAN sender, not a verified human identity; routers behind that MAC can hide additional devices. Gateway-host egress requires the source interface MAC, a host-assigned IP, and an exact connected TCP/UDP tuple in `/proc/net/{tcp,tcp6,udp,udp6}`. Forwarded packets after NAT are not assigned to compai merely because they carry its upstream IP. Tuple lookup is refreshed every second and bounded to 8,192 entries; unconnected UDP, short-lived sockets, other protocols and fragmented host packets can be missed. Captured LAN fragments still contribute their own IP bytes. No response traffic is inferred. Existing monitor probes can appear as real traffic.

`capture_config.py` publishes local addresses, interface MACs, and directly routed prefixes (including globally addressed IPv6 on-link/delegated prefixes) from host `ip -j` address/routes into ignored `.runtime/capture-config.json`. This refreshes with each inventory scan. Default routes are not local prefixes. Multicast, broadcast, loopback, private/reserved/shared space and all discovered local networks are excluded from internet destinations. Configuration older than 90 seconds disables attribution and exposes a capture error; no stale network assumptions silently continue.

The collector retains **1,024 source/destination pairs**, evicts least recently observed pairs at capacity, and expires each after **five idle minutes**. Byte totals cover the retained lifetime of each pair, not an unlimited history or a rolling five-minute total. Rates are outbound IP bytes over the latest approximately one-second sample, in decimal Mbps; peer counters remain captured Ethernet frame bytes. A zero means no matching packets in that capture interval only. Unseen/unavailable traffic is unknown. Capture is best effort, excludes traffic switched inside ASUS or using other gateways, and may miss packets under load. Counts are observations, not billing totals or physical wire capacity.

The UI displays at most **75 destinations / 200 traffic relationships**, ranked by retained outbound bytes and prioritizing the selected local source or selected retained external destination. Dotted orange-node relationships show observed source → destination IP traffic, **not direct cables, IP-hop measurements, available bandwidth or link capacity**. Actual infrastructure links remain separate. Selecting a destination is a passive graph perspective: no external destination is added to device discovery, SSH observation, web-test or probe target lists. Details show source, interface, attribution, first/last timestamps, retained bytes, latest Mbps and scope. Capture snapshots older than ten seconds, errors, or expired pairs no longer supply current destination relationships. Existing coordinates/pan/zoom are preserved as inventory changes and on selection; reduced-motion behavior is unchanged. A bounded 2,048-entry, five-minute position cache preserves continuity when nodes leave/re-enter the display cap.

## Router custom-firmware analytics

Authenticated read-only inspection of the current router at the private configured address reports **ASUS RT-AX86U**, `firmver=3.0.0.4`, `buildno=388.12`, `extendno=2`, **AP/bridge mode (`sw_mode=3`)**. Firmware capabilities and units were checked against its served `Tools_Sysinfo.asp`, `Main_TrafficMonitor_realtime.asp` and JavaScript. There is no firmware/configuration mutation, shell execution, flashing, reset or installation. Login is the only session action. Credentials stay exclusively in `~/.config/swarm-control/server/router.json` (0600), never in the container, source or API.

Every 15 seconds `router.py` reads bounded, whitelisted telemetry:

- `appGet.cgi?hook=get_clientlist()`: current associations, band, PHY TX/RX Mbps, signal dBm and firmware connection duration. These PHY rates describe negotiated/reported radio rates, not traffic throughput.
- Whitelisted individual `nvram_get()` fields: model, firmware version, mode and LAN MAC, cached for five minutes.
- `ajax_sysinfo.asp`: load averages over 1/5/15 minutes (not CPU percent), memory total/used/available/free in the firmware's labeled MB, and router conntrack/active counts. AP conntrack counts do not represent all LAN sessions.
- `ajax_coretmp.asp`: numeric CPU and available radio temperatures in °C. Disabled sensors stay unknown.
- `appGet.cgi?hook=netdev()`: aggregate BRIDGE/WIRED/WIRELESS0/WIRELESS1 byte counters and measured deltas in decimal Mbps, from router RX/TX perspective. First samples, resets and gaps over 60 seconds have unknown rates. These groups overlap: **never sum them or add them to gateway capture**. They can reveal aggregate activity not visible at compai, but cannot attribute switched traffic to a client/destination.
- `ajax_conntrack.asp`: the current firmware exposes empty connection and route arrays in AP mode. `get_clientlist()` totalTx/totalRx fields are also empty; no per-client byte totals or router destination records are invented. `get_ethernet_ports()` exposed no data during inspection; no Ethernet PHY capacity is inferred. WAN router service/health/capacity is not applicable in AP mode; compai is the internet gateway.

Select **ASUS access point** for firmware health, counters, sources and timestamps; select a client for association/rate/signal/duration. Values older than 45 seconds are marked stale; endpoint failures are exposed without discarding other successful telemetry. Firmware JavaScript responses are parsed only as whitelisted numeric arrays/assignments, never evaluated. Router metrics are separate from physical graph capacity and gateway-derived destination edges.

Validation: `npm test`, `python3 -m unittest discover -s tests`, Python/JavaScript syntax checks, and live API/browser checks. No `build.sh` exists; this project serves bind-mounted source directly, so no alternate build command is used. Restart `swarm-control/server` for Python server/router changes and `python3 docker_services.py up traffic --recreate` for collector changes; view edits take effect on reload without a container recreate.


Device history is retained in `.runtime/device-history.sqlite3` on the host and survives
collector/container restarts. It records MAC identities, Unicode names and aliases with
source/timestamps, IP addresses, previously responding ports, and last confirmed reachability
or ASUS association. Offline ASUS client records and the firmware remembered-device database
are imported; the latter refreshes every five minutes. “First recorded” is when this collector
first obtained evidence, not the original connection date. Cached neighbors and remembered
clients do not advance confirmed reachability. Private/randomized MAC addresses remain separate
records. History is searchable in the dashboard and retained without automatic expiry; it does
not create physical links or claim that remembered devices are currently connected. Back up the
SQLite file using SQLite's backup API if the collector is running.

The **Geographic map** checkbox pins particles to an offline Natural Earth land map
(equirectangular projection). Public-IP estimates are looked up over HTTPS through
https://ipwho.is/ only when the checkbox is enabled, at one request per second with
a maximum of 100 public addresses per refresh. Successful lookups are cached for
seven days in `.runtime/geography.json`; failures retry after an hour. Private,
link-local and multicast addresses are never submitted. LAN MAC identities share
the gateway's approximate public-IP site; this is not individual device GPS.
Unlocated nodes remain in the device list and are not drawn on the geographic canvas;
connections involving an unlocated endpoint are omitted from that view. Colocated
nodes keep their real projected anchor; stable pixel offsets and collision packing
separate the visible node rings without changing geographic coordinates. Tethers
show any displacement from the anchor. Geographic zoom now reaches 64×, with
Zoom + / Zoom − controls for touch and keyboard use. Fit uses only world-map bounds, never unlocated nodes. Location source and
observation time appear in the inspector. Geographic mode pins positions even
when animation is paused; pan, zoom, node selection and traffic styling remain.
Land geometry: Natural Earth `ne_110m_land.geojson`, public domain, downloaded from
https://github.com/nvkelso/natural-earth-vector/tree/master/geojson .

Selecting a remote IP starts an asynchronous public-IP identity lookup. The inspector
shows reverse hostname (or explicit resolver failure/no result), network operator,
ASN, ISP/network domain, approximate city/region/country, sources, and lookup time.
Resolved operators/hostnames label particles and can be searched alongside the raw
IP. A network domain is not attributed as the visited website: CDN/proxy addresses
can serve many domains. Results persist in `.runtime/remote-ips.json`; successful
network lookups are cached seven days, failures one hour. Requests are queued and
paced one second apart, with at most 100 pending. Private IPs are rejected. Reverse DNS uses bounded `dig -x` PTR queries through the configured DNS resolver;
network metadata comes from HTTPS ipwho.is. Observed remote destinations and route
hops are queued automatically, without selecting each node. Successful PTR answers
refresh at their TTL (bounded to 30 seconds–24 hours); missing records retry after
an hour, DNS failures after a minute. The inspector shows all PTR names, DNS status,
resolver, command and TTL/retry interval. Network-owner metadata is cached separately
for seven days and is never presented as a DNS hostname.


Website request analysis for CodeCollective and LifeTech is available at
`https://orgportal.cc/admin?tab=stats`. See [WEBSITE-OBSERVATORY.md](WEBSITE-OBSERVATORY.md)
for evidence semantics, retention, hourly collection, runtime dependencies and the
path to server/edge/real-user telemetry.


**Group providers** is available on the LAN map and website observatory. LAN remote
endpoints group using verified ASN metadata or explicit DNS/domain suffix families
(Google, Cloudflare, Facebook/Meta, Apple, Microsoft, Amazon/AWS, Canonical), with
exact network-domain/ASN fallback. LAN devices stay separate. Website origins group
by explicit domain families, not a hosting provider's IP ownership. Group inspectors
retain member endpoints/origins and traffic totals; rendered links deduplicate
unordered pairs while retaining underlying relationship evidence. Multiple
geographic locations are not collapsed onto an invented provider coordinate.
Toggle grouping off to inspect the original individual nodes.

Map controls, DNS visibility/status, legend, benchmarks and physics now live in the
left settings column; the selected device, device list and history remain on the
right. View settings are saved under `lan-map.settings.v1` in browser localStorage:
topology layer, geographic mode, provider grouping, additional names, pause choice,
automatic remote lookup, and repulsion. Preferences are per browser origin, so
`map.local` and the numeric host address have separate settings. Reduced-motion
preferences remain respected. Corrupt/blocked storage falls back to session defaults.
The DNS summary distinguishes IPv6 PTR names, no-PTR responses and pending/failing
lookups. Grouping hides member endpoint labels by design. Automatic lookup queues
unique active destinations rather than the first 100 source/destination pairs;
one lookup/cache-write failure cannot terminate the resolver worker. Pausing lookup
only stops requests from this browser; other viewers can still request resolution.

The distributed tests page includes **Device levels**, an overlaid live chart with
Network, Disk, and RAM choices. Colors follow device identity across views;
legend buttons toggle individual traces. The metric choice persists locally.
The chart builds up to five minutes of in-tab history and leaves missing samples
as gaps. Network displays receive + send Mb/s: saved SSH hosts report totals
across physical interfaces, while other LAN devices use gateway-observed Ethernet
frames. These scopes differ and must not be summed. Disk displays the busiest
physical block device's Linux I/O busy percentage, not storage space or an
inferred saturation score. RAM displays `(MemTotal - MemAvailable) / MemTotal`.
Disk and RAM require a reachable saved SSH account. Unmeasured devices remain
listed, and samples older than 15 seconds are unavailable.

**Where throughput comes from** on `/tests.html` reports bidirectional Internet
flow accounting at compai. Download rankings group remote source IPs; upload
rankings group local devices. Direction and device filters apply before share
calculation. The table lists source/destination addresses, transport protocol and
ports, current IP Mb/s, share of the filtered observed traffic, retained bytes,
interface, observation method, and last seen. PTR and ASN labels describe network
ownership, never verified application identity. Rates use the latest capture
interval (usually one second), refreshed in the browser every three seconds.

LAN uploads are attributed at ingress before NAT and downloads at LAN egress
after NAT. Host packets require an exact connected kernel socket tuple in the
appropriate direction, excluding forwarded traffic. The bounded bidirectional
cache retains at most 1,024 records, expires after five minutes idle, and exposes
record evictions. Fragmented/non-TCP/UDP traffic can lack ports. Retained bytes
cover each record's lifetime, not a chart window. Kernel packet-socket drops are
reported per interval and since collector start; zero drops do not establish
complete visibility. Other gateways, switched LAN traffic, unconnected host UDP,
and sockets absent from the quarter-second socket inventory remain outside coverage.
No packet payloads are stored. The presentation follows IPFIX observation-point
and flow-key conventions (RFC 7011) without claiming to implement IPFIX export.

The source ranking can also group by **Host process**. A host-side `ss` inventory
joins recent exact socket tuples to process name and PID on compai. It returns only executable/script identity, never full command arguments,
environments, or remote process inventories. Unattributed
traffic remains in the denominator, and sockets with multiple owners are counted
once as a shared group. Ownership snapshots refresh every half second and are
not proof of ownership for all retained historical bytes. Grouping, direction,
and device filters persist in browser localStorage.


Process attribution now includes an interpreter's script filename (for example,
`recovery.py`) and a **Local files & database destinations** section. A bounded
snapshot of `/proc/PID/fd` and `fdinfo` reports unique regular-file paths and
permitted read/write access, prioritizing database filename hints. It does not
read file contents or assign network bytes to individual files. Read/write
access is capability, not observed I/O. Snapshots include at most 32 files per
process, scan at most 2,048 descriptors and cover at most 64 host processes with
visible connected sockets. Truncation and inaccessible descriptors are explicit.

SQLite/database suffixes are filename hints, not format verification. Connected
TCP endpoints on known database ports show address, port, an inferred engine,
and local/remote service role. Database names remain unknown. No SQL, file
contents, connection strings, credentials, or remaining command arguments are
returned. Resource context is available on compai; remote file names and
socket-to-file or socket-to-database-name mappings require application
instrumentation. File context refreshes separately every three seconds and expires after
ten seconds. Opening a process card reveals its file and database details.

### Distributed image preparation

Before dispatching suites, the coordinator refreshes each required registry
reference once and rebuilds derived images against the prepared base IDs.
Matching worker image IDs skip transfers. Changed images use checksummed encrypted agent uploads, with two destinations at a time.
Each worker records and executes its verified local image ID with `--pull=never`.
Image preparation is cancelled by **Stop all tests**.

Preparing images does not run a suite; stopped runs remain stopped. These changes
apply on the next explicitly requested run after the coordinator loads this code.

### Live container website view

On the main tests page, select a machine, open a running container, and choose
**Website** beside **Console**. Publicly bound web ports appear in the port
selector. The embedded website remains live and interactive while machine
observations refresh; **Reload** refreshes only the website. **Open in new tab**
is available for sites that disallow embedding or need browser sign-in.

A custom HTTP/HTTPS website or gateway URL can be entered for services reached
through another container's gateway. Loopback-only bindings are identified rather
than presented as reachable LAN endpoints. Containers without a published web
endpoint still offer their console and a custom URL field. Selecting Website
does not start a server, container, test, or deployment.

### Cross-device WebRTC tests

`Start all tests` includes a coordinated **Cross-device WebRTC P2P** suite alongside
application file jobs. It starts as soon as its browser images are ready. It requires at least two distinct authenticated agent
machine identities. Each participant runs a disposable, non-root browser container with
all Linux capabilities dropped. This dedicated suite uses the host network for
real ICE connectivity; ordinary application test containers remain offline.
Only public SDK source is mounted. The coordinator keeps the enrollment signing
key and sends browsers short-lived invitations for a synthetic dataset.

The suite checks SHA-256 of received bytes, the selected ICE candidate pair and
physical-interface addresses, and RTCDataChannel byte counters. Loopback,
hidden/unverifiable routes, duplicate machine identities, and TURN relay paths
cannot satisfy the direct-P2P check. It also checks reseeding with the original
sender disconnected, repairing a missing cached chunk, and WASM consumption of
received fixtures. Cross-NAT and TURN coverage are reported separately; a LAN
pass does not establish either. Device clocks must be synchronized within one
minute so expiring invitations have consistent semantics.

The network suite runs through Start so SDK and fixture sources use the same published Organization commit as application tests.

Reports contain machine fingerprints, tested SDK hash, fixture hash, selected
routes and byte counts; invitations and signing keys are excluded. Normal
integrated reports follow the distributed runner's retention policy. A manually
chosen report remains until its operator removes it.

Browser images use the same refresh, content verification, and encrypted
agent delivery as application images. The SDK transfer is a small source-only bundle, separate
from container images. Browsers execute the verified local image ID with
`--pull=never`; Docker's ordinary image store remains necessary.

The coordinator participates through its own enrolled agent, using the same readiness checks and job protocol as other machines.

Distributed-run preparation verifies enrolled launcher agents, excludes unavailable machines and redistributes suites. Start updates outdated enrolled agents through saved SSH access; initial enrollment remains a separate operation.

The API loads current coordinator modules on each new start through `live_distributed.py`; active runs retain their implementation. Agent process code changes only through explicit installed releases.

The Network level chart includes **Docker images** operation labels. Exact image
references from the test coordinator distinguish registry pulls, LAN image
transfers, builds, and cached/ready images. Historical test selections do not
appear as current coordinator activity, and coordinator labels expire if live
polling stops. A read-only `DockerDownloads` worker also observes explicit local
`docker pull` / `docker image pull` command references and recent image pull
events from the local Engine socket. It never initiates a pull or reads registry
authentication. Event history is in memory for 15 minutes, capped at 64 records;
Docker itself retains only a bounded event history. Command observations mean a
pull CLI is active, not that bytes are moving, and ambient client daemon contexts
are not verified. Explicit alternate contexts are excluded. Compose, BuildKit,
API-initiated pulls and remote hosts may be absent unless the coordinator reports
them. The chart's device rate includes all traffic: no per-image Mb/s or layer
progress is invented. General host observation requires loading the updated
collector; coordinator labels use the existing test-context feed.

The distributed-tests page embeds the live particle map above execution details. It shows all discovered LAN devices, including inventory without SSH access, with a node count, searchable device inspector, and fit/zoom/pause controls. External traffic destinations stay in the full network map. The embedded view uses the same discovery feed and does not start tests or scans.

Each distributed run refreshes every required registry image once on the
coordinator before distributing it, and rebuilds the derived toolchain from its declared bases. Docker reuses unchanged layers;
unchanged peer contents are not retransmitted. A registry refresh failure stops
preparation rather than silently testing an old tag. Version and digest pins
remain explicit; refreshing does not upgrade a pinned version to another release.

Start commits and pushes the selected source repositories (Organization, OrgPortal and PIdP) on their existing branches, verifies the GitHub branch tips, and records their exact commit IDs. It uses only fast-forward reconciliation; divergence, rejected pushes, unresolved conflicts and sensitive/runtime files stop publication with an explicit error. Repositories publish independently: a later failure does not undo an earlier successful commit/push.

Every verified local worker initializes fresh checkouts, fetches the three pinned commits directly from GitHub, verifies FETCH_HEAD and HEAD, and records per-repository synchronization events. Existing working copies are untouched. No GitHub-hosted runner or SSH execution path participates. Private repositories require independent worker-side GitHub access; coordinator credentials are never copied into jobs. Test failures are reported, not automatically edited or committed as repairs. The worker verifies its daemon can read the exact run manifest through its host-local bind mount before executing suites.

Map edges render up to two minutes of in-tab throughput history along the link.
Each trace auto-scales independently; its hover card reports current rate, recent
peak, and measurement scope. Endpoint relationships use matched bidirectional IP
flows when available; other links keep their actual interface measurement scope.
Unknown or stale data leaves gaps, not zero-rate samples. Time has a stable
orientation based on endpoint identity. History is bounded and disappears when
this tab closes. Hover an edge or tap it to inspect; Escape dismisses the card.

Hover download context ranks matched flows by current download rate. Where a
recent socket owner is known, its process/script and open writable filenames are
shown as receiver context, with database endpoint hints where available. These
are not proven per-flow file destinations. Concurrent Docker image operations
appear only for relevant host/interface context or a Docker-owned socket, and
are explicitly not assigned a per-image rate. The map reads existing coordinator
observations; it does not start tests or network benchmarks.

**All observed connections** switches to a topology-independent schematic view.
It shows all retained captured endpoint pairs, including incoming-only flows,
without the normal 75-destination / 200-relationship display cutoffs or the
selected perspective's connected-component filter. Multiple sockets between the
same device and remote IP remain one graph edge, with summed upload/download
rates. Provider grouping remains optional. The choice persists in localStorage.
Geographic and IP-hop layouts are paused while this mode is active, so unlocated
endpoints and disconnected observed components stay visible. This means all
available observations, not every physical or switched connection: collector
retention, its flow limit, socket visibility and capture coverage still apply.

Run history displays the latest ten independent records, including per-machine suites and bounded logs. Selecting a run reads that run ID and never replaces the latest run. Existing retention remains 30 days/up to 100 saved records; limiting the visible history to ten does not delete older records early.

### Agent image distribution and package caches

Distributed tests reuse worker images by immutable ID and transfer changed images through the authenticated agent API. Temporary image archives are removed after import. At most two destinations receive an image concurrently. Package caches remain separate from installed dependencies.

Workers retain npm/pip download caches under
`~/.cache/organization-tests/packages/`, separated by suite, checkout lockfiles,
image ID, and CPU architecture. Installed dependencies remain isolated in each
run's source directory; npm still uses `ci`. Cache files stay on their owning
Docker host and are mounted into that host's containers. Removing the package
cache directory while no tests are running forces fresh package downloads.
Registry storage and package caches persist independently of the 30-day reports.

Loopback registry handling follows the [Docker daemon registry policy](https://docs.docker.com/reference/cli/dockerd/#insecure-registries).


Download attribution now keeps bounded socket-owner observation windows rather
than only the latest `ss` output. Ownership is sampled every 0.5 seconds,
independently of the three-second open-file scan. The packet collector refreshes
its connected socket inventory every 0.25 seconds. A five-minute, in-memory
ownership history (4,096 tuples, up to four identity windows each) lets a closed
connection retain its observed process/script label while its flow is retained.

Matches require the exact protocol/address/port tuple and an ownership window
within 1.5 seconds of the last captured packet. This is temporal correlation,
not a kernel event joining every packet to a process. Conflicting identities
are **ambiguous**, never arbitrarily assigned. Process start ticks distinguish
PID reuse and prevent joining a retained owner to a new process's open files.
Observations without a readable process start identity are excluded. Labels
explicitly distinguish **live**, **retained**, **ambiguous**, and **unknown**
evidence; they do not attribute the entire historical byte count to one owner.

The throughput page reports the fraction of *observed compai download rate*
with a process match. Its denominator excludes remote devices and unseen traffic,
so 100% is not proof of complete network visibility. Resource filenames remain
current process context; exact per-file, image, or database-name attribution
requires downloader/application transfer events or equivalent instrumentation.
These source changes require reloading the collector and packet monitor to take
effect; neither is automatically restarted by editing the files.

The existing **Run history** panel on `/tests.html` now includes a saved-reason
preview on each of its ten run cards and a **Why this run ended** explanation for
the selected run. It shows the recorded coordinator or host error first, groups
matching suite errors, includes phase/exit status, source revision and snapshot,
and provides bounded diagnostic excerpts with direct navigation to recorded
suite output. Infrastructure shutdowns without suite-specific error evidence
are counted as blocked work, separately from suite failures, unexecuted jobs,
timeouts, unavailable workers and deliberate stops. The last image preparation
operation is context, not an inferred cause. Missing logs remain explicitly
unknown; no diagnosis is invented from an empty log.

Reason previews read existing saved reports, at most two concurrently, and cache
summaries for the ten displayed runs. They do not start or rerun any tests and do
not require a collector API change. Recorded terminal output includes captured
failure details as well as its bounded log tail. Historical unfinished job
statuses use the run's saved end/update time for duration, not the current clock.

### Authenticated launcher agents

To add one Linux Docker host without starting the application stack, run from
the coordinator:

```sh
./onboard_node.sh julian@julian-inspiron-3793.local
# If mDNS is IPv6-only, use the discovered scoped address for SSH:
./onboard_node.sh julian@julian-inspiron-3793.local --address 'fe80::c23e:baff:fe12:47aa%enp7s0'
```

The host must already trust the coordinator's SSH key and appear in its known
hosts. Onboarding verifies the remote hostname, Docker, physical LAN address and
authenticated agent machine identity, preferring a wired LAN address. It retains the fleet key and existing
members, saves the SSH account, installs Python dependencies in a private venv,
and enables the `organization-test-agent` user service and login lingering.
The supervised agent uses the host's selected local Docker socket, including
rootless Docker, while observing real host interfaces. It restarts on failure
and starts after reboot without an interactive login.
It uses `~/.local/share/organization-node` on the worker and never starts the
application stack. Existing online peers receive the updated membership;
offline peers stay enrolled and are reported for a later onboarding retry.
Inspect worker runtime output with
`journalctl --user -u organization-test-agent.service`.
The next distributed run includes the enrolled node automatically.

Routine distributed runs use the existing launcher's encrypted `lifetech-agent.v1` API on TCP 8790, discovered through `_lifetech._tcp.local.` DNS-SD/mDNS (UDP 5353). Announcements contain only node/protocol metadata. They never enroll or authenticate a device. Missing multicast announcements can use an already-enrolled address, followed by the same authenticated readiness check. The coordinator requires its enrolled address on `enp7s0` and only schedules endpoints on that Ethernet subnet.

The enrollment file supplies the fleet credential and enrolled addresses. This is fleet-level trust: possession of that credential grants agent execution control. Requests and responses use AES-GCM, protocol-specific authenticated data, request IDs, freshness limits and replay rejection. There is no shell-command endpoint or routine SSH fallback. SSH remains in the explicit enrollment/provisioning workflow and unrelated network monitoring collectors.

Start checks the running source revision, Docker availability, physical addresses, machine fingerprint, capabilities and busy state. Current agents are reused. Outdated agents are updated before readiness verification. Offline, duplicate, failed-update or incapable agents are excluded with a stage/reason; application suites are reassigned to surviving verified agents. Readiness is checked again after image preparation. Successful remaining work with exclusions is **Partial coverage**, not a full-fleet pass. One worker can run application suites; multi-device P2P remains explicitly unmeasured.

Source code is fetched directly from GitHub by exact commit; image archives and synthetic P2P fixtures use bounded, ordered 1 MiB encrypted chunks and SHA-256 verification. Agents reject unsafe archive paths/links, enforce staging limits and available disk space, and execute immutable worker image IDs. Jobs have run IDs, sequence-numbered event polling, cancellation acknowledgements and a two-hour stop deadline. An event-history gap cannot produce a verified result. Agent restarts mark running work interrupted and clean up owned test containers. Reports distinguish infrastructure unavailability, excluded machines, missing coverage and suite failures.

A run shares one synchronized image resolver across application and P2P preparation. Each image reference refreshes once per run, including concurrent callers. Unchanged image contents reuse worker-local IDs. Changed images publish once to the coordinator's persistent loopback registry; worker Docker daemons request only missing compressed layers through a worker-loopback registry bridge. Registry bytes traverse authenticated AES-GCM agent RPC with bounded chunks and immutable manifest/blob digests. There are no SSH tunnels, LAN registry listeners, Docker daemon configuration changes or full-image transfer archives in this path. Two destination transfers may run concurrently; those are distinct worker copies, not independent internet pulls. P2P browser setup, WebDriver control, fixture delivery and cleanup use the agent API. Application containers are not restarted by Start.

Start automatically stages outdated agent sources over existing SSH access (locally for the coordinator), verifies the enrolled account and physical machine identity, and waits up to two minutes for the expected running revision. Current agents are reused; active jobs are not interrupted. Update progress and failures appear in the run record. For a manual update on an enrolled machine, `python3 scripts/launcher_update.py` atomically stages the current peer-agent sources; `--rollback` selects the previous installed release. The supervisor drains new jobs, waits for active jobs/browsers to finish, validates source checksums, and restores the previous release if the replacement fails its startup readiness check. An unchanged installed release does not reload. Updates to image dependencies or the supervisor itself require an explicit launcher-image rebuild/recreation. Initial activation of this migration also requires that rebuild on each machine; editing the coordinator files alone cannot upgrade existing agents.

### Commit, push and test

The Start control is labeled **Commit, push & test** because it publishes local changes before testing. The run report shows each repository's publication phase and commit ID; each machine shows its fetched/verified revisions. Authentication, push rejection and history divergence are preparation errors; a worker GitHub-fetch failure is a machine error while other workers continue. Outdated or unavailable agents are excluded before scheduling. A second readiness check after publication redistributes suites if a worker disappears. No publication, test or deployment happens simply by editing these files.

The distributed master exposes authenticated REST at `/api/v1/master` and MCP Streamable HTTP at `/mcp`. See [MASTER_API.md](MASTER_API.md) for Codex configuration, token setup, operation semantics and the OpenAPI endpoint. Starting via either interface has the same GitHub publication and test effects as the dashboard.

## Change-aware suite reuse

New runs fingerprint each suite's relevant committed source trees, dependency manifests, fixed suite commands, image contents, agent revision and physical worker identity. Input boundaries are explicit in `suite_cache.py`; cross-worker P2P fingerprints also include the complete eligible participant group and LAN addresses. Unrelated commits outside a suite's inputs do not invalidate its passing baseline. Deleted files, changed lockfiles, runner changes, image changes, a new machine, or missing/unverifiable inputs cause execution.

Only a recorded actual pass with the same fingerprint is reused. A newer executed failure blocks an older matching pass. Reuse is labeled **Unchanged · reused**, records `reused_from` with the original run ID/machine, and starts neither dependency preparation nor the suite command. Fresh pass counts remain separate from reused counts; prior logs and test counts are not fabricated into the new run. Original records remain independent. Baselines expire under normal run-record retention; without one, the suite executes. P2P reuse means historical coverage for an unchanged participant/input set, not a fresh measurement of live internet reachability.

This scheduling decision happens after source publication and image/agent verification, so preparation may still perform network work even when suites are reused. An already-running run keeps its pinned implementation; this policy applies to subsequent runs. The first run with fingerprints creates passing baselines for later reuse.

Worker preparation is independent: after shared source publication and initial identity verification, each machine receives its required images and starts its ordinary suites as soon as it is ready. A slow or failed transfer on another machine does not block it. Shared image refresh/publication happens once; delivery runs outside that image lock. Cross-machine P2P prepares its browsers concurrently with ordinary workers and does not wait for application jobs to finish.

**Direct traffic (ignore hops)** is available on the full LAN map and the embedded distributed-tests map. It draws every retained observed source/destination relationship directly, including incoming-only flows, with intermediate route segments and provider grouping hidden. The preference is saved in the browser and honored by both views. Remote endpoints remain visible in the embedded map when this mode is enabled. It changes presentation, not packet capture coverage or physical connectivity.

### Availability-based file scheduling

`work_queue.py` discovers tracked test files from the published Git commits. Each
file is one independent job; typecheck/build/bundle commands are separate jobs.
Ordinary checks run once across the fleet, including the coordinator, rather than
repeating suites on every machine. Only WebRTC remains a coordinated fleet check.

Each machine keeps one agent session and checkout for the run. Dependency installs
run in one background installer per worker and are reused by later files. Dependency-free
components become ready first. The master only assigns files whose dependencies are
ready, so installation waits do not consume test slots. It reserves 1 CPU and
1536 MiB for the installer, plus 2 CPUs and 2048 MiB while P2P is active, releasing
each reservation when that activity finishes. Per-component installation durations
are recorded on each host as `dependency_preparation`. The master feeds the
next fitting jobs whenever CPU and memory reservations are released. Recent file
execution times prioritize long jobs; there is no permanent suite assignment or
fleet-wide preparation barrier. CPU reservations match container limits; memory
budgets leave 1 GiB available for host services. Resource limits and indivisible
build/browser steps mean continuous 100% measured CPU usage is not guaranteed.

`job.feed` accepts only identifiers in the pinned catalog, is idempotent per job,
and closes the session explicitly once its work is complete. Stopping a run stops
its persistent sessions and owned containers. Worker loss marks dispatched work
unavailable while other workers continue claiming unassigned files. An exhausted
fleet or a task that fits no worker fails explicitly instead of hanging.

The dashboard and REST/MCP records expose pending tasks, per-machine capacity,
active file IDs, individual timings and results. Per-file reruns use the same
application-owned commands and dependency setup. No suite is split by a fixed
machine count. Start applies `launcher_update.py` automatically when an enrolled
agent revision differs. Editing source alone does not update agents or start tests.


### Post-deployment checks

A passing distributed run starts a separate background verification stage, recorded as `post_deployment` in that run and shown in Run activity. It waits up to 10 minutes for the tested OrgPortal commit to appear on both frontend and proxied API health endpoints, then checks the frontend entry asset. It never deploys. A missing release is `deployment_not_observed`, not a passing smoke test. Collector shutdown interrupts this in-process watcher; it does not automatically repeat email sends after restart.

Optional configuration: `~/.config/map-master/post-deployment.json` (keep private):

```json
{"origin":"https://orgportal-web.jcloiacon.workers.dev","email":{"campaign_id":"DEDICATED_TEST_CAMPAIGN_ID","token_file":"/absolute/path/to/private-organizer-bearer-token"}}
```

Email configuration enables one call per run to the existing authenticated campaign `/test` endpoint, defaulting to the connected sender's own mailbox. No audience-send endpoint is used. It polls the newly created test delivery for up to three minutes. Provider acceptance is checked; inbox arrival is not measured. Missing email configuration produces partial coverage with an explicit blocked email check. The bearer token stays on the coordinator and is never sent to test workers or included in run records. Post-deployment results are separate from isolated suite results; an overall suite pass does not imply deployment or email delivery passed.

### Portal layout renders

The `visual` suite runs OrgPortal's offline synthetic layout renderer in the
existing pinned Playwright image. Its contact sheets and individual PNG/JSON
artifacts appear under the suite's run details. Only loopback fixture traffic is
needed; test-phase Docker networking remains disabled. No real user data or
credentials are used. Transfers use 8 KiB chunks with SHA-256 verification,
4 MiB per file, 32 MiB per host/suite and at most 64 files. Artifacts share the
run retention policy. Successful rendering still requires human visual review.
See `OrgPortal/docs/LAYOUT_REVIEW.md` for direct visual review without starting
the distributed workflow.

### Worker Git object cache

Agent jobs retain a bare cache per GitHub URL under the agent job root's
`git-cache/` directory. Existing run checkouts can seed missing commits locally;
only commits absent from both are fetched from GitHub. The exact pinned commit
is verified, and per-repository file locks serialize cache updates. Each run
gets a fresh detached checkout with its own index and refs; immutable Git objects
are hardlinked locally where supported, with no external object alternates.
Deleting an old run does not invalidate another checkout. Cache directories are
independent of per-run retention.

A shard always checks out Organization for the runner. OrgPortal is fetched only
for organization/chat/web/visual suites; PIdP only for identity suites. Root-only
monitor/map/workspace work does not fetch either application repository. The full
pinned source manifest remains available for build metadata and provenance.
Source events record `cache_hit` and whether an existing run seeded the cache.

### Live coordination and background snapshots

The master keeps one encrypted HTTP/1.1 connection to each active worker. Event
requests wait on an agent condition variable and return as soon as events arrive;
20-second empty replies are heartbeats. Dispatch no longer sleeps between polls.
Transient transport failures reconnect and resume from the acknowledged sequence;
mutations are not retried automatically and missing events still fail the run.

The master watches committed HEADs every five seconds and requires two stable
observations before staging. It never commits, pushes, or copies uncommitted edits
in this background path. Each enrolled worker fetches exact GitHub commits into a
separate checkout. Unpublished commits produce a visible preparation error and a
bounded retry. Sources can fetch while a test session is active; dependency
installation waits until that machine's tests and P2P browsers are idle. Preparation
uses installed images only, leaving image delivery to the normal LAN-first path.

Successful preparation records the source digest, image IDs and installed package
trees. A matching run atomically consumes that isolated checkout once; mismatches
use normal fresh setup. Active checkouts are never edited by the watcher. Agents
without preparation support only cache sources and never fall through into tests.
Start can cancel background preparation when it needs to update the launcher.

The dashboard's Next snapshot section and REST/MCP status expose per-worker staging
state, revisions, missing images and errors. These changes require loading the new
master code and the launcher update applied by Start. Editing source alone starts
neither preparation nor a test run.

### Attached Pixel 3 browser worker

The coordinator discovers an ADB-authorized Pixel 3 over USB. Its device card is
visible on the network map and the distributed-tests page, with connection state,
last result, screenshot preview, and **Run Pixel tests**. It has one browser slot;
Docker build and backend jobs stay on the computer workers.

A full run includes the connected Pixel automatically. To request only the phone,
use the device button, REST `POST /api/v1/master/test-requests` with
`{"suites":["mobile"],"force":true}`, or MCP `request_tests` with those arguments.
These use the existing commit-and-push request queue. An explicitly requested
mobile run fails if the phone is absent; an ordinary full run without the phone
continues with the computer fleet.

The coordinator exports the published OrgPortal commit into an isolated directory,
installs locked frontend dependencies in a Bookworm Node container, and serves
synthetic application fixtures through a temporary USB reverse connection.
Playwright controls actual Android Chrome through a local ADB forward. The initial
checks cover organization, people and event screens, application links, viewport
overflow, and a touch gesture opening a submission. This is real-device browser
coverage; it does not yet cover interactive identity login or mobile WebRTC.

The run owns a new Chrome tab and closes it afterward. It removes its USB forwards
and restores the prior keep-awake setting. Screenshots, per-check durations and a
JSON report are retained with the run; the device card links to that exact run.


### Launcher participation and remote jobs

Open `http://127.0.0.1:8791/launcher` on an enrolled machine (or `./run.py --swarm`).
The loopback page shows enrollment and live WebRTC channels, submits authenticated
focused test requests, and controls worker participation. Choose Automatic (AC
power only), Always accept jobs, or Submit only. `./run.py --worker-mode
{auto,always,paused}` sets the same persistent local policy without restarting.
New jobs, source preparation, image pulls and P2P browsers obey this policy;
already accepted work finishes. Battery state is read from Linux power-supply
telemetry. An unreadable policy declines new work. Submission stays available.

The master accepts `tests.request` and `tests.status` through the existing
AES-GCM agent protocol. Only its private launcher config enables `master_api`,
and its REST credential stays in `master-api-token` beside that config, never on
client laptops. This is a separate scoped launcher token: only status and test
requests are permitted. It cannot control Codex or administer runs. Worker
execution and file mutations additionally require an Ed25519 controller signature;
the private key stays in `~/.config/map-master/controller-signing.key` on the
controller. SSH-authenticated launcher updates pin only the public key. Unknown
operations fail closed. Keep the private key out of launcher mounts and snapshots.

Enrolled internet nodes map fleet names to registration peer IDs in
`internet_nodes`. The loopback port 8791 gateway tries the enrolled LAN endpoint
first, then WebRTC on connection failure. It does not retry ambiguous completed
writes. WebRTC transports the same encrypted requests with nonce, freshness,
identity and replay checks; chunk assembly, concurrency and response sizes are
bounded. Registry layer downloads also use this gateway. Signaling and job
execution remain separate: registration alone is not proof of a working channel.
The local Test WebRTC path control explicitly bypasses LAN HTTP.

The current signaling origin is configurable per enrollment; this integration
uses no proprietary VPN client. Internet reachability and ICE connectivity are
required. Restrictive NATs may require a standard TURN relay; the deployed
signaling service currently reports no configured TURN relay. Ethernet plus
Xfinity Wi-Fi can be used together: keep the test LAN route on Ethernet and the
internet default route on Wi-Fi. A captive hotspot sign-in is still required.

### Aggregate progress and result caching

`http://map.local/tests` redirects to the test dashboard with query selections
preserved. Its top progress panel always follows the latest run across all
machines, even while older results are selected below. It counts logical jobs
once across reassignment attempts, includes unassigned work in the denominator,
and separates passed, reused, failed, active, queued, unavailable and stopped
work. Preparation stays indeterminate until the task plan is known. Elapsed time
and the last observed change in job output/state are shown without inferring that
a quiet process is stuck.

Run reports are saved atomically on disk independently of the browser. The UI
shares completed historical responses in memory for five minutes (at most ten
results); live runs and pending post-deployment checks retain short polling.
The selected historical response never supplies current run-control state or
replaces the live aggregate. Explicit reload bypasses the application cache.
Execution reuse remains based on verified passing results with matching relevant
source, dependencies, image contents and machine identity. A forced run bypasses
reuse and is labeled as such in the aggregate.

### Swarm Control launcher delivery through PIdP

Current WAN access is PIdP-only. `/pair` and WAN WebRTC offers are retired;
legacy pairing sessions and owner bearer tokens cannot authorize remote access.
Download the Android launcher through authenticated HTTPS. The following older
WebRTC delivery description is retained as historical implementation context:

### Retired paired-console WebRTC delivery

`https://compai.lifetech.fyi/swarm-control` provides a paired-owner-authorized
WebRTC connection to receive the fixed Swarm Control APK, verify SHA-256, and
hand it to Android's normal installer. The console home links the same controls.
An explicit HTTPS download remains available when a direct channel cannot form;
there is no TURN relay configured. Pairing alone is neither WebRTC connectivity
nor installation proof. No remote shell, arbitrary-file, or handset-control API
is exposed. See [the build, installation, transport tests and retention details](../swarm-control/README.md).
Initial owner-server setup: `python3 swarm-control/server/setup-test-deployment.py`.

### Worker disk retention

Launchers sweep persisted job folders at startup, every five minutes, before
admitting a job, and after its containers finish. Active jobs, running native
processes and container-mounted folders are protected. Successful execution
workspaces are removed after bounded PNG/JSON reports are copied beside the job
receipt. The coordinator retains its own uploaded results.

At most two failed/interrupted workspaces survive for 24 hours. One successful
preparation stays warm for 30 minutes so a matching run can consume it. Finished
workspaces and local artifacts share a 5 GiB budget; oldest workspaces and then
local artifact copies are evicted under pressure. Small manifests and state
receipts expire after 30 days. New jobs require 10 GiB free after cleanup.

Installed npm/Python dependency trees are saved in compressed, runtime-,
architecture-, install-command- and lockfile-keyed archives. Each job restores a
private writable copy, then regenerates its source metadata. Failed installs are
not cached; invalid archives trigger a clean install. Local-file npm dependencies
use a fresh installation. Package/download caches have a separate 5 GiB LRU
budget and are cleaned only when no worker is active and their locks/mounts are
free. Git objects remain in the existing separate per-repository cache.

## GPU and storage readiness

Worker status, CPU telemetry, native REST/MCP machine details, and machine cards
include PCI GPU inventory and free disk space. NVIDIA hosts also report VRAM and
utilization when the native driver utility is available. Detection is separate
from CUDA container readiness and from a successful compute test. Stale samples
are labelled explicitly.

The `gpu` suite is included in a full run and can be requested independently via
`POST /api/v1/master/test-requests` with `{"suites":["gpu"],"force":true}`. It uses
controller-signed `gpu.probe` to launch a fixed PTX kernel, check a value copied
back from the GPU, and remove the isolated container. There is no arbitrary GPU
command/image endpoint. It uses the existing Python Bookworm image and NVIDIA
container runtime. Hosts without that runtime remain visible as excluded GPU
coverage, not verified compute workers. CPU workers continue independently.

The hardware panel also offers a user-triggered WebGPU kernel check for the
visiting browser. WebGPU requires a supported browser and secure context
(HTTPS or localhost). It does not make Android/Intel/AMD devices CUDA workers,
and software adapters are labelled when the browser identifies them. Attached
Android devices display their graphics driver and explicitly unverified compute
capability. Browser compute is not a folding backend or a phone job scheduler.

For the molecular-computing starter environment, keep the largest datasets on
Latitude or Inspiron and stage bounded inputs/results over the LAN. Disk space
on separate nodes is not pooled storage or pooled GPU memory. Do not start
terabyte-scale database downloads on CompAI's nearly full system disk. Actual
folding/chemistry models still need tool-specific VRAM and correctness checks.

The map sidebar includes live whole-genome transfer ETA, served by `/api/wgs`.
It reuses the WGS measured-progress estimator and updates every five seconds
while visible. Estimates cover transfers only; unknown, blocked, stalled and
stale sources suppress a numeric estimate. Only aggregate operational counts
are returned, with no reads, genotypes, credentials or source paths. Histories
warm up in memory after the collector restarts.

## Built-in Swarm Node resource monitor

**http://map.local/monitor.html** selects an enrolled node and exposes its Memory,
CPU, GPU, Disk, Network and LAN observations. The read-only `/api/resources` feed
runs as `swarm-control-resources.service` on loopback port 8795 and is proxied by
map.local under the existing LAN allowlist. It is independent of the job
coordinator, so resource UI changes do not require restarting active test runs.
The monitor is linked from the existing map/task pages and included in JSON export.

`swarm-performance.service` collects on each native enrolled node and publishes
bounded, encrypted, freshness-checked, acknowledged samples to the existing master.
The full collector is built into the node runtime/image and its atomically
checksum-covered agent sources. It requires no Textual or psutil dependencies for
headless collection. `./swarm-node monitor`, `telemetry`, `performance`, `dashboard`
and `build` expose these components from the public node command.

Deploy node monitor components and an isolated map resource release with:

```bash
~/global/bin/python swarm-control/server/scripts/deploy-resource-monitor.py
```

The installer attests machine identity using the authenticated agent, uses the
existing verified SSH account, stages a digest-checked monitor ZIP, and restarts
only the monitor service. Busy workers and agents are not restarted. The dashboard
installer copies the current immutable release, updates the stateless frontend
and new resource service, and preserves the running coordinator. Deployment
receipts are stored under `~/.local/state/swarm-control/monitor-*.json`.

Memory bandwidth remains separate from cache events, allocation, faults, swap,
GPU requests and TCP payload rates. AMD without exposed IMC counters reports
cache/OS fallback; Intel IMC and mounted resctrl MBM remain supported. Stale
measurements are not zero. Raw process/socket/client details are bounded in latest
snapshots; aggregate-only history avoids repeatedly retaining those details.

The Task Manager now embeds the six resource detail tabs and five-minute memory
read/write graphs. Task-machine entries show live machine DRAM rates and an
"Inspect machine resources" button; values cover the whole host, not individual
jobs. The authenticated `/api/v1/swarm/task-manager` response carries `resources`
from the bounded local monitor feed. Monitor outages leave task activity visible.
The LAN frontend uses the same fixed read-only path via its LAN-restricted proxy;
remote access continues through owner authentication.

Deploy this integration without restarting job coordination or workers:

```bash
~/global/bin/python swarm-control/server/scripts/deploy-task-resources.py
```

Focused test preparation has no implicit P2P job. Until the selected task catalog
is ready, the task chart reports worker preparation and the verified/waiting
machine counts. Workflow stages whose remaining work requires unavailable inputs
propagate `waiting_inputs`; completed prerequisite audits do not promote a
scientifically partial stage to full completion.

Automatic retries claim up to 32 due logical tasks sharing the same pinned parent
run in one transaction. One focused child run prepares those immutable sources
and shares the selected file queue across the enrolled workers. Dispatch records
all tickets atomically; results, attempt limits and backoff remain per task. The
chart separates pending retries, attention-required retries and recovered history.


## Worker controls

Open **http://map.local/workers.html**, or use **Worker controls** in the map,
Task Manager, jobs, or resource-monitor header. Machine details also links there.
On the LAN no website login is required.

- **Pause new jobs** saves `worker-policy.json` in paused mode; admitted work may finish.
- **Stop swarm work now** also stops labelled compute containers, the scoped Venter
  QC service, and the machine's GPU lease supervisor. Audio and conversation
  services are left running. An interrupted alignment batch is requeued.
- **Resume jobs** restores automatic admission and restarts the GPU lease
  supervisor and any Venter QC service stopped through these controls. It never
  restarts an old alignment container or a GPU container with an expired lease.

The LAN-only nginx endpoint proxies to the independent loopback resource service;
writes require a same-origin JSON request and an enrolled host. Unknown status
stays unavailable. Repeated pause/stop actions keep the machine paused.

## Runtime boundary: swarm-node and application workflows

Decision (2026-11-01): `swarm-node` owns reusable execution, resource enforcement,
and authorized data movement. Applications own their algorithms, dependency
plans, domain evidence, and estimates of the resources those algorithms need.
Keep one node runtime; this boundary does not introduce another daemon.

The node runtime owns:

- Admission against measured CPU, RAM, GPU/VRAM, storage and network capabilities;
  enforcement of the admitted container limits and accounting for all concurrent
  stages, including RAM scratch and transfer buffers. Applications declare their
  needs; the runtime waits or rejects incompatible plans.
- Authenticated input streaming, integrity checks and bounded reusable caches,
  using the application's declared data-access, persistence and locality policy.
  Prefer streaming for single-pass data. RAM caching and SSD spill are explicit
  choices; RAM is not automatically the right cache for every workload.
- Container creation, bounded tmpfs, swap policy, permitted scratch tiers and
  budgets, disk-space reserves, ownership, cleanup, and durable operational
  events. A RAM-only job cannot silently spill to disk. The runtime cleans only
  declared disposable artifacts; application results and source data are retained.
- Worker discovery, hardware/backend capabilities, owner pause/resume, graceful
  draining, compatible updates, connection recovery, and bounded execution
  retries. Scientific validation failures are reported to the application rather
  than retried indefinitely or reclassified as infrastructure failures.
- Generic checkpoint storage/publication and progress reporting, including
  measured rates, active siblings and failures together, and appropriately scoped
  ETAs. Applications supply units, dependency boundaries and validation hooks;
  the runtime cannot infer an end-to-end estimate from a stage estimate.

The genome application owns reference/assembly selection, chromosome coordinates,
read pairing and partitioning, aligner/tool selection and parameters, RAM/buffer
sizing formulas, the collate/fixmate/sort/merge/markdup sequence, and the proof
that per-batch mate preparation permits a genome-wide sorted merge. It owns
read-count validation, cross-batch duplicate correctness, genotype/phase/callable
region semantics, uncertainty attribution, evidence reconciliation and reports.
It defines which checkpoint is scientifically valid and which generated BAM
aliases may be retired after a verified replacement. The runtime provides the
atomic publication and scoped cleanup mechanism, not that biological decision.

A job declaration should identify its immutable image/command and authorized
inputs, CPU/RAM/GPU requirements, bounded RAM scratch, permitted disk spill and
space requirements, durable outputs, retry category, and checkpoint/progress
hooks. Do not encode sample names, genome formats or samtools subcommands into
this generic contract. RAM-only private inputs, public reusable references and
durable outputs can have different policies within the same job.

Implemented placement: `scripts/swarm_runtime.py` is the shared node SDK for
worker discovery and identity checks, `run_job`, resource admission, bounded RAM
scratch and no-swap containers, operational receipts, cleanup, verified file
streaming, and atomic checkpoint publication. It imports no genome applications.
The existing node image includes it through its scripts package; no second daemon
or new arbitrary-command HTTP endpoint is introduced.
Verified transfers check the destination cache before opening the source stream.
A matching size and SHA256 skip retransmission; missing or corrupt copies still
use the verified resumable stream. Cache validation remains content based.

`scripts/genome_runtime.py` is a small application adapter selecting the genome
group and labels. `wgs_recall_swarm.py` re-exports its old names for compatibility;
its execution and transfer implementations have been removed. Current genome
controllers use the adapter. `julian_finalize.py` retains the batch algorithm,
scientific validation and resource/storage sizing formulas. Existing controllers
keep their exception catch boundaries during the migration.

New agents advertise `generic-workers` and accept `swarm-job-<id>` as well as
legacy job-bound names. The SDK uses existing wire names on older enrolled agents,
so adopting the common code does not require interrupting active jobs or an
immediate fleet update. Generic jobs use their own group and labels. Runtime
identity labels cannot be overridden, and all tmpfs budgets combined must leave
room inside the admitted RAM limit for application working memory.

Optional GPU scratch uses `scripts/gpu_scratch.py` and the worker-built
`gpu-tools/gpu_scratch.c` adapter. It uses only physical DEVICE_LOCAL Vulkan memory, requires live GPU
memory-budget support, serializes
scratch users with an owner-only lock, and retains at least 512 MiB GPU headroom.
The application supplies a separate GPU budget (currently 2 GiB for MiniPC QC).
Small or integrated allocations use coherent CPU mappings. Large discrete-GPU
allocations use a bounded 2 MiB transfer window when mapping is unsuitable. Shared
integrated heaps are additionally capped by the worker CPU-memory headroom.
Scratch is never added to CPU RAM admission capacity and cannot transparently replace
ordinary RAM for CPU aligners. Context exit wipes and frees buffers; process exit
releases driver allocations. Software Vulkan and host-heap fallback are rejected.

GPU workers now install and probe the same scratch adapter by default, including
the native RTX worker. QC working buffers prefer mapped device-local memory and
report their actual tier. Native and Docker genome workers share the same verified
compressed-input path. MiniPC's independent QC receives the mate streams over
pinned Ethernet SSH directly into bounded VRAM buffers. Size and SHA256 are checked
before parsing each mate; each mate is released before the next is admitted.
Gzip decodes bounded windows and sends inline chunks to the existing
physical-GPU QC socket. Input staging and QC chunk files are eliminated on this
path. Only operational scratch counters accompany the existing result. Ethernet SSH/TCP still uses CPU transit buffers;
this path avoids disk staging and does not claim NIC-to-GPU RDMA. Durable
source reads, verified alignments and checkpoints retain their existing storage.

Generic lifecycle, transfer, compatibility and RAM-accounting tests live in
`tests/test_swarm_runtime.py`; genome adapter tests remain in
`tests/test_wgs_recall_swarm.py`. Biological finalization tests remain separate.


### Host Docker fixtures in the release gate

The five explicitly listed Docker integration files in `work_queue.py` run on
the enrolled worker host, from the same pinned checkout and with prepared Python
dependencies. They start only their scoped synthetic sibling containers. Ordinary
file jobs retain Docker isolation and never receive a daemon socket. Host fixtures
retain resource admission, a private temporary HOME/TMPDIR and the worker's
credential-free environment. This is trusted, reviewed operator test code; the
dashboard cannot provide arbitrary commands or enable host execution for another
file. Docker and the fixture images must be available on that enrolled host.

Cold Selenium startup has a bounded three-minute readiness window; dependency
installation and active test time remain separate. Browser resource buttons are
selected within their named region so unrelated tab buttons cannot make a check
ambiguous.

File execution also respects CPU affinity and caps each enrolled worker at four
test CPU slots. This bounds Docker startup/inspection pressure alongside owner
workloads; preparation and P2P reservations still reduce the available slots, and
every selected test remains required.

Worker events remain durable and idempotent. Raw events commit in one transaction
per received batch, outside the coordinator control lock, before being applied to
its materialized state. The full run checkpoints once per batch instead of
independently checkpointing multiple state changes within that batch. Terminal
batches are drained and saved before completion.

### Job runtime observations

`python3 -m scripts.task_runtime_stats` samples the ongoing original-read replica
and Venter recovery without restarting their workers or reading genetic content.
It writes owner-only `task-runtime/<task-id>/<child-id>.json` sidecars. The task
registry merges only measurements younger than 60 seconds into running rows;
execution status, worker ownership and verification remain executor-owned.

The sampler reports phase, observed byte/shard counters, elapsed time, RSS and CPU
where measurable. Rates use fresh counter deltas within the same phase/attempt.
An ETA covers that phase only and requires a known denominator and positive rate.
A prepared shard count is not the final input denominator. Replica phase timing
starts when monitoring first observes the phase, and is labelled accordingly.
Linux file-descriptor offsets provide fallback SHA256 progress for already-running
legacy transfer processes. Upcoming mutual-TLS CLI transfers publish native
transfer and checksum-phase counters separately. No checksum result is inferred
from a file offset or reported as verified by this sampler.

Runner source updates inspect both the default Docker daemon and system Docker
for the controller, since compute workers may use rootless Docker while their
controller uses system Docker. Supervisor source updates require the enrolled
configuration mount to match. On HDD nodes with large retained job inventories,
`agent_startup_timeout_seconds` in the enrolled peer configuration can extend
startup recovery from its default 180 seconds, bounded to 45–900 seconds. This
changes startup rollback timing only; it does not declare an unready worker ready.
