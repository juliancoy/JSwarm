# LAN job acceptance contract

Acceptance is an authenticated owner-policy and CPU/RAM admission decision.
An otherwise eligible worker is not rejected because an interface is busy, Wi-Fi
is present, a transfer takes longer than an RPC timeout, or another peer is offline.
Network observations are scheduling hints, not proof that input bytes are ready.

The covered lifecycle is:

1. Transfer into a private partial file; publish only after full SHA256 validation.
2. Retain an incomplete prefix on disconnect; resume and verify the entire file.
3. Reuse verified staged data across owner pauses and capacity races.
4. Acquire the exact integer CPU/RAM reservation, including RAM scratch.
5. Re-attest machine identity and owner policy immediately before compute starts.
6. Retain accepted work across transient polling failures and preserve event cursors.
7. Confirm container cleanup before releasing its resource reservation.

Host regression command (no VM required):

```sh
PYTHONPATH=.:scripts:swarm-control/server python -m pytest -q \
  tests/test_lan_job_acceptance.py tests/test_swarm_runtime.py \
  tests/test_wgs_download_retry.py tests/test_alignment_scheduling.py \
  tests/test_resource_renewal.py \
  swarm-control/server/tests/test_shared_resources.py \
  swarm-control/server/tests/test_live_coordination.py \
  swarm-control/server/tests/test_granular_dispatch.py
```

The LAN acceptance tests use synthetic local data and actual file-transfer
subprocesses, with controlled agent/container boundaries. They validate lifecycle
and races; they do not measure Ethernet throughput, real packet loss, multi-host
fairness, or actual wall-clock lease renewal during a long LAN transfer. Those
require a separately identified swarm integration run; passing host regressions
must not be described as a network stress test.
