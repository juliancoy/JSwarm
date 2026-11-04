# Replace nodes and controllers without draining jobs

Use the existing enrolled machine name, Docker names and labels, immutable
release, node journal, resource pool and submission queue. Do not introduce a
second ownership registry, lease transfer system or job-count gate.

1. Build an immutable release and run its required checks before fencing the
   current scheduler. Verify the new image imports its recovery dependencies.
2. Record the existing controller epoch and job identities. Fence fresh claims
   with the SQLite queue epoch; keep accepted node-job contracts unchanged.
3. Start the successor, verify its enrolled machine identity and readiness, and
   recover existing execution by its node journal and Docker job/container IDs.
4. Stop and remove the previous control container as soon as the successor is
   healthy. Do not wait for accepted jobs, browser jobs, input transfers or
   result exports to finish. Those operations must be owned by independently
   supervised node/transfer services and detached job containers.
5. Confirm unchanged container IDs for surviving node jobs, retained verified
   receipts, correct resource accounting, and continued scheduling. A running
   job's lifetime must not depend on its controller client's connection.

For a node update, use the published deployment procedure in
[Swarm Control](../swarm-control/README.md#node-and-job-lifecycle). The supervisor
replaces the node process and reconciles Docker-owned workers before readiness.
Only the prelaunch admission gate is briefly closed; running workers continue.

## Legacy controller-owned pipes

An old alignment worker may still read FIFOs fed by processes inside its
controller. Those live OS pipes are not transferable through a node journal.
Do not claim such an execution can survive controller removal unchanged, or
retain generations of controllers waiting for it to finish.

The GenomeHarbor replacement adapter handles this legacy case explicitly:

- Stop the fenced predecessor once the successor is healthy.
- Preserve verified receipts and durable published results.
- Leave accepted node-owned requests and their workers untouched.
- For each unfinished legacy claim owned by that predecessor, attest the node
  and worker identity, confirm removal of the obsolete execution, then requeue
  that same immutable shard under the successor. Do not replay uncertain work.
- Remove the predecessor and record preserved node jobs, preserved receipts and
  replayed legacy shards in its rollout report.

This is bounded checkpoint/replay, with no job drain. It can repeat unfinished
legacy computation and is not zero-restart execution. New workflows should use
JSwarm's node-owned stream recipes, where the detached worker owns input
validation, mapping and result publication. That is the standard path for
replacement without either draining or replaying scientific work.

Failures to attest or remove a legacy worker leave its claim held for recovery;
never release it speculatively and create duplicate execution. Completed
scientific evidence and source inputs are never deleted during replacement.
