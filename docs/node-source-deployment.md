# Mounted node source and Git updates

The node image contains OS and Python dependencies. Application code, UI assets,
Docker utilities and the monitor are read from host-backed source directories.
`run.py` mounts the Organization checkout at its original path and read-only at
`/opt/swarm-node`; job and configuration state keep their existing mounts.
The original workspace mount remains writable for services that manage state there.

`./build.sh` builds the dependency image. Launching or recreating the node checks
an image label against the Dockerfile and resolved dependency requirements. It
runs `./build.sh` only when that fingerprint changes or the image is absent.
Changing Python, JavaScript, or monitor implementation code does not rebuild the
image. Monitor dependencies come from `ramtop/pyproject.toml`; other runtime
requirements are in `requirements-node.txt`. Existing images need one migration
build because they contain application code and lack the dependency fingerprint.

## Standard fleet update

1. Commit and push the Organization, JSwarm and HarborGenetics changes to origins
   reachable by every node. No deployment branches are needed.
2. Run `python -m scripts.deploy_swarm_inputs --publish`. Validation exports the
   exact recorded commits, excluding local edits and untracked data. The release
   records all three origins and commit IDs, plus its Git installer, under the
   existing publication integrity checks.
3. Run `python -m scripts.swarm_node update` (optionally `--node NAME`). The
   existing signed node command API fetches the pinned commits into dedicated
   detached worktrees under the persistent config directory's `node-source/`.
   Containers already mount this directory, so no image rebuild is required.
4. Each node regenerates the existing checksummed agent runtime from its fetched
   JSwarm and HarborGenetics source. It must exactly match the validated publication
   before bootstrap files or active release pointers are changed. The supervisor
   activates that runtime and the controller verifies readiness and revision.

The generated runtime remains a small, immutable projection of the Git source:
embedded streaming/command support, monitor sources and UI assets keep the
existing native and container node protocols compatible. It is stored on the host
configuration volume, rather than in a Docker layer. Docker workers keep running
while the agent process is replaced. Existing rollback uses retained releases;
it does not reset a developer checkout or rebuild an image.

Git operations are noninteractive. Missing repository credentials, unreachable
origins, missing commits or modified managed worktrees stop staging and leave
running agents and jobs in place. Enrollment must supply Git access separately
from controller signing credentials. Publications without Git provenance are rejected for deployment and rollback.
Run `--publish` from committed source to replace an old desired publication. Old
receipts are retained only for audit history and generation ordering. The updater
does not bridge or restart legacy draining supervisors; recover an unsupported
bootstrap with the normal local runner before retrying.
Dependency changes require updating the dependency image on each container host
using `./build.sh` and recreating its node container; fleet code updates do not
silently rebuild Docker images or install Python packages.

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
