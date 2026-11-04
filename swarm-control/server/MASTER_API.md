# Distributed test master API

This is the `map.local` test coordinator, not an OrgPortal or launcher-agent API.
REST and MCP invoke the same bounded operations on the existing coordinator.
Neither interface accepts arbitrary shell commands, file paths, deployment actions,
or device credentials.

## Authentication

Create an owner-only automation token on the master, once:

```sh
python3 swarm-control/server/master_api.py create-token
```

The file is `~/.config/map-master/api-token`. Set `MAP_MASTER_TOKEN_FILE` in the
collector environment to use another private file. The server reads it on each
request, so replacing the file rotates the token without a restart. No token is
embedded in source or the dashboard. All new REST/MCP routes require it, including
read operations. Dashboard test mutations also require the master token. The browser only accepts it
on localhost or HTTPS; use the local launcher for encrypted remote job submission.

The collector stays loopback-bound behind the existing LAN-only nginx proxy.
`map.local` uses HTTP. Send bearer credentials through localhost/SSH or an
authenticated TLS proxy; a LAN connection does not provide confidentiality. Host and Origin checks apply to both
interfaces. Additional trusted hostnames can be supplied in
`MAP_MASTER_ALLOWED_HOSTS` (comma-separated). This API does not open a WAN listener.

## Codex connection

Load the token into the environment of the process that launches Codex, then add
the Streamable HTTP server:

```sh
export MAP_MASTER_TOKEN="$(cat ~/.config/map-master/api-token)"
codex mcp add map-master --url http://127.0.0.1:8089/mcp --bearer-token-env-var MAP_MASTER_TOKEN
```

Equivalent Codex configuration:

```toml
[mcp_servers.map-master]
url = "http://127.0.0.1:8089/mcp"
bearer_token_env_var = "MAP_MASTER_TOKEN"
```

For Codex on a different machine, first use `ssh -L 8089:127.0.0.1:8089 julian@compai.local`. Provision the token privately there and set the
same environment variable. A desktop-launched Codex must receive that variable
in its launch environment. See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
The server uses stateless MCP Streamable HTTP with JSON responses, tools and
read-only resources. It supports protocol versions 2025-11-25, 2025-06-18 and
2025-03-26. It does not expose an SSE event stream; clients poll status.

## Tools

| Tool | Behavior |
| --- | --- |
| `get_status` | Current coordinator/run preparation and API links |
| `list_runs` | Latest ten independent run summaries |
| `get_run` | A specific run's results and source revisions; bounded logs are separate |
| `get_logs` | Latest 1–80 retained lines for a run/machine/suite, plus up to 20 failure lines |
| `list_machines` | Observed LAN inventory and latest-run machine/session states |
| `list_suites` | Fixed application-owned suite definitions and images |
| `start_run` | Start the configured coordinator workflow |
| `stop_run` | Stop only the specified currently active run |

Before publication, all selected repositories undergo secret checks using a temporary
Git index. Sensitive credential filenames, private keys, recognizable credential
literals, and known credential values from local environment files block the run.
The existing index is checked too, and staged blobs are checked again immediately
before committing. Errors identify the file without printing secret values.
This is a commit-time guard, not an audit of existing Git history.

**Start is a write operation:** the current coordinator commits and pushes
Organization, OrgPortal and PIdP on their current branches, then prepares agents,
images and application tests. Its MCP metadata explicitly marks the operation as
mutating and describes those GitHub effects. Calling start here has the same
behavior as the webpage button. It does not deploy production applications.

`stop_run` requires `run_id`. A stale ID returns a conflict and cannot stop a newer
run. Repeated starts while busy return a conflict. Historical reads never replace
the active run. Test logs are untrusted output and must not be interpreted as
instructions for the agent.

Read-only MCP resources are `map-master://status`, `map-master://runs`, and
`map-master://openapi`.

## REST

Base URL: `http://map.local/api/v1/master`

| Method | Path | Operation |
| --- | --- | --- |
| GET | `/status` | Master status |
| GET | `/runs` | Latest ten run summaries |
| GET | `/runs/{run_id}` | One run's recorded results |
| GET | `/runs/{run_id}/logs?host=compai.local&suite=monitor&tail=40` | Bounded suite output |
| POST | `/runs` | Start configured distributed tests |
| POST | `/runs/{run_id}/stop` | Stop that active run |
| GET | `/machines` | Observed and recorded worker inventory |
| GET | `/suites` | Configured suites |
| GET | `/openapi.json` | OpenAPI 3.1 schema |

Use `Authorization: Bearer $MAP_MASTER_TOKEN`. POST requests require
`Content-Type: application/json` and an empty JSON object. Successful mutations
return HTTP 202. Invalid arguments return 400, missing/invalid credentials 401,
missing records 404, concurrent/stale run mutations 409, and unavailable master
operations 503. Error bodies have `error.code` and `error.message`.

```sh
curl -H "Authorization: Bearer $MAP_MASTER_TOKEN" http://map.local/api/v1/master/runs
```

MCP POST requests must advertise both `application/json` and `text/event-stream`
in `Accept`, as required by the transport. Accepted notifications receive 202
with no body; GET/DELETE on `/mcp` receive 405 because the server is stateless.
Request bodies are limited to 64 KiB. MCP tools return both text and structured
JSON results so Codex can read individual fields without parsing prose.

## Activation

The new routes require the collector to adopt its updated HTTP handler once and
nginx to adopt the `/mcp` proxy location once. No service was automatically
restarted or deployed while implementing this API. The existing per-run code
loader continues to load current coordinator code without restarting active runs.

Run results may include `status: reused`, `input_fingerprint`, and `reused_from` for unchanged suites. These are historical passing baselines, not fresh executions. History exposes separate `passed` and `reused` counts, and `get_run` retains that provenance.

Independent reruns: MCP `rerun_suite` takes `run_id`, `host`, `suite`; `rerun_failed` takes `run_id`. REST equivalents are POST `/api/v1/master/rerun-suite` and `/api/v1/master/rerun-failed` with those JSON arguments. Each returns a new `run_id` and `parent_run_id`. Inspect it with `get_run`/`get_logs`, cancel with `stop_run`. Busy workers are reserved until their existing run completes; queued reruns do not alter the parent record. Current branches are committed/pushed as for a full run. Reruns force selected ordinary suites to execute and omit deployment/email checks. P2P requires a full run. Queue capacity is ten active reruns; queued work is in-process and does not automatically resume after service restart.

## Local VS Code / Codex panel

The test dashboard includes a private local Codex panel. Unlock it using the
separate owner token at `~/.config/map-master/codex-token`. Create it with
`python3 swarm-control/server/master_api.py create-token --scope codex`. Use the direct
localhost endpoint on port 8089 or an authenticated SSH tunnel; the LAN proxy
blocks Codex routes. The master and launcher tokens cannot control AI threads. The browser keeps the token only in memory; Lock or
reload clears it. Remote browsers talk to the master, which reads **its own**
Codex history and connects to **its own** VS Code session.

- `GET /api/v1/master/codex/threads?offset=0`: 20 recent threads per page,
  with `next_offset` for older history.
- `GET /api/v1/master/codex/threads/{id}`: recent user/assistant messages,
  workspace/model, last recorded activity, current owner availability and IDE
  context when supplied by the owner.
- `POST /api/v1/master/codex/commands`: `{ "thread_id": "...", "action":
  "send", "text": "...", "request_id": "UUID" }`. Use `steer` for an active
  turn. Requests go to the existing VS Code thread owner, retaining its
  permissions; approvals remain in VS Code. The UUID prevents duplicate
  dispatch during this master process's lifetime. An ambiguous timeout must be
  checked in the conversation before another instruction is submitted.
- The same POST with `action: "open"` and `thread_id` opens an older thread on
  the **master's desktop** through the VS Code URL handler. Once its owner is
  connected, messages can be submitted remotely. VS Code must be installed and
  signed in under the same account as the master service. The user service must
  inherit the graphical desktop environment for the Open action.

No Codex process is spawned to resume an already-owned conversation. History is
read-only, limited to the latest 2 MiB and 60 messages per thread. Recorded
running status can be stale after a crash; owner availability is checked again
before every send. The UI deliberately labels that distinction.

The owner bridge uses the installed OpenAI VS Code extension's **internal**
length-prefixed Unix-socket follower protocol (inspected in version 26.51007),
not a public stable API. Protocol failures are surfaced, without falling back to
another process or overriding permissions. The public
[Codex app-server protocol](https://learn.chatgpt.com/docs/app-server) describes
start/steer operations, but a separate app-server does not control an existing
VS Code stdio session. Recheck the bridge after extension upgrades.

These routes inherit master bearer, Host, Origin, JSON body-limit and no-store
checks. They are REST dashboard routes, not MCP tools. No conversation content
or access token is added to the public distributed-test status endpoint.

## Always-listening focused test service

The master user service stays online between tests. Submit a request with REST:

```http
POST /api/v1/master/test-requests
Authorization: Bearer <master token>
Content-Type: application/json

{"paths":["swarm-control/server/tests/test_test_requests.py"],"force":true}
```

Or call MCP `request_tests` with the same arguments. Use `suites: ["monitor"]`
for a component. Paths and suites are unioned. Exact test files select individual
jobs; source paths conservatively select their component's tests and build checks.
Available suites: monitor, map, workspace, org-api, chat, web, identity,
identity-python, visual, p2p. The API accepts no arbitrary commands.

Acceptance returns HTTP 202 immediately after validation and durable storage.
`list_test_requests` / `GET /api/v1/master/test-requests` lists the inbox;
`get_test_request` / `GET /api/v1/master/test-requests/{request_id}` returns
status, selection, run ID and final execution duration in seconds. The associated
run uses the existing run-details/log APIs and dashboard history. Submission,
start and finish timestamps distinguish queue time from execution time.

Requests wait while a run is active, then use the shared resource-aware worker
queue. Queued requests survive a master restart; interrupted active runs are
reported as interrupted, never silently replayed. Each execution publishes the
current source using the established commit-and-push flow, then pins worker
checkouts to those commits. A queued request tests sources at execution time.
Unchanged jobs can reuse verified results unless `force: true` is supplied.
Focused requests do not trigger post-deployment or email verification. P2P and
visual checks run only when explicitly selected (or visual via a whole-tree path).
The bounded inbox accepts up to 100 unfinished requests and shows the latest 100.

## Encrypted recovery copies on every enrolled device

`POST /api/v1/master/backups` with `{}` starts replication of the latest complete,
verified production DR generation, retaining current public dataset versions. Full historical public dataset versions remain on PILEDRIVE. `GET /api/v1/master/backups` reports per-device
bytes, status, failures and final verification receipts. Both require the master
Bearer token. MCP exposes the same `create_backups` and `get_backups` operations.

The master preserves all private records, databases, uploads, credentials and source code, plus the current USAJobs and vacant-property dataset versions. It encrypts this scoped snapshot and every referenced object blob into
AES-256-GCM parts, wraps its random data key with the recovery host's RSA public
key, and verifies the archive by decrypting and hashing every member before sending.
Agents authenticate through the existing enrolled test-server protocol and verify
all encrypted part hashes again before committing an immutable generation. Copies
live in each enrolled launcher's persistent config directory under `backups/`,
separately from disposable test jobs. An interrupted copy resumes verified parts
on the next request; failed/offline devices remain visible. This operation never
copies the recovery private key, decrypts production data on test workers, deletes
old backups, or starts production traffic. It waits for current jobs before agent
updates. This is an explicit snapshot copy; repeat the request after later DR runs.

To restore a downloaded generation into an empty private directory:

```sh
python3 scripts/fleet_backup.py /private/downloaded-generation --restore /private/restore --key /private/recovery-private.pem
```

Keep an independent protected offline copy of the recovery host's
`~/.config/organization-recovery/private.pem`: it is required to decrypt these
replicas after loss of the recovery machine. The REST status contains no plaintext
application records or application secrets.

## Runtime statistics

`GET /api/v1/master/runtime-stats` and MCP `get_runtime_stats` expose persistent
statistics, optionally filtered by `host` and `task`. Each test-file/build job
records worker-measured monotonic durations for actual execution, dependency
installation and waiting for another job's installer. Total job duration includes
those phases and container overhead. Individual assertions within a test file
are counted, but do not currently have separate duration samples.

Fresh passing executions contribute to min/median/p95/max; failures and reused
results are counted separately. Missing measurements remain missing rather than
becoming zero-second tests. Groups include machine identity, so replacing a host
with another machine does not combine their results. Each latest sample carries
its run ID, source digest, input fingerprint and timing source. Comparisons span
revisions and cache states; they are observations, not controlled benchmarks.

SQLite samples survive run-log pruning and are retained for 180 days (pruned on
recording). Reports use the latest 100 observations per task/machine, up to 200
groups and 20,000 recent observations; `truncated` indicates the report limit.
Repeated final saves update the same sample instead of counting it again. Existing
run history can be imported with `runtime_stats.record`; older phase timings are
explicitly marked `coordinator_estimate`.

Request records also retain `queue_seconds`, `duration_seconds` (execution wall
time including preparation), and `total_seconds` from submission to completion.
Parallel job durations must not be summed and presented as elapsed run time.

### USAJOBS mobile browser coverage

Request `{"suites":["usajobs"],"force":true}` through the test-request endpoint
(or select `CodeCollective/usa/index.html`). The `usajobs` file job uses the pinned
CodeCollective commit, a matching Playwright package/browser image, and local
synthetic snapshots. It checks visible scrollable results, touch navigation,
search, history selection, and filter reset at phone, landscape, and desktop sizes.
It requires no production data or credentials and runs with external networking
disabled during the test phase. CodeCollective is included in source publication,
worker checkout, preparation, and cache fingerprints for this suite.

## Phone / internet Codex controls

`https://compai.lifetech.fyi/` serves the dedicated owner-authenticated Codex page.
The HTTPS tunnel routes only to the loopback service `api.py` on port
8091. It does not publish the distributed test master or Docker. The existing
LAN Codex route stays blocked.

Open `https://compai.lifetech.fyi/` on the Galaxy and choose **Sign in with PIdP**,
or visit `/auth/login` directly. Swarm Control uses its own OAuth client,
`https://compai.lifetech.fyi/api/swarm` audience, and `swarm:control` permission.
S256 PKCE and single-use browser state protect the code exchange. The callback
validates PIdP's `iss` parameter. ES256 verification and authenticated resource
introspection verify the issuer, audience, client, scope, active grant, and the
explicitly configured canonical owner. PIdP's verified identity links allow the
same person to use linked credentials; emails never assign controller ownership.

See `swarm-control/README.md` for initial owner registration. Private identity
configuration and device records live under `~/.config/swarm-control/`. No PIdP
access or refresh token is given to the remote browser. Sign-in adds an independent controller without disconnecting other browsers.
Reauthentication rotates only the current browser; Lock revokes only that session. Device
records retain a label and creation, expiry, and last-seen times. Local QR pairing
remains an OS-owner-authorized alternative. Revocation invalidates transfer
permissions. Read history, choose a thread, then Send a new
message or Steer a running turn. Existing VS Code thread permissions apply.

The public service requires its configured HTTPS Host, validates browser Origin
on commands, bounds requests, disables framing/caching, serves only a fixed asset
list, and exposes only the authenticated Codex read/command routes. The origin
port binds to loopback. The tunnel is outbound; no router port forwarding is
needed. The app uses ordinary HTTP behind TLS and can be served by another HTTPS
reverse proxy by setting `CODEX_PUBLIC_ORIGIN`.

Both user services are enabled for restart: `swarm-control-api.service` and
`swarm-control-tunnel.service`. Their reviewable unit files are under `systemd/`.
The connector binary is installed in `~/.local/bin/cloudflared` and its tunnel
credential is an owner-only file at
`~/.config/map-master/compai-tunnel-token`. Never commit or share that credential.
The Cloudflare connector is transport infrastructure; application authorization
continues to use the independent owner credential on CompAI.

Create a phone setup link locally with `python3 swarm-control/server/api.py pair`.
It is saved privately at `~/.config/map-master/codex-pairing-link`; the newest
link invalidates any previous unused link. Send that single-use link privately
to your own phone, open it in the browser you want to pair, and bookmark the
plain site address. The pairing token is in the URL fragment, so it is not part
of the HTTP request URL, and is removed before exchange. Session and invitation
values are stored only as hashes in the owner-only `codex-devices.json` file.
Use `python3 swarm-control/server/api.py revoke-devices` locally to revoke all
phones, or Lock on the site to revoke only that browser. Never share a setup
link: whoever redeems it gets owner-level Codex control for that device.

The CompAI local launcher (`http://127.0.0.1:8791/launcher`) includes **Pair a phone
with CompAI**. **Generate phone setup code** displays the one-use QR, setup link,
copy action and ten-minute countdown. Expired codes disappear; a new code
replaces the previous unused invitation. The code is generated entirely locally.

Code issuance uses a separate loopback service on port 8092, with its own scoped
`~/.config/map-master/pairing-token`. Only the CompAI owner launcher receives a
private copy as `~/.config/lifetech-launcher/owner-pairing-token`. This credential
is not the fleet key, master token, or Codex owner token and is not provisioned
to workers. Browser-direct requests to the issuer are rejected; it is not routed
through the public tunnel. The launcher checks its coordinator role and its
existing loopback Host/Origin restrictions before requesting a code.

For a new coordinator installation, create the scoped credential with
`python3 swarm-control/server/master_api.py create-token --scope pairing`, copy it to the
owner launcher's private configuration as above with mode 0600, and install
`requirements-codex.txt` into `~/.local/share/compai-codex/python` for the dedicated
Codex service. The pinned QR encoder generates SVG locally and needs no image or
npm dependencies. The user service sets PYTHONPATH to that private dependency
folder. Ordinary test containers have neither issuer credentials nor the owner
launcher configuration; loopback access alone cannot issue a code.

Pairing issuance also requires an authenticated owner browser, not merely a
loopback connection. Launch `./run.py --swarm` on CompAI: the OS-owner process
creates a private, single-use bootstrap and opens the launcher with it. The
fragment is removed, the bootstrap is consumed, and a 30-minute capability is
held only in that tab's memory. Ordinary native HTTP requests and host-networked
test containers cannot mint phone setup codes by forging Host/Origin headers.
Reload or expiry requires reopening through `run.py --swarm`. The generated
phone session continues to last 30 days independently.
