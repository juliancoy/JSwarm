# Node-owned streaming job lifecycle

An accepted job and its immutable contract belong to the node. Closing a client,
replacing a manager or timing out a client's wait leaves the job running.

New `stream.submit` declarations may include a `lifecycle` object. The node pins
the full effective policy in the accepted contract; replaying the original request
returns that same contract. All durations are nonnegative integer seconds.

| Field | Default | Meaning |
| --- | ---: | --- |
| `queue_seconds` | 0 | Maximum queue/admission wait for each attempt; zero disables. |
| `execution_seconds` | 0 | Execution budget per attempt, excluding verified-result delivery; zero disables. |
| `stall_seconds` | 3600 | Maximum interval without measurable progress during execution; zero disables. |
| `delivery_seconds` | 0 | Budget in the result-delivery phase; zero preserves verified RAM outputs through sink outages. |
| `retry_seconds` | 86400 | Elapsed automatic recovery budget, beginning with the first interruption. Checked before starting another attempt and while awaiting admission. |
| `max_retries` | 8 | Maximum automatic retries following interruptions or admission errors. Zero requires explicit retry. |

Phase changes and counters such as completed reads or transferred bytes count as
progress. Heartbeat timestamps, changing ETA/rate estimates and error messages do
not. Existing accepted contracts keep their identity and acquire no implicit stall
deadline during an upgrade. Automatic recovery uses exponential backoff capped at
five minutes. Integrity failures require corrected inputs and a new contract.
`stream.retry` requires worker cleanup to have finished and resets recovery and
queue budgets. It does not change the accepted contract.

A deadline durably requests a stop with target `failed`. Owner cancellation uses
`stream.cancel` and target `cancelled`. Both enter `stopping` before any Docker
operation. A bounded background pool performs graceful stops, confirms worker
absence and removes stopped containers before releasing leases and publishing the
terminal state. Lost acknowledgements, unknown daemon state and restart recovery
keep the request pending. Cancellation responses are acknowledgements of intent;
poll `stream.status` for terminal confirmation. Cancellation accepted before result
reconciliation wins; a receipt already reconciled as completed stays completed.

`stream.remove` accepts terminal work only. It confirms cleanup, writes a private
JSON audit archive including transitions, and moves the contract and receipt to a
retained tombstone. Removed jobs leave `stream.list`, while `stream.status`,
`stream.timings` and identical resubmission still resolve to the retained outcome.
Changed contracts cannot reuse the ID. Archived jobs require a new ID to execute.
RAM-only/builtin per-job scratch is removed; external results, shared caches and
arbitrary durable recipe artifacts are preserved.

Nodes archive eligible terminal entries after seven days by default, in batches of
100 at most once a minute. Owner configuration `stream_retention_seconds` changes
this period; zero disables automatic archival. Active jobs and entries awaiting
worker cleanup are never eligible. Archives live beside the journal under
`.stream-work/archives`, with owner-only permissions. Tombstones and audit archives
are retained for replay safety and investigation; retention does not erase verified
scientific results.

GenomeHarbor qualification fixtures declare their own node-side queue/execution
and stall budgets. Their client also waits for confirmed cancellation after its
qualification deadline; production jobs are excluded by fixture ID validation.
