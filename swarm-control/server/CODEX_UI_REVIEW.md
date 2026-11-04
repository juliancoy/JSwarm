# Swarm Codex remote-control UI

The workspace uses a restrained hierarchy: application identity and connection
status, conversation selection, thread context, messages, then the next instruction.
The desktop sidebar supports searching the current page of conversations. On a
phone, selecting a conversation opens the reading/composing view; Conversations
returns to the list without discarding the draft. Search is explicitly page-scoped.

Polling retains unchanged message DOM and the reader's scroll position, keeps a
manually selected delivery mode while activity is unchanged, and updates owner
connection changes. Draft-switch confirmation and command request IDs remain in
place. The browser keeps using the existing owner/device authentication flow.

## Review and checks

Run the synthetic browser review from the Organization root:

```sh
CHROME_PATH=/usr/bin/google-chrome node swarm-control/server/scripts/review-codex-ui.mjs artifacts/codex-ui
PYTHONPATH=swarm-control/server python3 -m unittest discover -s swarm-control/server/tests -p 'test_api.py' -q
```

The browser review serves the actual Codex JS/CSS with synthetic thread data on
loopback. Its command endpoint is a local in-memory stub: no real instruction,
owner token, private conversation or production mutation is used. It checks
connection, search and empty search, thread selection, draft/delivery retention,
unchanged message DOM, single-send behavior, disconnected owner state, lock,
horizontal overflow and script errors at 390 and 1440 pixels. A twenty-thread
fixture also checks that pagination stays inside the workspace. PNGs cover locked,
list and conversation states. The fixture isolates the Codex panel and does not
claim to review the separate LifeTech deployment panel or real paired-device flow.

The distributed visual suite invokes this review through
`OrgPortal/web/scripts/render-layout-fixtures.mjs`; its six PNGs and JSON report
are collected with the existing visual artifacts. Review images manually: passing
checks do not establish aesthetic quality or full accessibility conformance.

Design basis: [IBM typography](https://www.ibm.com/design/language/typography/type-basics/)
for restrained emphasis and alignment, and [GOV.UK action guidance](https://design-system.service.gov.uk/components/button/)
for explicit labels and hierarchy. They inform the interaction design; the app
retains its existing dark, teal-accented identity.

## Remote LAN status

The header's Swarm status link opens `/swarm-status` on the same remote origin.
`/api/v1/swarm/status` accepts authenticated GETs using the existing
paired-browser session. It projects devices, observed ping responses, interface
traffic/capacity and worker-test status from two fixed loopback coordinator GETs.
No general LAN proxy, scan trigger, command endpoint or coordinator logs/session
metadata are exposed. Unauthenticated reads are denied and POST is not accepted.
The page clears observations when authentication expires and labels source times.

This telemetry uses the existing authenticated HTTPS transport, not the APK's
WebRTC data channel. The latter remains an APK-transfer protocol. A future P2P
telemetry option would need an explicitly authorized, read-only protocol and
failure/reconnection handling; no claim of WebRTC telemetry is made here.

### Machine cards

LAN status starts with one combined card for each observed device/worker identity.
Worker addresses match network addresses first; a unique mDNS hostname alias is
used when an address is unavailable. Ambiguous names are not silently merged.
Worker-only machines remain visible even if the network scan did not observe them.

Filled cards use the existing distributed-test palette. A failed task takes
precedence, followed by running, queued, stopped, unavailable and passed states;
counts retain mixed task states. Queued and stopped share the existing neutral
color, with explicit labels. Ended runs cannot display queued/running work as
still active. An unused worker or missing ping response does not imply passed,
stopped or failed tasks. The overview labels recorded runs.

Card links open a hash-addressed machine detail view with reachability, available
CPU/memory and task states. Native Back and All machines preserve the overview;
refresh retains the selected machine. Keyboard activation moves focus to the
machine heading. Network interface details remain available under Network overview.

Screenshot attachments use the composer’s file picker, clipboard paste, or drop.
The browser strips image metadata by re-encoding to PNG/WebP, preserves alpha,
limits images to four and 4 MB total, and shows removable previews. Send and Steer
carry bounded inline image inputs through the same authenticated command route.
Images are not saved to a separate upload store. Draft images survive failed sends;
an uncertain retry preserves the command request ID. Lock and confirmed thread
changes clear image drafts. The headless review checks these controls and alpha at
phone/desktop sizes; Python tests check image validation and IPC input serialization.
