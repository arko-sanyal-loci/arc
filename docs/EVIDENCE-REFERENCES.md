# Private evidence references

Arc stores metadata-only pointers to evidence that remains encrypted on the
workstation/VPS. It never receives original recordings through this endpoint.

`POST /api/evidence/reference` is intended for a loopback caller. Uvicorn must
bind to `127.0.0.1`; the route also checks the remote client address and a
bearer token from the `ARC_EVIDENCE_TOKEN` environment variable. It validates
UUIDs, SHA-256 values, proxy paths, and video-only proxy metadata before adding
an idempotent `EVIDENCE_REFERENCE` node to `data/arc.db`. Do not expose this
API through a public reverse proxy.

The Arc UI workbench's browser A/V preview is separate. It requires a visible
user action, stays in the browser, and does not send a stream to chat, memory,
or a profile model.

## Local workstation launch

The persistent `arc-interface.service` uses a locked dependency set and runs
Uvicorn on `127.0.0.1:7860`. The `arc-monitor.service` is separate and provides
the opt-in A/V monitor at `127.0.0.1:11500`. User-service linger keeps both
running across desktop logout. Inspect service state with:

```bash
systemctl --user status arc-interface.service arc-monitor.service
journalctl --user -u arc-interface.service -n 50 --no-pager
```

The Arc SQLite database is private runtime state and is excluded from Git.
