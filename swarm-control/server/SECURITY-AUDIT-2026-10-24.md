# Distributed machinery security audit — 2026-10-24

## Assessment

**Not injection-proof.** This review found and corrected exploitable authorization boundaries. It does not certify that an AI model will ignore every malicious instruction. Repository content, test output, screenshots, filenames, machine metadata, MCP results, and conversation history remain untrusted input. Their contents must never confer permission to run commands, disclose credentials, change approvals, or deploy.

Scope: launcher HTTP/WebRTC RPC and enrollment boundaries, controller job dispatch and update path, distributed master REST/MCP, dashboard controls and rendering, Codex history/IPC bridge, source/archive handling, and live local deployment. This is a source review with focused adversarial tests and operational verification, not exhaustive fuzzing or a review of every application dependency, public signaling implementation, or historical commit.

## Findings and corrections

| Severity | Finding | Correction / status |
| --- | --- | --- |
| Critical | A shared fleet AES key authorized arbitrary GitHub runner selection and uploaded P2P code. Any enrolled peer could impersonate the controller. | Privileged RPC operations now require an Ed25519 controller signature binding sender, target, operation, arguments, nonce, and time. Only explicit observation/submission operations are exempt; new operations fail closed. Public keys are pinned through authenticated SSH updates. The private signing key stays outside launcher mounts. Offline/unupdated agents remain a rollout gap. |
| Critical | The master API token also authorized instructions to existing Codex threads, and was copied into the coordinator launcher. | Separate master, launcher, and Codex owner tokens. Launcher scope permits only status and queued test requests. Codex routes require the owner token and a direct loopback connection; the LAN proxy blocks them. Coordinator launcher credential replaced with scoped token. |
| High | Dashboard test start, stop, rerun, and mobile-start trusted an easily forged Origin header without authentication. Starts can commit and push source. | These mutations now require the master bearer token plus Host/Origin checks. Browser controls refuse credentials over plaintext LAN HTTP. Remote job submission remains available through the local encrypted launcher. |
| High | Codex credentials and instructions could traverse plaintext LAN HTTP. | Codex controls are localhost/SSH only, with a separate token held in browser memory. LAN input is disabled. REST/MCP documentation now uses loopback/SSH. General REST/MCP bearer use over plain LAN HTTP remains technically possible; use the documented encrypted path. |
| High, residual | Workers run controller-approved code with Docker daemon access; the coordinator launcher also has Docker, writable checkout, and read-only SSH key mounts. | **Not isolated from host compromise.** A malicious approved commit or compromised launcher can use Docker to read host credentials, including keys outside direct mounts. Signature and token changes do not protect against this. Run untrusted code in disposable, independently isolated workers without coordinator/AI credentials or host Docker access. |
| High, residual | An AI client can follow malicious instructions in tool results and invoke its own powerful tools. | MCP exposes fixed schema-checked operations, not arbitrary shell or Codex commands. Initialization explicitly marks machine output untrusted. No automatic log-to-Codex command path was found. This does not constrain the AI client's independent shell/network permissions. Use least privilege and explicit authorization for consequential actions. |
| Medium, residual | Fleet identity and observations still use a shared symmetric key. | Controller signatures restrict execution, but enrolled peers can still forge observations, impersonate peers for exempt operations, and request tests. Per-device authentication and revocation are the next boundary improvement. Requests can also consume resources and publish the controller's current checkout. |
| Medium, residual | Replay cache is process-local and observations are not independently attested. | Freshness, target, nonce, and replay checks remain in place. Restart loses the nonce cache; a captured valid signed request may be replayable within its short freshness window. Do not treat worker results as independently trustworthy attestations. |

## Boundaries checked

- Source runner execution uses argument lists, but executing fetched Python is still code execution; absence of `shell=True` is not a sandbox.
- Archive extraction rejects traversal, absolute paths, links, duplicate entries, nonregular files, and excessive sizes/counts.
- Codex thread lookup uses SQL parameters, validates identifiers, bounds history reads, and confines history paths to the Codex directory. IPC ownership checks and explicit owner-directed operations remain enforced.
- Reviewed dashboard logs and Codex transcript render dynamic text with DOM text APIs. Static HTML templates are not evidence of dynamic HTML injection.
- WebRTC transport bounds assemblies/concurrency, checks enrollment, and uses the same encrypted RPC envelope. Public signaling enrollment alone does not grant the controller signing key.
- MCP operation names and argument schemas are server-owned. A hostile log string remains a result field; it does not become another dispatched server operation.
- No real malicious instructions were sent to an active AI thread during verification.

## Verification

Local monitor and workspace security tests cover token scope separation, forwarded Codex rejection, forged Origin/Host, unsigned and altered privileged operations, unknown-operation denial, malicious MCP log data, AES wrong-key/target/freshness/replay, and bounded WebRTC transport.

Live HTTP checks: launcher status 200; launcher run administration 401; master token on Codex 401; separate owner token on direct-loopback Codex reads 200; LAN Codex route 403; unauthenticated test start 401. Localhost dashboard serves successfully.

Distributed verification request: `d6183839-f61e-4d91-84b8-000ab02d38ac`, suites monitor/workspace/map, forced execution. Run `20261024T215802Z-c038966f` **passed: 47/47 selected tasks** on the available workers, pinned to root commit `27fc356`. Latitude was unreachable; Wolf did not finish its launcher update and was excluded. This is not full-fleet verification.

Local checks: **131 monitor tests and 9 swarm/security tests passed**. Browser verification rendered hostile transcript HTML as literal text, submitted no AI commands, and confirmed the LAN Codex credential input was disabled. A live, correctly AES-encrypted but unsigned worker execution request received **403**. Four available workers advertised the controller-signature capability. The monitor tests emitted a pre-existing runtime-statistics fixture error (`KeyError: host`) without test failures.

## Operating the controls

On the coordinator, open `http://127.0.0.1:8089/tests.html`. From another computer use `ssh -L 8089:127.0.0.1:8089 julian@compai.local`, then open the same local URL. Use the Codex owner token for AI controls and the master token for test administration. Do not paste credentials into chat or logs. Ordinary enrolled devices can submit tests through `http://127.0.0.1:8791/launcher` without receiving either owner token.

The controller signing key is `~/.config/map-master/controller-signing.key` (raw 32-byte Ed25519 key, mode 0600). Back it up privately; loss requires replacing pinned public keys through authenticated SSH. Never include it in source snapshots or worker configuration. Public-key pinning is part of launcher update staging.

## AI-specific guidance

The OpenAI [agent safety guidance](https://developers.openai.com/api/docs/guides/agent-builder-safety) describes prompt injection as a risk requiring layered controls, including untrusted-data isolation, structured extraction, and limited tool authority. Labels or a system prompt alone are not an enforceable security boundary. This audit's regression tests verify deterministic service behavior, not model resistance to every adversarial prompt.

## Follow-up: owner-requested phone access

Later on October 24, the owner requested access from cellular and other Wi-Fi
networks. `https://compai.lifetech.fyi/` now publishes a separate narrow Codex
service through an outbound HTTPS tunnel. This is an intentional addition to the
original localhost-only arrangement; the LAN Codex route remains blocked.
The service on loopback port 8091 requires the separate Codex owner bearer,
restricts Host/Origin and static assets, caps command bodies, and exposes only
Codex history/command routes. It does not publish the master, shell, or Docker
APIs. The tunnel credential and owner credential are separate private files.
Cloudflare is part of the transport trust boundary; the origin application can
also sit behind another HTTPS reverse proxy.

The attached Pixel 3 successfully authenticated through the public HTTPS URL,
sent a harmless instruction, and displayed the Codex reply. The test performed no
shell/file actions. Phones can pair using an owner-issued, one-use invitation (ten-minute expiry).
The URL fragment is removed before exchange; only hashes are stored on CompAI.
The browser receives a revocable 30-day Secure/HttpOnly/SameSite=Strict session
cookie, so it can reload without retaining the owner token. Lock revokes the
current browser; the local owner can revoke all devices. Tests cover expiration,
concurrent single-use redemption, replay, origin checks, cookie flags, and logout.
This is owner-level access: pairing links must only be given to the owner’s own
devices. The attached phone verified pairing and authentication across reload.
Unauthenticated public history returns 401; other master APIs return 404; LAN
Codex still returns 403. Local validation passed 135 monitor tests, including
four new remote authorization and command-target regression tests.

The owner launcher now offers QR issuance through a separate credential-checked
loopback broker on port 8092. This is an owner-level capability, provisioned only
to the coordinator. The launcher requires a tab capability obtained from a
private one-use OS-owner launch (`run.py --swarm`) before invoking the broker.
Loopback, Host/Origin headers, and fleet membership alone cannot authorize this
action. Browser capabilities stay in tab memory, expire in 30 minutes, and are
invalidated by launcher restart. The public tunnel does not route to the broker.
