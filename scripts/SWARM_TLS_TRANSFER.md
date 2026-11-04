# Private bulk transport

`swarm_tls_transfer.py` provides an optional LAN HTTPS path alongside enrolled
SSH transfer. It is independent of conversation and launcher services.

Provision the server address certificate, a dedicated client-auth certificate,
and CA through pinned SSH. Keep keys and manifests owner-only. Client private
keys must be generated on the destination. Bind the server to the source LAN
address; do not publish the port through WAN proxies or tunnels. The certificate
SAN must match the address used by the client. TLS 1.3 and a valid client
certificate from the dedicated CA are required; plaintext fallback is absent.

The server manifest maps opaque object IDs to absolute `path`, `sha256`, and
`identity` fields. Identity is `[device,inode,size,mtime_ns,ctime_ns]`, captured
when SHA256 is calculated. Only explicit manifest objects are served. Manifest
updates must use atomic replacement. Changed source identities are rejected.
HTTP Range and If-Match allow resuming against the same immutable receipt.

The fetch command stores resumable bytes in `.rsync-partial/<filename>`, hashes
the entire result (including the resumed prefix), and only publishes after
SHA256 and length match. File and directory fsync protect final publication.
It uses a destination `.transfer.lock`; SSH rsync fallback must use
`--rsync-path='flock -n <destination>/.transfer.lock rsync'` to avoid simultaneous
writers after a connection loss. Integrity failures exit 78 and must stop
rather than silently fall back. Connection failures can retry through SSH.
Renew certificates before expiration; connection verification is never disabled.

The server requests kernel TLS when Python exposes `OP_ENABLE_KTLS` and uses
`SSLSocket.sendfile`. Actual acceleration depends on Python, OpenSSL and kernel
support. Check `/proc/net/tls_stat` during transfer. `TlsCurrTxSw` indicates
kernel TLS software encryption, not NIC TLS offload or RDMA. A transport receipt
does not replace application validation: original-read replication still
checks decompressed bytes against the canonical alignment input.

Host tests: `PYTHONPATH=. python -m pytest -q tests/test_swarm_tls_transfer.py`.

## Default routing and enrollment

The shared `swarm_runtime.copy_verified_file` prefers direct HTTPS for objects
at least 64 MiB. This is a routing threshold, not a size cap. Configure `min_bytes`
in the enrolled controller's `bulk-transport/config.json` to adjust it. Smaller
objects and peers without a working HTTPS endpoint retain pinned SSH. Reference/index distribution uses the shared verified path as well. Cache hits
are checked before opening a source stream. Receipts record `transport` for new
HTTPS copies, and operational progress includes the transport and verified bytes.
Arbitrary command pipes continue to use their existing stream protocol.

Run `python scripts/deploy_swarm_bulk.py` from the controller checkout to install
immutable code releases and separate LAN-only port-9448 services. Deployment
uses `LauncherUpdates.command`, checks agent machine identity and enrolled owner
configuration over pinned SSH, and generates each private key on its node. It
never restarts launcher, conversation, or compute services. Nodes that are offline
retain SSH fallback. Re-run deployment when they return. The bulk service itself
reloads code/certificates when idle; an active transfer can defer that reload.

The controller keeps the dedicated CA key; peers receive only its public
certificate and their own identities. Each object offer authorizes the precise
destination certificate. Clients verify the CA, address SAN, and enrolled source
public-key pin. Manifest-directory services reject entries without destination
ACLs. Source and destination both must match the authenticated machine IDs in the
controller's enrollment. Certificates renew on deployment when fewer than 30 days
remain; daily deployment maintenance keeps enrollment and certificates current.
Do not copy the earlier single-purpose original-read CA into this general fleet.

The new default applies to newly loaded controller runtimes. Already-running
controllers retain their loaded code until their normal checkpoint/restart;
active transfers and scientific jobs are not interrupted for this migration.

## Overlap transfer and verification

`swarm_stream_verify.py` checks a gzip object from an enrolled, pinned mTLS source
using a 1-MiB working buffer. It checks gzip CRC, compressed SHA256, compressed
length and decoded length, and emits the decoded SHA256 for comparison with the
canonical input. It writes progress separately from the final receipt. An object
can be copied to a backup node while a different admitted worker validates its
contents. Accept the backup only when its independently verified compressed
SHA256 equals that content-checked object's SHA256 and the decoded hash equals
the canonical input. Recheck source and destination identities at publication.
This avoids staging another whole file or repeating decompression on a busy node.
Stream checks themselves restart after interruption; file downloads retain resume.

`swarm_pressure_guard.py` drains **new** work after two samples show at least 30%
CPU I/O wait and 50% I/O pressure. It restores admission after three recovery
samples (I/O wait below 15%, I/O pressure below 25%, memory pressure below 5%).
The guard uses identity-checked standard SSH deployment access, retains existing
jobs, and only removes maintenance markers it created. Existing owner maintenance
and worker policy are preserved. These are congestion thresholds, not data-size
or task limits. Unknown measurements do not prove recovery.
