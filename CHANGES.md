# Arc secure tooling — change notes

## 1. Loopback / Origin / Host guard (closes the CORS `*` hole)

**The hook is one line.** In `app/server.py` the wildcard
`CORSMiddleware(allow_origins=["*"], …)` is replaced by:

```python
app.add_middleware(LocalOriginGuard)
```

It sits at the app's existing middleware seam rather than inside the `/mcp`
if-chain, so one layer covers `/mcp`, `/api/*`, `/v1/messages` and the
`/ws/chat` WebSocket. CORS never applies to WebSockets, and the in-handler
`client.host` loopback checks can't tell a browser tab from a CLI, so a
per-route check would leave holes.

Policy (`core/http_guard.py`):

| Request | Result |
|---|---|
| `Host` not loopback (`127.0.0.1`, `localhost`, `[::1]`) | 403 (blocks DNS rebinding) |
| `Origin` present and one of Arc's own loopback origins | allowed (the Arc web UI, arc-desktop) |
| `Origin` present and anything else (including `null`) | 403 |
| No `Origin`, `GET`/`HEAD` on `/`, `/api/status`, `/api/health`, `/static/*` | allowed (status probes) |
| No `Origin`, anything else | must send the local tooling token |

No `Access-Control-Allow-*` header is ever sent.

Tooling token: `$ARC_CONFIG_DIR/tooling-token` (default
`~/.config/arc/tooling-token`, mode 0600, created on first use) or
`ARC_TOOLING_TOKEN`. Send it as `Authorization: Bearer <token>` or
`X-Arc-Tooling-Token: <token>`.

Extra hosts and origins (for example a reverse proxy) can be added through
`ARC_ALLOWED_HOSTS` and `ARC_ALLOWED_ORIGINS`, comma-separated.

### Clients that must add the token before `arc-interface` is restarted

These POST without an `Origin`, so they get 403 until they send the token:

- `loci-workstation/evidence_worker.py` → `POST /api/evidence/reference`
- `mr-calendar/playground/arc-monitor/live_media.py` → `POST /api/streams/index`
- Antigravity MCP (`config/antigravity.json` → `http://localhost:7860/mcp`): add the header
- any other local MCP bridge

`arc_telemetry_push.py` (`GET /api/status`) keeps working without changes.

The running server only picks this up after `systemctl --user restart arc-interface`.

### Tests

`tests/test_http_guard.py` covers: loopback same-origin allowed, foreign
Origin blocked, non-loopback/missing Host blocked, missing Origin blocked
without a token, token accepted, public reads allowed, no CORS on preflight,
and the WebSocket handshake allowed and blocked.
