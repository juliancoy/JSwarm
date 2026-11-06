# Swarm prerelease simulation

This host-based test uses a discrete-event clock to run five simulated machines.
It starts no containers, submits no jobs, and changes no live caches or policies.
A captured calibration is sufficient for offline runs; credentials are needed only
when refreshing that calibration.

From the JSwarm directory:

```sh
# Refresh using the signing controller's existing enrolled access.
python3 -m scripts.swarm_prerelease capture --controller compai.local \
  --output .local-reports/prerelease/calibration.json

# Replay 100 independent workloads under each scheduling policy.
python3 -m scripts.swarm_prerelease run \
  --profile .local-reports/prerelease/calibration.json \
  --experiments 100 --jobs 300 --seed 0 \
  --output .local-reports/prerelease/report.json

# Model regression tests, including fault detection.
python3 -m pytest tests/test_swarm_prerelease.py -q

# Required project build.
./build.sh
```

When running directly on the signing controller, omit `--controller`. Otherwise,
the capture command uses the existing host-pinned SSH transport to execute only
read-only authenticated API requests there. The controller must have the normal
JSwarm source layout and enrollment. Each selected machine supplies its own
`resource.capacity` and **all pages** of `worker.records`. Use `--hosts` with five
enrolled names if the fleet contains more than five. An unreachable node,
nonadvancing cursor, page limit, or absence of measured history fails capture;
there is no generic-machine fallback. Increase `--max-pages` for larger journals.
Captures retain resource measurements and execution-stage names, excluding keys,
paths, specimen/job identifiers, and command text. Local artifacts are ignored by
Git. Keep the calibration with the report to reproduce a result.

Only successful CPU executions with positive elapsed time and measured peak RAM
qualify as runtime samples. A configured memory limit is never substituted for
usage. Timestamp differences supply elapsed time where no duration was recorded.
Complete and partial CPU/RAM coverage remain labeled. At most 64 samples per
machine and stage are retained by deterministic hash sampling. Failed, running,
unmeasured, and GPU executions are counted separately from usable samples. GPU
execution and VRAM are outside this model.

Each seed generates overlapping bursts across several waves, reusable and unique
cache datasets, preparation delays, one-retry failures, and per-node admission
pauses. Samples keep their observed CPU reservation, RAM peak, and elapsed time
together. A stage can run only on machines with history for that stage. Both
policies get the identical workload and per-machine runtime samples:

- `round-robin` rotates among nodes that can currently admit a job.
- `cache-aware` chooses the available node with the shortest estimated completion
  time, using that node's stage median plus any cache preparation delay. It does
  not know the eventual sampled runtime.

Active and preparing cache entries remain pinned. Concurrent consumers share one
load. Idle entries expire after the configured TTL or are evicted for admission.
Memory accounts for worker RAM, resident caches, safety headroom, and an assumed
background load. CPU reservations are exclusive for preparation and execution.
Production uses CPU timesharing, so these results describe the model rather than
predict exact production throughput. Host histories also differ in workload and
contention; they are not matched hardware benchmarks.

The JSON report includes the seed and workload hash, calibration hash, all model
settings, completion counts, makespan, tail waits, per-node cache peaks and final
RAM, cache hits/load volume, reservation utilization, and policy comparisons.
The virtual clock jumps to the next arrival, completion, expiration, or admission
state change without sleeping. Reported compression compares the **sum of virtual
time across policy runs** against the batch's wall-clock runtime. Model failures
include residual cache RAM after quiescence, memory overcommit, and pending work
without a future event that could admit it. A bounded trace tail accompanies
failures. The command exits nonzero on any failed run, suitable for a prerelease
gate. A slower candidate policy is reported rather than silently rejected: inspect
paired regressions as well as the median improvement.

Cache sizes, transfers, memory margin, arrival rates, retries and admission pauses
are assumptions because worker journals do not measure all of these. Change
`--cache-mb`, `--transfer-mb-s`, `--cache-ttl`, `--memory-factor`,
`--background-fraction`, `--waves`, `--failure-rate`, and `--pause-seconds` to run
sensitivity experiments. Stage selection is uniform rather than a replay of live
arrival frequencies. Preparation pauses apply to new admissions; already-running
jobs continue. Cache pressure eviction is global across stages on a node, while
ownership is per dataset and node.

Verify the detector itself with negative controls (both should exit **1** under
the default stress workload):

```sh
python3 -m scripts.swarm_prerelease run \
  --profile .local-reports/prerelease/calibration.json \
  --experiments 5 --jobs 300 --inject-cache-leak \
  --output .local-reports/prerelease/leak-control.json

python3 -m scripts.swarm_prerelease run \
  --profile .local-reports/prerelease/calibration.json \
  --experiments 5 --jobs 300 --inject-global-guard \
  --output .local-reports/prerelease/guard-control.json
```

The first retains completed caches. The second blocks cleanup while any queued
preparation remains, modeling the earlier fleet-wide guard failure. These checks
prove the model detects those faults. They do not replace the production cache
ownership tests or an end-to-end deployment qualification, and the simulation
does not change the production scheduler.

## Expanded matrix and completed shards

The `suite` command runs baseline, memory pressure, cache churn, slow transfers,
failure/recovery, and long-soak scenarios. Recovery includes random dependency
DAGs, cancellation propagation, five node outages/restarts, interrupted input
loads, retries, and stale completion events from superseded attempts. Outages
interrupt modeled workers and discard that node's modeled caches; recovery
readmits work. Cancellation after completion preserves the completed result.

Capture aggregate inspiration from completed shards, then include a seventh
scenario:

```sh
python3 -m scripts.swarm_prerelease_shards --controller compai.local \
  --output .local-reports/prerelease/completed-shards.json

python3 -m scripts.swarm_prerelease suite \
  --profile .local-reports/prerelease/calibration.json \
  --shard-history .local-reports/prerelease/completed-shards.json \
  --experiments 20 --jobs 150 --ownership-seeds 20 \
  --output .local-reports/prerelease/expanded-suite.json
```

Completed queue receipts supply method frequencies, deduplicated cache budgets
on the destination machine, cache reuse counts, and dependency fan-in from the
current manifest. Bounded execution receipt samples expose duration differences.
The export contains aggregates, not specimen identifiers or commands. Worker API
histories remain the runtime calibration. Methods without a matching API stage
are listed as unmodeled. Refresh the API calibration when new stage measurements
become available.

The shard-inspired scenario samples methods by observed frequency, cache budgets
by method, and dependency degrees by method. Its DAG is synthetic. Per-job cache
budgets combine references and inputs conservatively; they do not reproduce the
complete network of shared reference caches or source-side RAM allocations.
Recorded cache reservations are budgets rather than measured resident RAM. Other
matrix scenarios continue to stress the synthetic workload independently.

Every suite also calls the **actual production `finished_cache_keys`** against
temporary local SQLite queues. It checks nested source/destination bindings,
CPU/GPU namespaces, active and waiting consumers, completed/failed/cancelled
owners, legacy preparation scope, corrupted queue data, and invalid bindings.
These are production ownership decisions, while eviction, admission and worker
execution remain modeled. The checks never open the live submission database.

Reports now include the worst runtime regression and its seed. Optional
`--max-regression-percent 10` fails the suite if any paired candidate makespan
is over 10% slower. Without that option, runtime regressions remain findings;
cache, ownership and accounting violations still fail the suite. This threshold
is a model policy gate, not a production performance guarantee.

[Historical failure coverage](prerelease-history-risks.md) records which issues
from earlier debugging sessions are exercised and which require separate checks.
