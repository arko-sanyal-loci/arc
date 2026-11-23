# Arc experience status

Updated: 2026-11-23

## Workstation UI

Arc has a functional browser UI served by the FastAPI runtime on port `7860`.
The current experience includes:

- Billo Tech branding in the sidebar and entrance scene.
- An animated Arc entrance overlay with orbital motion and a reduced-motion
  fallback.
- An optional local WebAudio entrance chime, enabled only after the user
  presses **Enter Arc**.
- A notification tray and transient toast notifications.
- Provider-lane notifications when the active routing preference changes.
- Live RTX fast-lane and Arc deep-lane status indicators.

The UI remains intentionally self-contained in `app/static/index.html` while
the experience is being shaped. A later extraction into separate CSS and
JavaScript modules should preserve the current DOM IDs and accessibility
labels because the backend and smoke checks use them.

## Workstation setup

`scripts/03_workspace_setup.sh` now installs
`requirements-workstation.lock.txt` when present. This keeps the setup script
aligned with the tested workstation environment and avoids the optional
`posix-ipc` build, which requires system Python headers and is not used by the
Arc workstation runtime.

## VPS continuity

The VPS chat now:

- Normalizes a client-provided `session_id` before using it in encrypted
  journal records.
- Recovers recent encrypted turns when `ARC_CHAT_AGE_IDENTITY` points to an
  owner-controlled age identity file and the `age` executable is available.
- Keeps decryption opt-in; without the identity, existing chat and telemetry
  behavior remains available without exposing prior turns.
- Returns the session identifier and whether persistent recall was available
  in the authenticated chat response.

The systemd unit now rate-limits restart loops and waits ten seconds between
restart attempts.

### VPS deployment and backup procedure

Before deploying the VPS files:

1. Make a dated copy of `/opt/arc-vps-chat` outside the release directory.
2. Verify `/etc/arc-chat/server.env` remains root-owned and mode `0600`.
3. Install the application files and service unit.
4. Restart `arc-vps-chat.service`.
5. Verify the protected chat login, authenticated chat response, and status
   endpoint.
6. Keep the dated application backup until the authenticated checks pass.

This commit records the code and deployment procedure; it does not claim that
the live VPS has been updated.

## Verification

The local Arc preview was checked for:

- The status endpoint reporting LOCI/Glassbox memory and session continuity.
- The visible `Arc activity`, `Billo Tech`, and `Enter Arc` markers.
- JavaScript syntax with `node --check`.
- Arc's Python test suite and shell syntax checks.
