# Encrypted genomic jobs on managed JSwarm nodes

This is the implementation runbook for
[the protection plan](GENOMIC_PROTECTION_PLAN.md). The `genomics.protected.v1`
workflow uses upstream Crypt4GH 1.8.6, existing PIdP OAuth/introspection, the
independent mTLS source service, and installed node-owned recipes. OpenTDF is not
a dependency. Administrative dataset approvals remain outside PIdP.

## What runs where

The source holds ciphertext, an owner-only dataset registry, and custody private
keys outside all input/output roots. A registered native controller signs in to
PIdP using authorization code + PKCE. Access and rotating refresh tokens remain in
controller memory. No refresh retry occurs after an ambiguous failure.

The operator approves an immutable contract containing the exact PIdP subject,
worker, pinned image, command, ciphertext/plaintext identities, output names and
limits, result recipient and RAM budget. A fresh run ID distinguishes new attempts
from earlier immutable publications. The CLI generates exact request hashes.

The source introspects identity before creating/renewing a delegated session.
Only the approved worker's existing mTLS certificate can use its random session
capability. Sessions and delegated access tokens live only in source process RAM,
expire within 60 seconds, and are renewed by the controller every 20 seconds.
Workers receive no user bearer token or custody private key. Session creation and
header/output release check that the separate bulk service advertises expiry
support; an older bulk service cannot silently ignore capability expiration.

At execution the worker generates an ephemeral X25519 private key in its own
process, requests a replacement Crypt4GH header for each input, and verifies the
released job digest before decrypting. The source re-checks live PIdP status and
the approval on every release. Only encrypted headers for that worker key leave
the source. Ciphertext is streamed in one authenticated download per input into
bounded tmpfs, verified, then decrypted to private RAM files. Both ciphertext and
plaintext hashes/sizes must match the contract. Each input can be independently
released so a long earlier download does not consume later offers' start windows.

The reviewed analysis receives `INPUT_DIR`, `OUTPUT_DIR` and RAM `TMPDIR`; inputs
use the approved aliases as filenames. Standard input is closed and stdout/stderr
are discarded. It must produce exactly its declared outputs. Each derivative is
encrypted to the approved result recipient before export, and only ciphertext
receipts cross the node's durable control journal. Delivery gets a fresh grant per
file. Temporary plaintext, ciphertext scratch and ephemeral key references retire
with the worker; Python does not guarantee zeroization of every memory copy.

The source can no longer serve legacy catalogue/offer/grant operations when
`encrypted_catalogue` mode is selected. Existing callers must explicitly select
the protected workflow; there is no plaintext fallback.

## Build and register the worker

Install host/controller dependencies from JSwarm using the shared Python
environment: `~/global/bin/python -m pip install -r requirements.txt`. The host
and node requirements both pin Crypt4GH 1.8.6.

From `HarborGenetics`, run:

```sh
./build.sh --protected
```

The build projects only five reviewed Python modules plus the Dockerfile into a
small temporary context. Private workspace files never enter this build context.
For a scientific toolchain, use `--base sha256:ACTUAL_LOCAL_IMAGE_ID` and a separate
`--tag`. The base must contain Python and the reviewed tools; the default image
supports Python analysis, not BWA/samtools. Image distribution uses the existing
JSwarm image transfer path. Build the node dependency image from JSwarm with
`./build.sh`; it includes the same pinned Crypt4GH dependency.

On the selected worker, install its locally available image into the existing
recipe registry:

```sh
~/global/bin/python -m scripts.protected_genomics_cli install-recipe \
  --image lifetech/genomics-protected:1 \
  --registry ~/.config/lifetech-launcher/stream-recipes.json
```

The command verifies image imports, resolves an immutable image ID and updates the
registry atomically. Record the printed ID for the contract. It does not change
worker storage policy. Configure the existing node to require `ram-only`, verify
host swap is disabled and confirm live capabilities before using this workflow.
The controller rejects a node not reporting RAM-only mode. The worker independently
checks native tmpfs and `/proc/swaps`, and disables core dumps. The standard node
launch sets a read-only root, no worker logs, equal memory/swap limits, dropped
capabilities and no-new-privileges.

## Configure PIdP

Register an exact genomic resource with `scopes: ["genomics:data"]` and the SHA-256
hash of a fresh resource introspection secret in
`MCP_OAUTH_RESOURCE_CONFIG_JSON`. Put the raw secret only in the source's owner-only
`source-authorization/resource.secret`. Register a native public client in
`MCP_OAUTH_CLIENTS_JSON`, with `tokenEndpointAuthMethod: "none"`, exact resource,
`scopes: ["genomics:data"]` and exact loopback redirect
`http://127.0.0.1:8917/callback`. Use the existing trusted portal mapping if login
for this resource goes through OrgPortal. Preserve other resources and clients.
See [PIdP configuration](../../PIdP/serverless/MCP_AUTHORIZATION.md).

The controller's non-secret OAuth configuration is:

```json
{
  "issuer": "https://ACTUAL_ISSUER",
  "resource": "https://ACTUAL_RESOURCE/genomics",
  "client_id": "ACTUAL_REGISTERED_NATIVE_CLIENT",
  "redirect_uri": "http://127.0.0.1:8917/callback"
}
```

Use the actual issuer and endpoint prefix, not a guessed identity host. PIdP's
planned shared-account migration is separate from this change. OAuth consent and
organization membership do not create dataset approvals.

## Encrypt and catalogue inputs

On the source host, provision the existing bulk transport base and dedicated
input/output directories. The following paths are examples, not deployed values.
The `source-authorization` directory belongs under the actual bulk transport base.
Keep it mode 0700; keys, registry and approvals must be owner-only. Back up custody
and result keys securely before using them with real data.

Generate a custody key pair and a separate result-recipient key pair:

```sh
~/global/bin/python -m scripts.protected_genomics_cli keygen \
  --private /PRIVATE/source-authorization/custody.key \
  --public /PRIVATE/source-authorization/custody.pub
~/global/bin/python -m scripts.protected_genomics_cli keygen \
  --private /PRIVATE/result-custody.key --public /PRIVATE/result-custody.pub
```

Private keys are raw 32-byte X25519 values, mode 0600; public files contain the
hex-encoded public key. This is a filesystem custody backend, not an HSM/KMS.
Result private keys need not be present on source or worker machines.

Register an encrypted input without overwriting existing files or dataset IDs:

```sh
~/global/bin/python -m scripts.protected_genomics_cli ingest \
  --input /PRIVATE/original.fastq.gz --output /ENCRYPTED/reads.c4gh \
  --recipient /PRIVATE/source-authorization/custody.pub \
  --custody-key /PRIVATE/source-authorization/custody.key \
  --catalogue /PRIVATE/source-authorization/catalogue.json --dataset reads-opaque-id
```

The CLI checks key correspondence and source stability, atomically publishes
ciphertext, and records ciphertext and plaintext identities. Original plaintext
is not deleted automatically. Its retention and any migration cleanup require a
reviewed data lifecycle. For genomics, ingest reads, indexes and other required
private sidecars as separate aliases. The current worker stages complete encrypted
shards into RAM; it does not yet expose a transparent random-access filesystem.

## Prepare, review and approve a job

Put the reviewed tool argv in a JSON array, e.g. an installed analysis program
that reads `INPUT_DIR` and writes only declared files under `OUTPUT_DIR`. Keep
patient identifiers, sequence literals and secrets out of argv and contracts.
Command wrappers can adapt scientific tools that do not use these environment
variables. Include all private tool outputs in the encrypted output declaration.

```sh
~/global/bin/python -m scripts.protected_genomics_cli prepare \
  --catalogue /PRIVATE/source-authorization/catalogue.json \
  --analysis /PRIVATE/analysis-argv.json \
  --result-public /PRIVATE/result-custody.pub \
  --peer ACTUAL_WORKER --machine ACTUAL_ENROLLED_MACHINE_HASH \
  --image sha256:ACTUAL_PROTECTED_IMAGE_ID \
  --input reads=reads-opaque-id --result variants.vcf=10485760 \
  --cpus 2 --memory-mb 1024 --scratch-mb 512 --output /PRIVATE/job.json
```

Review the contract, including the command, worker, input identities, recipient
and RAM/output budgets. The operator approves it on the source host, against the
exact namespaced PIdP subject:

```sh
~/global/bin/python -m scripts.protected_genomics_cli approve \
  --contract /PRIVATE/job.json --subject owner:ACTUAL_ACCOUNT_ID \
  --seconds 3600 --approvals /PRIVATE/source-authorization/approvals.json
```

Approvals are limited to 24 hours. Do not give submitters write access to the
approval file. Reviewers must independently establish project membership,
permitted dataset use and any clinical/research restrictions.

## Install the encrypted source

Use the source authorization settings described in
[GENOMIC_AUTHORIZATION.md](GENOMIC_AUTHORIZATION.md). Install through the existing
`deploy_node_source` command, adding both:

```text
--authorization /LOCAL/source-auth-config.json
--encrypted-catalogue /SOURCE_ABSOLUTE/source-authorization/catalogue.json
```

The registry and secret paths must be under the source's bulk transport
`source-authorization/` directory; custody keys must be outside payload roots.
Update the separate bulk transport service first. Retire live legacy capabilities
before activation; startup refuses those for configured roots. Routine source
reinstallation refuses to remove either authorization or encrypted-only mode.
These steps do not modify existing private datasets or running nodes implicitly.

## Preflight and run

On the controller, with the reviewed contract and enrolled source identity:

```sh
~/global/bin/python -m scripts.protected_genomics_cli preflight \
  --contract /PRIVATE/job.json --oauth /PRIVATE/genomics-oauth.json \
  --source ACTUAL_SOURCE --source-machine ACTUAL_SOURCE_MACHINE_HASH
~/global/bin/python -m scripts.protected_genomics_cli run \
  --contract /PRIVATE/job.json --oauth /PRIVATE/genomics-oauth.json \
  --source ACTUAL_SOURCE --source-machine ACTUAL_SOURCE_MACHINE_HASH \
  --receipt /PRIVATE/encrypted-result-receipt.json
```

Both commands authenticate through the browser; tokens stay in RAM. `--base`
selects a different existing controller mTLS configuration directory. The source
stores encrypted results under its first configured output root and the approved
job key. A controller renews session identity while the job queues, computes and
publishes. Renewal failure requests node cancellation. Loss of the controller or
source session requires a new delegated session; released plaintext/keys cannot
be recalled, and an active transfer can finish. There is no hidden offline bypass.

A fully completed durable node receipt can be inspected through existing node
status APIs. To rerun after uncertain or partial encrypted publication, prepare a
new contract/run ID and obtain a new approval. Immutable prior files are never
silently replaced by newly randomized ciphertext.

## Revoke and recover

Remove an approval with:

```sh
~/global/bin/python -m scripts.protected_genomics_cli revoke \
  --job-key ACTUAL_JOB_KEY --approvals /PRIVATE/source-authorization/approvals.json
```

Future source sessions and header/output releases fail immediately. An already
issued transfer can start until its short expiry, and in-progress transfers and
released keys remain usable. Revoking the person's PIdP connection/account also
denies subsequent introspection. Update enrollment at the source when revoking
worker certificates; node credentials are an independent trust boundary.

Recover a result using its ciphertext hash from the verified receipt and the
result recipient's private key:

```sh
~/global/bin/python -m scripts.protected_genomics_cli recover \
  --input /ENCRYPTED/variants.vcf.c4gh --sha256 ACTUAL_RECEIPT_SHA256 \
  --private /PRIVATE/result-custody.key --ram-output /dev/shm/variants.vcf
```

Recovery verifies ciphertext integrity and refuses a non-tmpfs destination or a
host with swap enabled. It never overwrites an existing output and removes failed
partial plaintext. Custody-key backups and encrypted-result backups require
independent access controls and periodic recovery drills.

## Validation and remaining deployment responsibilities

Focused host tests exercise real Crypt4GH and mTLS, malformed/tampered inputs,
wrong recipients/workers, revocation, changed analysis contracts, PKCE state and
serialized refresh. `PROTECTED_DOCKER_TEST=1` enables the actual Docker worker
integration test. The shared development host currently has swap enabled; the
synthetic tests simulate an empty `/proc/swaps` view only for synthetic bytes.
That does not establish this host as ready for PHI, and production guards remain
unchanged.

Worker recipes use JSwarm's existing host networking for authenticated transfers.
Analysis programs and host/controller administrators are trusted; this is not
confidential computing, remote attestation, or a sandbox for hostile binaries.
Persistent control records include dataset identities and must themselves be
access-controlled. Audit events contain operation and job digests, without token
or genomic payloads; collect and retain them centrally. This implementation does
not provision organizational policy, BAAs, physical safeguards or a HIPAA
certification, and it has not activated any production issuer or cluster rollout.
