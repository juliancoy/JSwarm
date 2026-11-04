# Message delivery checks

Run from the Organization workspace:

```
PYTHONPATH=swarm-control/server python3 -m pytest swarm-control/server/tests/test_api.py swarm-control/server/tests/test_message_queue.py -q
node swarm-control/test-pidp-browser.mjs
```

The focused API/queue run currently passes 42 tests, including 21 session-matrix
subtests. The matrix exercises queue submission, Send now and direct commands for:
signed out, expired session, revoked session, another linked sign-in identity,
legacy pairing, valid sign-in and renewed sign-in. Rejected requests never reach
the queue or execution adapter.

Queue checks model machine unavailability and an active turn, preserving waiting
messages until the machine is connected and the turn is idle. They check exactly
one delivery after readiness, explicit steering, cancellation/dispatch races and
ambiguous delivery without automatic retry. This models the confirmation wait;
it does not detect or click an actual native machine/tool confirmation dialog.

The isolated HTTPS browser fixture runs at desktop and phone widths. It uses real
HTTP controller routes, session records and the persistent queue with synthetic
accounts and an execution adapter that never invokes a real machine. It checks
queueing while disconnected, readiness transitions, confirmed delivery removal,
Send now, expiry rejection, draft-preserving sign-in in a new tab, independent
browser sessions, logout and Back/Forward. No production conversation is sent.

PIdP code/token/identity security tests run separately in its checkout. This
fixture substitutes the identity provider and native IPC; it does not establish
that every production OAuth-provider handoff or machine-confirmation UI works.
The observed upstream PIdP GET/POST consent-session mismatch is a separate known
issue, not resolved by these controller changes.

## Full-access delivery

New conversations created by Swarm Control explicitly request full access and
`approvalPolicy: never`. Sends to saved full-access conversations explicitly
retain the no-approval policy, while inheriting model and other thread settings.
Restricted, missing, malformed or unknown sandbox settings do not receive an
approval override. No sandbox is widened when sending to an existing thread.
Steering retains the running turn's permissions; this does not dismiss an
already-open approval or remove the requirement for a connected VS Code owner.
Focused permission tests use a disposable SQLite database and fake IPC owner.
