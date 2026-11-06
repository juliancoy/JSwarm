# Historical failure coverage

These risks came from local Codex conversations on November 5–6, 2026 and
inspection of completed follow-up shard receipts. Historical reports are evidence
for regression cases, not assertions that the same problems remain live today.
No credentials, raw conversation dumps or specimen data are stored here.

| Historical finding | Risk to exercise | Current coverage |
|---|---|---|
| MiniPC had 40 cache owners; admission retries supplied no eviction candidates | Low CPU utilization with no room to start compute | Memory-pressure model and terminal-only progress check against the production ownership policy; actual eviction/admission call plumbing still needs its existing runtime tests |
| Any unfinished preparation disabled cleanup fleet-wide | A waiting job on one node stalls unrelated nodes | Global-guard fault control; production queue audit checks legacy preparation on the same, other and unknown nodes |
| Prepared/waiting jobs could lose data before Docker attachment | Pressure eviction deletes inputs still owned by unfinished jobs | Production audit protects queued, accepted, preparing and unknown-state consumers across nested bindings and namespaces; concurrent modeled consumers remain pinned |
| Unpublished shared-reference locks held multiple assignments in preparation | Readiness/admission deadlock before compute launch | Slow-load and shared-load scenarios, interrupted preparation and restart recovery; actual distributed lock/probe behavior is not executed |
| About 29 GiB of superseded staging data filled coordinator `/dev/shm`; logs said `No space left on device` | RAM-backed scratch can fill independently of host admission RAM | Cache churn and interrupted-load cleanup stress RAM budgets; a distinct tmpfs/scratch quota is **not modeled** |
| A worker finished but later Docker inspection timed out | Retrying already-completed work or releasing ownership without confirmation | Stale attempt completions and cancellation-after-completion semantics are modeled; controller/retirement host tests separately exercise actual receipt recovery and cleanup timeouts |
| Startup cleanup scanned the complete queue for each historical job | Recovery cost grows quadratically and blocks dispatch | Existing controller host regression checks one background sweep per node; the discrete-event model does not estimate real queue scan cost |
| Free RTX VRAM coexisted with stale/unknown GPU inventory | GPU work stalls despite apparently sufficient hardware | **Not covered by the CPU simulator**; requires actual inventory freshness, GPU lease and route tests |
| Fixed dispatcher lanes limited work despite spare resources | Artificial concurrency cap hides available capacity | Model dispatch uses live modeled capacity without fixed lanes; production dispatcher concurrency needs its existing tests |
| Valid regional BAMs had no reads and phasing exited with code 1 | Empty evidence gets treated as execution failure or invented scientific evidence | **Not covered by resource simulation**; retain scientific empty-input controls |
| Completed Vulkan receipts lost execution backend metadata in the UI | Pipeline incorrectly labels GPU work as CPU | **Not covered**; keep pipeline projection/backend-evidence tests |
| A repair deployment differed from the authoritative published release | Standard update can overwrite a working fix | **Not covered**; retain standard publication/update verification |

The shard inspection also found substantial workload variation: calling,
comparison, projection, phasing, structural work and report aggregation have
different cache budgets and dependency counts. Comparisons can have dozens of
parents; shared references coexist with per-job inputs. The completed-shard
export records these shapes, and the suite reports any methods for which worker
API histories do not yet provide a matching execution stage.

Relevant conversations are titled “Check MiniPC JSwarm API”, “RAMspiron”,
“WAN Connection”, “GPU”, and “Web UI” in the local Codex session index. The
production ownership audit plus selected controller/retirement tests narrow the
gap between a model-only result and implementation coverage. They do not execute
a full production swarm or replace scientific result-equivalence qualification.
