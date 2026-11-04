# Voice messages and durable delivery

Open https://compai.lifetech.fyi on a paired browser, select a conversation, and
press **Record**. Allow microphone permission. Recording is limited to three
minutes and eight MB. **Stop · keep draft** transcribes into the editor without
submitting. **Finish & send** explicitly transcribes and submits to the selected
delivery mode. Failed transcription retains the recording in browser memory for
retry; retry always returns to a draft. Leaving or locking the page releases the
microphone. Text drafts survive reload in this browser tab via sessionStorage,
are separate per thread, and are cleared on Lock. Audio is never written to browser
storage. Closing the tab removes these drafts.

The default delivery mode queues messages until their thread is idle and connected
to VS Code. Steer is an explicit alternative for the currently running turn.
Queued messages remain in `SWARM_STATE_DIR/messages/queue.sqlite`, mode 0600.
They execute FIFO within each thread; a busy thread does not prevent another idle
thread from receiving its message. Cancel applies only before dispatch. Timeouts
or process interruption during dispatch become **uncertain**, block subsequent
messages in that thread, and never retry automatically. Check the conversation,
then mark the uncertain item reviewed. A reviewed message is not resent.

Request IDs are idempotent and bound to the message payload. Completed records
expire after seven days on subsequent submission. Up to 100 queued/delivering/
uncertain messages are allowed. Images are removed from queue payloads after
confirmed delivery, cancellation or review; text and receipts remain for history.
No message body is written to service logs. The queue worker holds an exclusive
process lock and starts with the authenticated API service after a restart.

## Service

`run.py deploy` brings up Whisper through `server/docker_services.py`, resolving the pinned image
through docker_utils (local, then LAN, then registry). The image is upstream
[whisper.cpp](https://github.com/ggml-org/whisper.cpp/tree/master/examples/server),
pinned by digest, with its bundled **base.en English model**, four CPU threads,
2 GB memory limit, read-only root filesystem and temporary audio conversion in
RAM. It publishes only `127.0.0.1:8094`; browsers use the authenticated same-origin
portal proxy. Audio uploads go to CompAI, not a third-party transcription service.
The container restarts after Docker restarts; transcription logs are disabled.

The service transcribes when recording ends, rather than showing partial words
while recording. Hardware microphone testing on a physical phone is separate
from the automated Chrome MediaRecorder checks.

## HTTP API

These routes use existing paired-browser authentication and exact same-origin
checks on the remote portal. The direct loopback master endpoint supports the
same routes with the separate Codex owner bearer token; LAN proxy access remains
blocked. Existing VS Code thread settings and approval requirements are inherited.

- `POST /api/v1/master/codex/transcribe`: raw audio body, Content-Type audio/webm,
  audio/ogg, audio/mp4 or audio/wav. Returns `{ "text": "..." }`. Never sends a message.
- `POST /api/v1/master/codex/queue`: JSON `thread_id`, `request_id` UUID, `text`,
  optional existing screenshot `images` list. Returns a durable acceptance receipt.
- `GET /api/v1/master/codex/queue?thread_id=...`: up to 100 latest messages.
- `POST /api/v1/master/codex/queue/cancel`: `{ "id": "request UUID" }`. Cancels a
  queued message or marks an uncertain outcome reviewed.

## Verification

Python tests cover auth/origin/upload boundaries, queue persistence, per-thread
ordering, idempotency, cancellation, uncertain delivery, and the fixed Whisper
proxy. `scripts/review-voice-ui.mjs` uses synthetic conversations and Chrome's real
MediaRecorder with a fake microphone to test stop/draft, finish/send, reload,
retry, permissions, lock cleanup, and phone/desktop layouts. It never submits
instructions to a real agent. A known public WAV and WebM speech clip verify the
running Whisper engine separately.
