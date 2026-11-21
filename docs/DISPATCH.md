# Arc Dispatch — persistent operations journal and maintenance notices

**Status: design; not implemented or deployed.** This document records the
dispatch requirement for Arc and LOCI. The existing Arc chat UI, MCP endpoint,
health API, and workstation timers do not yet provide this capability.

## Purpose

Dispatch is the shared, durable place to announce planned outages, maintenance,
incidents, handoffs, and recovery. It must remain usable when the workstation
is powered off. A phone or another computer can create and read notices through
a small authenticated web UI hosted independently of the workstation. Arc and
connected IDE agents can read and acknowledge the same entries through MCP and
an API.

## Topology

```text
Phone/browser ───────┐
Antigravity/MCP ─────┼── VPS Dispatch API + append-only event journal
Claude/Copilot MCP ──┤                 │
Arc/workstation ─────┘                 └── notification adapters
        │
        └── durable local outbox; retries after network returns
```

The VPS journal is authoritative. The workstation keeps an encrypted local
outbox and cache so it can queue a dispatch while offline and synchronize it
when connectivity returns. A successful create must return a server event ID
and timestamp before the UI says “saved.” If the server cannot be reached, the
UI must say “queued on this device” and must not imply other systems have seen
the notice.

## Event record

Use an append-only event stream, with edits represented by new events rather
than destructive updates. Each event has:

- `event_id` (UUID), `schema`, `created_at` (UTC), `actor`, and `source`
- `kind`: `maintenance`, `incident`, `handoff`, `status`, `acknowledgement`,
  `recovery`, or `correction`
- `title`, `summary`, affected `systems`, severity, and optional start/end time
- `correlation_id` linking updates to the same outage or maintenance window
- `supersedes` for corrections, and an optional list of acknowledgement states
- optional attachment references by content hash; no raw evidence or secrets

Keep message text factual and short. Do not put passwords, tokens, private
keys, camera/audio media, or personal profiling data in dispatch events.

## User flow: add a 500 GB NVMe

1. Open Dispatch from a phone or another computer and choose **New maintenance
   notice**.
2. Enter “Workstation power-down to install 500 GB NVMe”, affected services,
   expected downtime, and an optional note. Choose which integrations should
   receive a notification.
3. Save and wait for the VPS event ID and **Saved to shared journal** status.
4. Dispatch publishes a maintenance window. Arc and IDE integrations show it
   as pending/acknowledged; local services can quiesce or mark themselves
   unavailable. The workstation may then be shut down safely.
5. During downtime the VPS UI remains available and displays “workstation
   offline; maintenance in progress.”
6. After power-up, a heartbeat posts service health and the operator closes the
   window with a recovery note. The journal retains the whole history.

The first release should not automatically power off, restart, or modify
hardware. It can provide a checklist and explicit status transitions only.

## Integrations

- **Web UI:** responsive authenticated page, usable on mobile; timeline,
  compose form, affected-service badges, acknowledgements, and outage banner.
- **Arc:** read/list/create/acknowledge tools exposed over the existing MCP
  endpoint, plus a read-only timeline view in the Arc interface.
- **Antigravity, Claude, Copilot, ChatGPT:** configure each supported client to
  call the Dispatch MCP tools or poll its feed. An agent can acknowledge only
  when its MCP connection is active.
- **Notifications:** email or another explicitly configured adapter, with
  delivery result recorded as a separate event. Notifications are a convenience;
  the journal remains the source of truth.
- **Workstation monitor:** publish service heartbeat and recovery status; never
  infer that an agent has read a notice from mere delivery to a webhook.

MCP alone cannot force a notice into another product's open chat window. Some
clients may only see Dispatch when a session asks the MCP server for it. For
those clients, use notifications and make the current outage summary available
as a startup context resource.

## Security and durability requirements

- Put the web UI and API on the VPS behind TLS and strong user authentication
  (OIDC/passkey); keep the journal private to authorized users and service
  identities.
- Issue distinct revocable credentials to Arc and each agent integration; use
  read-only scopes by default. Protect create/acknowledge operations with
  separate scopes and audit the actor.
- Validate event schemas and size limits. Escape rendered text. Rate-limit
  writes and reject arbitrary attachment URLs.
- Use a transactional database with append-only events, encrypted backups, and
  documented restore checks. Publish stable event IDs and content hashes.
- Keep only service availability, maintenance, and user-authored messages in
  this channel. Audio/video evidence belongs in the evidence journal, referenced
  by hash only when needed.
- Record delivery attempts and acknowledgements separately; expose failed
  notifications and stale heartbeats in the GUI.

## Acceptance checks before calling it ready

1. Create a maintenance notice from a phone; read it back after closing and
   reopening the page.
2. Confirm an authorized Arc MCP client can list and acknowledge it, while an
   unauthenticated client cannot read or mutate the journal.
3. Stop the workstation and verify the notice and outage banner remain
   available from another device.
4. Restore the workstation and verify a new heartbeat and recovery event appear
   without deleting the original notice.
5. Disconnect the client network and confirm the UI distinguishes a local
   queued message from a server-confirmed event.
6. Restore from an encrypted backup and verify event IDs, ordering, and hashes.

Until these checks pass and the service is deployed, the supported procedure is
to post a maintenance notice to an independently reachable existing channel
before shutdown and explicitly confirm it was saved. There is no Dispatch GUI
in the current Arc build yet.
