# Arc Dispatch — persistent operations journal and maintenance notices

**Status: interim GitHub Issues board available; custom service not
implemented.** The private [LOCI Dispatch repository](https://github.com/arko-sanyal-loci/loci-dispatch)
provides a mobile-capable issue form and durable handoff comments now. This
document records the target VPS/API and Arc/MCP implementation. The existing
Arc chat UI, MCP endpoint, health API, and workstation timers do not yet expose
Dispatch directly.

## Purpose

Dispatch is the shared, durable place to announce planned outages, maintenance,
incidents, handoffs, and recovery. It must remain usable when the workstation
is powered off. A phone or another computer can create and read notices through
a small authenticated web UI hosted independently of the workstation. Arc and
connected IDE agents can read and acknowledge the same entries through MCP and
an API.

### Interim procedure

Until the target service below is deployed, use the private GitHub Issues page
as the GUI. Create a **System dispatch** issue with the reason, affected
systems, UTC start and expected return, and a recovery plan. Confirm it appears
in the Issues list before shutdown. Record acknowledgements and recovery as
comments; close the issue after service health is checked. Repo access is
required; the issue board cannot push notices into clients that are not
connected to GitHub.

## Target topology

```text
Phone/browser ───────┐
Antigravity/MCP ─────┼── VPS Dispatch API + append-only event journal
Claude/Copilot MCP ──┤                 │
Arc/workstation ─────┘                 └── notification adapters
        │
        └── durable local outbox; retries after network returns
```

In the target implementation, the VPS journal is authoritative. The workstation keeps an encrypted local
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

### Shared workstation pointer

Arc sometimes needs to move the visible pointer to show a screen detail. The
current GNOME session has one pointer, so it cannot provide a second independent
cursor. Arc may take a short, visible control lease automatically after a
configurable period of workstation idleness, so the user does not have to stop
and approve routine work. If the user is active, Arc must ask before taking
control. Idle detection should use only the desktop's idle state; it must not
record typed text or the contents of pointer activity.

- Show a desktop notification before control begins: “Arc is working on the
  workstation UI.” Include the task and a **Give me control** action.
- While the lease is active, mark Arc's pointer with a distinct halo/color and
  show a persistent desktop status with a countdown and **Stop Arc control**.
- Any human mouse or keyboard input immediately cancels the lease and returns
  control to the user. Arc may ask for a new lease later; it must not steal
  control back automatically.
- End the lease on task completion, timeout, screen lock, or session change, and
  report the result through the desktop notification and Dispatch journal.
- Prefer app APIs, keyboard navigation, or command-line operations when they
  can complete the task. Pointer control remains available for visual work that
  benefits from showing the user exactly what Arc is inspecting.

The local desktop notification is the first supported alert. A phone or laptop
alert requires a configured notification adapter; delivery must be recorded as
pending, delivered, or failed, and must never be represented as a guaranteed
message to another device without acknowledgement.

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
7. While Arc holds a UI-control lease, move the human mouse and confirm that
   Arc immediately yields control and the desktop records the handoff.
8. Leave the workstation idle past its configured threshold and confirm Arc can
   start a lease after showing the notification, without a user click.

Until these checks pass and the service is deployed, the supported procedure is
to use the private GitHub Issues board described above and explicitly confirm
the issue was saved. A custom Dispatch GUI/API and direct Arc MCP integration are
not in the current Arc build yet.
