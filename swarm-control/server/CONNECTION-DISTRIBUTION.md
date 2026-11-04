# Connection distribution plan

Keep Comp AI as the gateway observer and test coordinator. First reduce redundant
browser reads: share recent snapshots across panels and embedded frames, and cache
finalized run artifacts. Hidden views already suspend their polling.

Next, give each enrolled worker a local observation feed for its own sockets and
interface counters. Include source identity, observation time, sequence number and
coverage in each sample. Keep gateway observations separate to avoid double counting.

Have the coordinator subscribe once per worker and retain its latest sample. Serve
one browser subscription carrying changed records, with a full snapshot on initial
connection or after a sequence gap. Use bounded queues and disconnect slow consumers;
reconnecting clients fetch a current snapshot rather than replaying an unlimited log.

Finally, let authorized browsers retrieve large immutable artifacts directly from
the owning worker using scoped, expiring access. Keep coordinator proxying available
for unreachable workers. Verify IPv4/IPv6 paths, reconnects, stale workers and measured
bytes before replacing the central delivery path. Do not distribute gateway-only
capture to workers that cannot observe it.
