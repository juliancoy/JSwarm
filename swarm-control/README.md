# Swarm Control

Swarm Control is the independently operated application for machine inventory,
distributed jobs, remote Codex conversations, controller enrollment, and Android
access. Its implementation and runtime live here; LifeTech and OrgPortal are not
application dependencies. PIdP remains the shared identity provider.

## Ownership

- `server/`: coordinator, REST/MCP operator API, network collectors, remote API,
  web console, controller sessions, and authenticated Android-launcher delivery.
- `android/`: browser-based Android launcher; it shares the browser's authenticated
  session instead of maintaining a second WebView identity.
- Root `scripts/launcher_*.py`: machine-local agent transport and enrollment.
- Application repositories: their own test suites, release and backup operations.

Swarm Control schedules application-owned operations. It does not copy application
business data or replace application permissions. The packet collector has its
own restricted container; the public tunnel does not expose Docker or the native
operator API.

## Operate

### Node and job lifecycle

Each enrolled machine has one `swarm-node`, enforced by the existing supervisor
lock. Enrollment names, Docker container names and labels, service units, the
resource pool, and the retry queue remain authoritative; there is no second
node registry or coordination service.

An explicit node update briefly gates admission before replacing the node process.
Docker-owned shard jobs continue running: the replacement rediscovers their names,
ownership labels, CPU/RAM limits and resource records from Docker. Remote shard
launches are detached; their controller polls the same container across node
reconnects instead of holding a command pipe for the lifetime of the job. Logs
remain with Docker under a bounded rotating local log driver.
An update declines new admissions for one supervisor tick and replaces the agent
without waiting for jobs, commands, transfers or reservations to finish. Command
pipes and acknowledged byte windows belong to a persistent local runtime attached
to this same enrolled node; replacing the API process reconnects to those existing
command IDs. Accepted launch reservations are adopted under their existing IDs. Durable queued
stream work waits and resumes after startup. The supervisor verifies the atomic
release checksums and the new process's readiness marker; failed startup restores
the previous release. Cancelling the staged update removes only the update gate,
preserving owner maintenance, battery policy, and pressure protection.

Publish a validated immutable release, then let the existing deployment service
follow it one enrolled node at a time:

```sh
python3 scripts/deploy_swarm_inputs.py --publish
python3 scripts/deploy_swarm_inputs.py --published --continuous --watch
```

Publication runs the release/recovery tests, checks that sources did not change
while they ran, and atomically publishes the complete checksummed bundle. The
continuous mode follows that publication, not live edits in the workspace. It
uses the existing `swarm-input-deployment.service`, enrolled names, identities,
and `input-deployment.json` receipt; it creates no second node registry. A source
release digest describes code, not a new node or job identity.

For a single existing node:

```sh
python3 scripts/deploy_swarm_inputs.py --published --node compai.local --wait-timeout 180
```

Only one rollout can hold the deployment lock. Each node must report the exact
target revision and its existing machine identity before the next is updated.
A pending or offline node leaves later nodes untouched. Existing Docker workers
are neither drained nor relaunched. Work counts never gate replacement. Startup
health checks and rollback deadlines concern only the new agent, not job runtime.
The local command runtime has a private Unix socket and no additional node identity,
network listener, enrollment record or pipeline item.

Each node journals the existing Docker job label, run/shard label, container ID,
image digest, CPU/RAM limits, timestamps and exit status on its persistent config
mount. Startup reconciles labeled containers before readiness; Docker exit events
retain completion evidence across container cleanup. `worker.status` and paginated
`worker.records` use the existing authenticated agent protocol. Neither API
creates work. Container-name reuse with a different container ID is rejected.
The shared agent lock prevents two processes from managing the same node.

Jobs retain their existing durable identities, pinned sources, phase events and
verification receipts. Preparation and verification are phases of the same job,
not new queues. A retry keeps its original source parent; a corrected-code run
is a separate linked run, never a rewrite of the original failure. Docker names
and labels identify workers; resource records account for active ownership.
Application recipes, including genome algorithms and scientific validation,
remain with their applications. The node owns admission, transport, execution,
resource accounting, telemetry and updates.

From Organization:

```sh
python3 swarm-control/run.py install
python3 swarm-control/run.py start
python3 swarm-control/run.py status
python3 swarm-control/run.py stop
```

`swarm-control.target` owns the coordinator, authenticated remote API, HTTPS
tunnel, and collector containers. `deploy --revision SHA` extracts immutable application code and installs the reviewed implementation
and restarts this target. The configured all-repository release invokes it after
the distributed test gate. No second coordinator or old service API is retained.
The existing CompAI URL remains the access address during this extraction.

`python3 swarm-control/run.py deploy-recovery` deploys the workspace's reviewed
event-journal, collector, retry, and file-dispatch modules into immutable copies
of the active releases. It drains accepted application tests, then restarts only
the coordinator and task collector, with health checks and rollback. The
conversation API, login sessions, launchers, and DNA workers continue running.
Executor records take precedence over dashboard registrations; retained pinned
run files repair older shadowed records automatically.

## Identity and recovery

Open `https://compai.lifetech.fyi/auth/login`. Swarm Control has a separate PIdP
client, audience `https://compai.lifetech.fyi/api/swarm`, and `swarm:control`
permission. LifeTech portal credentials are not requested. The configured owner
is an explicit PIdP-verified canonical person; linked credentials are checked
through authenticated introspection. Email and application working copies never
assign ownership. Initial registration is an operator action:

```sh
python3 swarm-control/server/setup-identity.py \
  --owner-subject YOUR_VERIFIED_PIDP_SUBJECT \
  --owner-canonical-id YOUR_VERIFIED_PIDP_PERSON_ID \
  --resource-secret-file /private/path/to/swarm-resource-secret
```

Client credentials and controller records are owner-private under
`~/.config/swarm-control/`. Each successful owner sign-in adds an independent controller. Multiple browsers
can remain signed in; reauthentication rotates only that browser’s session.
Lock revokes only the current browser, including its transfer authorization.
Controller records retain their label, creation, expiry, and last-seen times.
WAN controller sessions are issued only after PIdP sign-in. Legacy owner tokens,
one-time pairing redemption and legacy unclassified sessions cannot authorize the
public API. Local QR codes now link to `/auth/login`; they carry no credential.
Existing browsers must sign in once again after this migration.

## Interfaces and state

- Public HTTPS: console, `/task-manager`, `/swarm-status`, `/swarm-control` APK receiver, and
  authenticated `/api/v1/swarm/*` operations. `/health` exposes readiness only.
- Loopback 8089: coordinator, native authenticated REST/MCP, local dashboard.
- Loopback 8091: the remote API, reached through the configured tunnel.
- Loopback 8092: separately authenticated local QR issuer.
- `~/.local/state/swarm-control/`: coordinator history, queues, metrics and captures.
- Workspace `.runtime/mobile/`: signed test APK; signing material stays private.

## Validate

```sh
python3 swarm-control/run.py test
node swarm-control/test-console.mjs
```

The transport check uses an isolated HTTPS fixture and a real browser-to-aiortc
channel. It validates authorization, APK bytes, SHA-256, receipt, narrow layout,
and disconnect; it never borrows a production session or claims handset
installation. Build the Android launcher with `npm --prefix swarm-control run build`.
The user must approve installation on the Galaxy.

Run the global distributed suite through the coordinator or the Swarm status
page. Queueing tests publishes reviewed source revisions before execution.

## Remote Task Manager

Open `https://compai.lifetech.fyi/task-manager` after signing in to Swarm Control.
CPU, GPU, GPU memory, RAM, disk, network, and task observations use one
same-origin authenticated HTTPS feed. The browser never contacts LAN addresses.
Measurements refresh every three seconds while the page is visible; missing or
stale telemetry remains unmeasured. Session expiry clears the displayed data.

## CPU reporting

CPU load uses differences in Linux counters across the entire interval between
snapshots, normally five seconds. No one-second sampling gaps or root privileges
are required. Startup, counter resets, reboot and long gaps warm up without
reporting idle. Missing, permission-denied and stale samples remain unmeasured.
Agents need the current metrics implementation; old agents show update required.

The fleet average is weighted by measured logical CPUs. A second average includes
only machines currently executing tests, including active reruns. Both describe
all processes on those machines, not CPU time attributed to test containers.
The UI shows coverage, interval, age and execution phase. I/O wait and stolen CPU
time are reported separately from executing CPU time. CPU pressure is the kernel's
10-second average of time tasks wait for CPU. Container CPU quotas still constrain
execution; they do not change the whole-machine utilization denominator.

## DNA research workflow

`/research` on the authenticated remote service (`/research.html` on the local
coordinator) maps the documented DNA analysis stages, file formats, completed QC
and existing-annotation audit, and pending coverage/annotation/comparison work.
Research links are available in the console, LAN dashboard, and swarm status.
The dated milestone is historical; the session-protected
`/api/v1/swarm/research` endpoint refreshes batch counts and public download
presence every 30 seconds while visible. It reads only operational state and an
allowlist of public dataset folders on PILEDRIVE; it never serves genomic records,
sample identifiers, report contents, or arbitrary filesystem paths. Missing state
is shown as unavailable. Partial downloads do not imply active transfer or
successful validation. Browser checks use synthetic status fixtures.

Verified reference-match snapshots can be rebuilt with
`python3 -m scripts.genome_demo --root <private-workbench> --reference-run <completed-private-run> --output <snapshot.js>`.
The reference run uses quality-qualified gVCF calls for personal data and official
GIAB benchmark regions minus both official and normalized variant REF spans.
Unknown quality stays unverified. Only aggregate counts enter the public snapshot;
private intervals, sample headers, and worker hostnames stay outside it.

## Concurrent tests and private DNA jobs

The durable request inbox admits up to eight independent runs concurrently and
keeps up to 100 pending/running requests. Ordinary tests and configured DNA
analyses use the same inbox. Publication is serialized; execution is not. Every
run owns its checkout and dependency trees. P2P/device/GPU runs serialize against other coordinated hardware runs, while
ordinary test and DNA jobs continue to share remaining worker capacity. P2P
browsers and GPU kernel probes also reserve their CPU and memory in the same pool.

All test/install containers and private DNA batches acquire CPU and memory from
one process-safe pool per worker. The pool also reserves each container’s PID
limit and leaves up to 256 process/thread slots free in finite cgroup ancestors for host
services and Docker. Combined reservations cannot exceed logical
CPUs or current schedulable RAM, including visible shared cgroup constraints.
Genomics jobs divide live available memory across remaining concurrency and queued
jobs; their Docker memory limits equal the resource leases. There is no fixed
512 MiB worker ceiling or fixed host-memory reserve. Agent-private container
limits do not constrain sibling worker containers. Fitting jobs backfill free slots. Groups with the smallest dominant CPU
or memory share get priority, and a waiter older than 30 seconds reserves
headroom to prevent starvation. Docker still enforces each container's limits.
The controller bounds pending compressed DNA payloads to 128 MiB per run and 64 in-flight batches. Process locks recover capacity after crashes; orphan container reservations remain
until the container exits. Private stream leases expire with container cleanup.
Worker policy, enrollment/attestation, disk space and bounded session counts still
apply. Cancellation affects one request/run and releases its owned resources.

The Research page lists test and DNA requests and provides queue/cancel controls.
DNA submissions use idempotent request IDs and named owner-only profiles in
`~/.config/swarm-control/research-jobs.json` (mode 0600), each containing `qc`,
`annotations`, and `output` paths. Those paths, private logs, and genomic reports
are never returned by the control API. Research code is snapshotted from an exact
Git commit into the private report directory before execution. Inputs remain
SSH-streamed into RAM-only containers. The general test artifact system receives
only operational progress. A coordinator restart marks unfinished requests
interrupted instead of silently repeating private work; explicit reruns are
available after reviewing the private reports.

Node updates decline new admissions briefly, replace the agent, and reconcile
existing Docker workers. They never wait for running jobs to finish.

Launcher updates raise undersized PID limits on the verified launcher container to
a bounded budget based on logical CPUs (at most 32,768). This uses Docker update
and does not restart containers; unrelated containers and unlimited limits are
left alone.


## Task Manager history

The coordinator's resource sampler retains a rolling five minutes of host CPU,
RAM, disk, network and GPU measurements in bounded memory, independently of
browser requests. Opening or reloading Task Manager restores that history from
the authenticated feed. Collection continues while the page is closed; a
coordinator restart clears the window. Unavailable samples remain gaps. Historical
gateway-only traffic is not reconstructed from current measurements. Private
process details, paths, credentials and genomic records are excluded.

## Live machine connections

Task Manager uses a small owner-authenticated connection feed separately from
resource history. Enrolled peers receive signed status heartbeats every second,
with a two-second timeout per peer. Each result publishes independently, so a
slow or disconnected machine cannot stall healthy machines. Enrollment changes
are picked up automatically. Samples older than five seconds become unknown.
The visible page polls the feed every second and rechecks immediately after
network recovery or returning to the tab. A paused worker remains connected;
browser/API outages show unknown status rather than claiming machines are offline.


## Workload History

Open `/history` from Conversations, Machines, or Research. The History view lists
up to 30 recent genome analyses and test runs from existing coordinator records,
prioritizing active work before applying the limit. Each entry identifies its
profile or test scope and shows status, timestamps, available progress counts,
and participating machines and task titles. Filters cover workload type, active
or finished state, and title/run-ID search. The owner session is required for the
read-only `/api/v1/swarm/history` feed. It excludes private paths, genomic report
contents, credentials, and logs; it creates no new persistent history store.

The focused UI check uses synthetic local data:

```sh
CHROME_PATH=/usr/bin/google-chrome node swarm-control/server/scripts/review-history-ui.mjs /tmp/swarm-history-review
```

This check also runs in the existing distributed Swarm Control browser suite.


History includes a Computer contributions table for the filtered runs, with
rankings by newly completed tasks, recorded process CPU time, or estimated
CPU-slot time. Reused and skipped work contributes no new tasks or CPU time.
Estimates multiply recorded task execution duration (falling back to total task
duration) by allocated CPUs; they are not measured utilization. Measurement and
timing coverage are shown beside the totals. Missing measurements stay unknown.
Genome batches now carry their worker process CPU seconds into operational
state for future analyses; older records cannot supply that missing measurement.


The background resource history also saves gateway send/receive counters and
their observation timestamps. Directional network charts restore those actual
measurements; older frames without gateway counters do not insert artificial
nulls between live readings. Failed or stale capture samples still render gaps.


Network charts group each machine's physical interfaces separately (for example,
Ethernet, Wi-Fi, and WWAN), with independent send/receive counters and five-minute
history. Loopback and virtual interfaces are excluded to avoid double-counting.
The coordinator reads bounded Linux interface counters using existing enrolled
addresses and verified saved SSH access; workers are not restarted or reconfigured.
New, replaced or reset interfaces warm up before reporting rates. Unavailable
interface telemetry is labeled unknown rather than assigned to a guessed device.


Device labels retain a MAC-matched mDNS name for up to 24 hours after its last
actual discovery reply. Cached names are labeled with their source and observation
time in the feed; they do not change measured connectivity or renew themselves on
failed lookups. Fresh replies replace cached names immediately. IP reuse by a
different MAC never inherits the previous device's name.

## Conversation Markdown

Received conversation text is parsed as GitHub-flavored Markdown: headings,
paragraphs, nested/task lists, tables, quotes, emphasis, links and fenced code.
Code blocks retain literal whitespace and have a Copy button. The same renderer
serves the desktop console and Android browser launcher. Partial streamed fences
remain readable. Original message text and submission payloads are unchanged.

The pinned Marked lexer is vendored with its license in `server/view/`; no CDN
or runtime package fetch is required. Parsed tokens create DOM nodes rather than
inserting rendered HTML. Raw HTML stays literal; unsafe URL schemes and local
filesystem links are inactive. Images become explicit links rather than loading
remote tracking resources. Very large messages or parsing failures fall back to
literal text. Update the vendored module and license with the npm dependency.

Run `npm --prefix swarm-control run test:markdown` for isolated desktop/mobile
rendering, clipboard, unfinished-message and hostile-content checks. Screenshots
are written to `artifacts/swarm-markdown/` without reading real conversations.


The upper-right identity badge uses the verified PIdP person's public name and
avatar, with initials when no image is available. Only the verified canonical ID
is stored alongside the existing expiring controller session; no PIdP access or
refresh token is retained. Lock deletes that browser session. Public-profile
responses have a bounded 60-second in-memory cache and disappear on process exit.
The optional `public_profile_endpoint` must be HTTPS on the configured issuer;
OrgPortal's identity proxy uses `/pidp/auth/public/users`.

`npm --prefix swarm-control run test:pidp-browser` uses isolated synthetic HTTPS
sessions to check desktop/mobile sign-in, avatar rendering, retired pairing,
independent devices and logout. PIdP protocol tests run locally with
`python3 -m pytest tests/test_mcp_authorization.py tests/test_identity_urls.py -q`
from the PIdP checkout; they use disposable fixtures rather than production login.

WAN WebRTC offer/signaling for APK delivery is retired (`/api/v1/test-app/offer`
returns 404). The launcher is delivered through PIdP-authenticated HTTPS. The
local fleet agent protocol remains a separate machine execution boundary and is
not published by the console tunnel.

WAN authorization also checks the signed PIdP subject against the private
`allowed_subjects` policy on every request. The production policy is pinned to
the active, verified PIdP account identities for `julian@codecollective.us`,
confirmed against the authoritative identity directory. Other linked sign-in
identities are denied even if they share a canonical person ID. Sessions retain
the verified sign-in subject under their existing expiry/revocation lifecycle;
older sessions without that proof require a fresh sign-in. Email text provided
by the browser never grants access.

Queued messages have a **Send now** action above the composer. It bypasses the
normal FIFO wait for that selected message: running turns receive a steer; idle
threads receive a new message. The server checks connection/state and atomically
claims the record against automatic delivery and cancellation. Confirmed messages
leave the visible queue. Uncertain deliveries remain for review and never retry
automatically. Sending, reviewed and cancelled records cannot be sent again.

Opening a conversation creates a browser-history entry. Browser/system Back
returns to the conversation list before leaving the console; Forward reopens the
thread. The Conversations button uses that same history. The selected thread is
kept in the URL fragment so reload can restore it, while an unsent text draft
stays in this browser tab. Back does not cancel queued work or a running turn.
At the conversation list, Back retains normal browser navigation behavior.

## Conversation attachment

A signed-in phone and a conversation's local IDE owner are separate connections.
Selecting a detached conversation or queueing an instruction now requests
attachment automatically through the existing VS Code conversation URI. It does
not create a second app-server, duplicate a conversation, start an unsolicited
turn or change sandbox/approval settings. Running conversations keep their owners.
Attachment requests are asynchronous, serialized and limited to three attempts
with backoff; genuine failures expose a Retry connection action. Queue messages
show their connection state and remain durable while disconnected. Known failures
before dispatch remain queued; ambiguous outcomes after dispatch remain blocked
from automatic retry. PIdP expiry preserves drafts and still requires sign-in.
