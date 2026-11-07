# Genomic protection implementation plan

## Outcome and boundary

Provide a complete opt-in encrypted genomic job path for managed JSwarm workers:
PIdP identity -> operator-approved immutable job -> encrypted input transfer ->
authorized Crypt4GH header rewrap -> RAM-only analysis -> encrypted outputs.
Existing scientific workflows remain available; selecting the protected workflow
must never silently fall back to them. PIdP keeps identity/OAuth ownership and
external dataset approvals keep domain permissions. No branches or PRs.

## Implementation sequence

1. Add pinned upstream Crypt4GH support, streaming ingestion and header rewrap.
   Bind source catalogues to ciphertext hashes and sizes. Keep custody private
   keys outside data roots; return only headers encrypted for approved recipients.
2. Extend source authorization with encrypted dataset offers and header release.
   Require live PIdP introspection, exact job/request approval and the enrolled
   worker identity. Retain fail-closed behavior and short transfer capabilities.
3. Add tooling for preparing immutable protected contracts, reviewing/approving
   them, token acquisition via existing PIdP PKCE, revocation and preflight.
   Do not put bearer tokens or private keys in job journals or worker arguments.
4. Add a protected GenomeHarbor workflow and recipe wrapper. Generate a fresh
   worker key in RAM, obtain authorized headers, decrypt only to private tmpfs,
   run an installed/pinned analysis command, and encrypt all declared derivatives
   to the configured result custodian before they leave RAM. Bind the analysis
   image/command, inputs, recipient, output limits, worker and job identity.
5. Package the new modules through existing immutable source/image build paths.
   Validate permission failures, wrong recipient, revocation, expiry, ciphertext
   tampering/truncation, plaintext spill prevention, cleanup and output recovery.
   Use synthetic genomes and host/local Docker tests; build via ./build.sh.
6. Produce deployment/preflight instructions and status receipts. Activate only
   configurations with real issuer/resource/client, custody keys, approved workers
   and reviewed job contracts. Report unavailable configuration explicitly; do
   not invent production credentials or migrate existing private genomes blindly.

## Acceptance criteria

- A synthetic protected job executes end to end and its encrypted output decrypts
  only for its approved result custodian.
- Live identity and exact immutable contracts gate every new data/header release.
- No custody private key or user bearer token crosses into a worker job record.
- Plaintext and ephemeral worker keys are restricted to bounded RAM, with swap,
  core dumps, worker logs and persistent caches excluded by execution checks.
- Missing crypto dependencies, authorization, dataset mappings or RAM guarantees
  fail closed. All relevant tests and required builds pass.
- Document trust limits: host administrators can see active plaintext; revocation
  cannot recall released headers/keys, plaintext or an already running transfer.
  Central audit retention, organizational policies, BAAs and recovery drills remain
  deployment responsibilities and are not HIPAA certification by this code.

## Production activation prerequisites

Actual issuer/resource and registered PKCE client; introspection credential;
custody/result keys with protected backups; approved worker enrollment; isolated
private storage; configured audit collection; source and worker deployment with
validated revisions. Source changes alone do not enable production protection.

## Implementation result

The opt-in software path is implemented: upstream Crypt4GH ingestion, in-memory
PIdP PKCE/refresh, reviewed immutable contracts, live-introspected delegated
sessions, worker-bound header release, a RAM-only analysis wrapper, encrypted
result export, approval/revocation/preflight/recipe tools and RAM-only recovery.
See [the operational runbook](PROTECTED_GENOMIC_WORKFLOW.md).

Validation: 81 focused tests passed, including a real local Docker worker,
real mTLS source/result transfers, Crypt4GH recovery, denial paths, CLI lifecycle,
and PKCE/refresh behavior. Identity introspection/token exchange use synthetic
fixtures; a production PIdP connection was not exercised. Both JSwarm and the
protected GenomeHarbor image built through their ./build.sh entry points. The
protected build context is a curated projection of about 127 kB.

Production rollout is not active. The shared host has swap enabled, so it fails
the unmodified production RAM guard. Tests simulate a no-swap kernel view only
for synthetic bytes. Real issuer/resource/client registration, introspection
secret, custody/result keys and backups, approved worker configuration, reviewed
job grants, and audit collection must be provisioned before activation. Existing
private genomic files and running services were not migrated or reconfigured.
