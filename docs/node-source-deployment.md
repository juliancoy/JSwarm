# Mounted node source and Git updates

The node image contains OS and Python dependencies. Application code, UI assets,
Docker utilities and the monitor are read from host-backed source directories.
`run.py` mounts the Organization checkout at its original path and read-only at
`/opt/swarm-node`; job and configuration state keep their existing mounts.
The original workspace mount remains writable for services that manage state there.
The running agent uses its selected checkout under the configuration bind mount;
the original checkout supplies only the bootstrap and local runner.

`./build.sh` builds the dependency image. Launching or recreating the node checks
an image label against the Dockerfile and resolved dependency requirements. It
runs `./build.sh` only when that fingerprint changes or the image is absent.
Changing Python, JavaScript, or monitor implementation code does not rebuild the
image. Monitor dependencies come from `ramtop/pyproject.toml`; other runtime
requirements are in `requirements-node.txt`. Existing images need one migration
build because they contain application code and lack the dependency fingerprint.

## Standard fleet update

The controller owns validation, repository credentials, bundle preparation and
rollout. Nodes need no GitHub access, build tools, deployment branches or manual
update commands. They retain their enrolled identity, Docker, Python/Git runtime,
a small supervisor, and persistent bind-mounted source and state directories.

1. Commit the Organization, JSwarm and HarborGenetics changes on the controller.
   Run `python -m scripts.deploy_swarm_inputs --publish`. Publication validates
   those exact commits and then rolls out the selected release. Use
   `--publish-only` to prepare a release without activating it.
2. The controller delivers missing pinned Git objects over the existing enrolled
   LAN transfer service. Mutually authenticated HTTPS is preferred, including for
   small source bundles; the encrypted signed command channel remains a fallback.
   No new Git server, node-side repository credential or Internet route is needed.
   Transfers use enrolled hostnames and the existing IPv4/IPv6 discovery and
   connection fallback. Source checksums and enrolled machine identity are checked.
3. Each node imports the verified bundles with `git fetch` and checks out pinned
   detached worktrees in the persistent configuration volume's `node-source/`.
   Missing objects fail staging; nodes never fall back to Internet origins.
4. Activation writes a small `source.json` descriptor and atomically switches
   `agent-releases/current`. The supervisor executes `launcher_peers.py` directly
   from that mounted Git checkout, with imports, monitor and web assets pointing
   at the same workspace. No agent bundle is generated on the node.
5. The controller verifies the running revision and readiness. The supervisor
   restores the retained previous release if startup fails. Existing Docker job
   containers remain running while the node agent is replaced.

`git fetch` downloads objects; it does not change a checkout or loaded Python
modules. The standard update command handles fetch, checkout selection and process
replacement together. Code-only updates require neither an image build nor manual
node intervention. Changes to dependencies still require provisioning the updated
dependency image; a source rollout does not install packages or silently build.

The first rollout from the old supervisor verifies its actual writable source
bind mount and persistent configuration bind mount, installs the new bootstrap,
and restarts only the enrolled `swarm-node` container. Later bootstrap changes
reload the supervisor automatically. A node without these mounts is rejected
before activation and must be recreated using the standard runner. Legacy native
or custom runners need migration to that mounted container layout.

Modified managed worktrees, corrupt transfers, unavailable pinned source or an
identity mismatch leave the active release selected. Developer working trees are
never reset, stashed or cleaned. Existing legacy publications remain readable and
retain their original deployment/rollback path; new committed publications use
mounted source. Rollback retains both Git checkouts and legacy runtime releases.

## Parallel updates

For an explicitly concurrent rollout, run `python -m scripts.swarm_node update
--parallel`. All selected nodes update against one pinned publication under the
same fleet lock, with up to five node updates in flight. A failed node is reported
independently; other enrolled nodes can finish. Each node must still pass its
identity, source revision and readiness checks. Per-node timings are recorded in
the deployment receipt.

Node activation updates the node process. HarborGenetics orchestration is a
separate running process: deploy its orchestration from the same managed Git
workspace with `genomics_followup_controller.deploy_orchestration`. That procedure
preserves the scientific executor binding and hands off existing queue claims;
fetching a checkout alone does not update modules already loaded by a controller.

## mDNS endpoint ownership

The normal node supervisor binds its swarm API to current UP physical Ethernet
and Wi-Fi IPv4 interfaces. Saved enrollment addresses do not select listeners.
Its DNS-SD service publishes a dedicated name such as `compai-swarm.local`,
separate from Avahi's `compai.local` host records, which may include Docker bridges.
Swarm discovery and node web proxy connections use service hostnames. A legacy
`hostname.local` lookup is retained while nodes receive a rolling update; no saved
IP endpoint is used as a discovery fallback. Enrollment still authorizes node
names, authenticated status must attest a resolved physical address, and existing
worker admission holds remain effective. Explicit WAN enrollment keeps its
existing authenticated gateway path. Standard source publication and fleet update
install this behavior without changing Avahi configuration or rebuilding images.

## Private repository source

All three repositories, including private HarborGenetics source, are delivered by
the controller. GitHub credentials remain on the controller. The earlier manual
`seed_git_source_mirror` step is unnecessary for mounted-source publications; it
remains available for older publications only. Local cached commits require no
transfer. Failed or interrupted delivery can be retried without selecting a new
active release.
