# Arc VPS continuity chat

This small service keeps a private Arc chat and last-known workstation status
available at `https://billo.tech/arko/chat` when the workstation is offline.
It binds to `127.0.0.1` and is served through the HTTPS Caddy vhost. The chat
uses the VPS-local Ollama model; it does not forward prompts to Nexos or Grok.

## Security and data flow

- Human access uses an individual username and a high-entropy password checked
  with scrypt. Caddy supplies HTTPS; the application binds only to loopback.
- Workstation telemetry uses a separate bearer token and strict, minimized
  fields. Sequence numbers reject older updates. Stale status becomes Offline
  after 75 seconds and keeps the last report visible.
- User/assistant chat turns are age-encrypted to Arc's existing public
  recipient before insertion in an append-only SQLite journal. The VPS has no
  age identity, so it cannot decrypt prior turns. The browser keeps the active
  conversation locally for continuity.
- Maintenance notices and sanitized health changes are plaintext inside the
  protected VPS journal so the owner can review them. They are append-only and
  hash-chained. Other agents cannot read or write this journal until their
  separate identities and access scopes are provisioned. Do not put passwords,
  API keys, or private credentials in a notice.
- Voice input is push-to-record, capped to an 8-second browser clip, sent over
  HTTPS, transcribed locally on the VPS CPU, and discarded. The transcript is
  shown for review before the user sends it to chat. Replies can be spoken by
  the browser's local speech synthesis.
- The service has no shell, browser, repository, or device-control tools. It
  can report only the latest telemetry and dispatch records received by the
  VPS.

## Deployment files

`server.py` is standard-library HTTP plus local Ollama, SQLite and the `age`
CLI. The service unit runs as the dedicated `arc-chat` Unix account. Secrets
belong in `/etc/arc-chat/server.env` (root-owned, mode `0600`), never in this
repository. Keep the Caddy route for this service inside the existing
`billo.tech` site block.

`requirements.txt` installs CPU-only voice transcription dependencies. If
those dependencies are unavailable, text chat and telemetry remain available
and voice transcription reports unavailable rather than sending audio to a
cloud service.

## Billo Tech public pages

`landing/index.html` and `landing/status.html` are the source for the static
Billo Tech home page and `/status` page in `/var/www/billo.tech/landing` on the
VPS. They link to the separately protected Arc chat at `/arko/chat`; the public
pages do not reveal the authenticated heartbeat or journal state. The VPS chat
deployment is independent of the workstation. Save a dated copy of the landing
directory before changing it, then verify `/`, `/status`, the protected chat
login, and the authenticated status endpoint after deployment.

## Event record

The journal contains monotonically increasing SQLite sequence numbers, unique
event IDs, previous-record hashes, and SHA-256 record hashes. Chat payloads are
encrypted before storage; dispatch and sanitized service-state payloads remain
readable only through the authenticated API. Startup and status reads verify
the chain. SQLite triggers reject updates and deletes.

The initial deployment authorizes the owner web login and one workstation
telemetry key. Separate Claude, Gemini, Copilot, and Codex identities still
need individual credentials and client configuration before those systems can
read or publish dispatch events.
