# PIdP-backed private source authorization

For the complete opt-in Crypt4GH workflow, header release, approval CLI and RAM
worker, use [PROTECTED_GENOMIC_WORKFLOW.md](PROTECTED_GENOMIC_WORKFLOW.md). This
document describes the original authorization-only source mode; encrypted-only
source mode rejects its legacy catalogue/offer/grant routes.

The independent source service supports opt-in PIdP authorization in addition to
its existing controller mTLS identity. This protects source catalogue reads,
immutable input offers and output grants. It does not implement genomic file
encryption, a KAS, OpenTDF compatibility, automatic OrgPortal dataset permissions,
or a replacement identity provider. Existing unconfigured sources retain their
current behavior; they must not be described as protected deployments.

## Trust and enforcement

PIdP establishes the person and delegated resource scope. An operator-owned job
approval file establishes dataset permission. A browser's editable identity
profile, email address, administrator flag or OAuth consent never creates a job
approval. The exact PIdP `sub` is used, preserving account namespaces.

Every protected source request requires the controller's existing certificate,
a bearer access token, and `X-Job-Key`. The source calls PIdP introspection over
HTTPS on every request and checks active status, exact issuer, resource audience,
subject, `genomics:data` scope and expiry. Redirects, outages and malformed
responses deny access. Bearer tokens are never persisted or sent to workers.

Approvals bind a job key to the subject, enrolled destination peer and machine,
expiry and SHA-256 digests of exact operations and JSON arguments. Input offers
therefore bind paths, immutable content hashes and byte counts; output requests
bind destinations, filenames and quotas. Approval files are reloaded on every
request. The controller cannot broaden an approval by changing the request.
Use `request_digest` from `scripts.swarm_data_authorization` to generate these
hashes, and independently review the underlying contracts before approving them.
Do not let a job submitter write the approvals file.

Protected `/v1/plan` is intentionally unavailable: approve explicit immutable
`/v1/offers` requests instead of issuing capabilities from mutable shard receipts.
Catalogue requests at `/v1/shards` may be approved separately, page by page.

Input capability identifiers are separated by job and from legacy identifiers.
Output capabilities use a separate protected namespace. Capabilities are bound to
the enrolled worker certificate, and their expiry is capped at the earliest of
60 seconds, token expiry and approval expiry. The bulk transfer service checks
expiry when each GET/PUT starts. A transfer already in progress is not interrupted
at expiry. Revoking PIdP access immediately blocks new source grants; an already
issued capability can start a transfer for up to 60 seconds. No mechanism here
recalls delivered plaintext or keys. Delivery retries need a fresh grant.

These are data-release capabilities, not a claim that worker execution or every
administrative data path has been authorized. Host administrators, controller
administrative commands, previously staged RAM caches and other source endpoints
remain separate trust boundaries. Run only approved nodes; RAM-only policy and
worker admission remain necessary independent controls.

## Configuration

Register a dedicated genomic resource and client in PIdP's existing
`MCP_OAUTH_RESOURCE_CONFIG_JSON` and `MCP_OAUTH_CLIENTS_JSON` configuration. The
resource must permit `genomics:data`; register an exact client redirect and use
the existing authorization-code + PKCE flow. Configure a trusted portal login
mapping where applicable. Do not reuse `swarm:control` as dataset permission.
PIdP's current OAuth server is sufficient for introspection; this integration does
not require or add OpenID Connect discovery or client-credentials grants.
See [PIdP authorization configuration](../../PIdP/serverless/MCP_AUTHORIZATION.md).

On the source host, provision an owner-only `source-authorization/` directory
inside `~/.local/state/lifetech-launcher/bulk-transport/`. Put the resource
introspection secret in `resource.secret` (mode 0600) and job approvals in
`approvals.json` (mode 0600). The PIdP resource configuration stores the SHA-256
hash of the secret. The source needs the secret itself. Keep both out of Git,
command arguments, queue records and release payloads.

Create a configuration JSON with actual absolute source-host paths:

```json
{
  "issuer": "https://identity.example",
  "resource": "https://source.example/genomics",
  "introspection_endpoint": "https://identity.example/oauth/mcp/introspect",
  "secret_file": "/home/operator/.local/state/lifetech-launcher/bulk-transport/source-authorization/resource.secret",
  "approvals_file": "/home/operator/.local/state/lifetech-launcher/bulk-transport/source-authorization/approvals.json"
}
```

The approvals file maps each actual 64-character execution/job key to:

```json
{
  "subject": "owner:actual-account-id",
  "peer": "approved-worker",
  "machine": "actual-enrolled-machine-hash",
  "expires": 1800000000,
  "requests": ["actual-sha256-of-reviewed-operation-and-arguments"]
}
```

The example expiry and placeholders must be replaced. Use atomic file replacement
when updating approvals. The enclosing base directory is mounted read-only into
the source service, so updates remain visible without container recreation.

## Activation

1. Validate the source and bulk transfer changes together using host tests or
   local Docker. Build only through `./build.sh` from JSwarm.
2. Update the bulk HTTPS service to the revision that enforces capability expiry.
   Deploying the source container alone does not update that separate service.
3. Retire legacy input/output capabilities for the protected roots, and account
   for transfers and plaintext caches already in use. Protected source startup
   refuses live legacy capabilities for its configured roots; installation runs
   this check before replacing the current source container. Administrative
   offer creation can still bypass this gate and must remain restricted.
4. Provision PIdP resource/client configuration, the secret and reviewed job
   approvals. Check effective RAM-only worker settings separately.
5. Install the source through its existing command with the added
   `--authorization /path/to/source-authorization-config.json` option. An ordinary
   reinstall cannot silently remove protection from a labeled protected source.
6. Supply a controller-side token provider and job key to the client:

```python
client = SourceClient(owner, token_provider=current_access_token, job_key=execution_key)
client.offers(worker_name, approved_inputs)
```

`current_access_token` is a callable returning the current access token in memory.
Refresh through PIdP on the controller; never store this token in job specs or
worker environments. Existing GenomeHarbor callers without this context will be
denied by a protected source. Workflow-specific login/approval UI and automatic
permission provisioning are not included; provision reviewed contracts and pass
an authorized client before switching a workflow to protected mode.

Structured `swarm.data_access` events record the operation, job digest and allow/
deny outcome without tokens or genomic paths. Collect these logs centrally with
appropriate access, retention and review; container log rotation alone is not a
complete audit system. Correlate the job digest with the separately protected
approval record and execution receipt.

No production configuration or migration is activated by adding this code. It is
an authorization building block, not certification of HIPAA compliance.
