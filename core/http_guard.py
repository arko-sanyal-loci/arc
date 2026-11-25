"""Loopback request guard for Arc's local HTTP and WebSocket surface.

Arc listens on 127.0.0.1, but a loopback bind alone does not stop browser
attacks against a local tool server:

* cross-site requests: any web page open in a local browser could POST to
  ``http://127.0.0.1:7860/mcp``; the old ``allow_origins=["*"]`` let it read
  every reply;
* DNS rebinding: an attacker domain re-resolved to 127.0.0.1 reaches the same
  server with a foreign ``Host`` header;
* cross-site WebSocket hijacking: CORS never applies to WebSockets.

Policy (one seam for /mcp, /api/*, /v1/*, /ws/*):

1. ``Host`` must be a loopback name (or one listed in ``ARC_ALLOWED_HOSTS``).
2. If ``Origin`` is present it must be one of Arc's own loopback origins
   (or listed in ``ARC_ALLOWED_ORIGINS``).
3. If ``Origin`` is absent (a non-browser client), only safe reads of the
   public status/UI paths pass without credentials; everything else must
   present the local tooling token (``Authorization: Bearer`` or
   ``X-Arc-Tooling-Token``; file ``$ARC_CONFIG_DIR/tooling-token``, 0600).

No ``Access-Control-Allow-*`` headers are ever emitted, so browsers refuse
cross-origin reads by default.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Iterable

from core.tooling_state import token_ok

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
SAFE_METHODS = {"GET", "HEAD"}
# Read-only paths a credential-less, Origin-less client may fetch
# (status probes from arc_telemetry_push, health checks, the UI itself).
PUBLIC_READ_PREFIXES = ("/api/status", "/api/health", "/static/", "/favicon")
PUBLIC_READ_EXACT = {"/", "/tooling", "/consent"}


def _split_host(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def allowed_origins(port: int | None = None) -> set[str]:
    port = port or int(os.environ.get("ARC_PORT", "7860"))
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
    extra = os.environ.get("ARC_ALLOWED_ORIGINS", "")
    origins.update(o.strip().rstrip("/") for o in extra.split(",") if o.strip())
    return origins


def host_allowed(host_header: str | None, extra_hosts: Iterable[str] = ()) -> bool:
    if not host_header:
        return False
    host = _split_host(host_header)
    return host in LOOPBACK_HOSTS or host in {h.lower() for h in extra_hosts}


def decide(method: str, path: str, headers: dict, origins: set[str],
           extra_hosts: Iterable[str] = (), token_check: Callable[[dict], bool] = token_ok) -> str | None:
    """Return None to allow, or a short reason string to reject."""
    if not host_allowed(headers.get("host"), extra_hosts):
        return "host not allowed"
    origin = headers.get("origin")
    if origin is not None:
        return None if origin.rstrip("/") in origins else "origin not allowed"
    if method in SAFE_METHODS and (path in PUBLIC_READ_EXACT or path.startswith(PUBLIC_READ_PREFIXES)):
        return None
    if token_check(headers):
        return None
    return "origin required (or local tooling token)"


class LocalOriginGuard:
    """Pure ASGI middleware covering HTTP and WebSocket scopes."""

    def __init__(self, app, port: int | None = None, extra_hosts: Iterable[str] = (),
                 token_check: Callable[[dict], bool] = token_ok):
        self.app = app
        self.origins = allowed_origins(port)
        env_hosts = os.environ.get("ARC_ALLOWED_HOSTS", "")
        self.extra_hosts = set(extra_hosts) | {h.strip() for h in env_hosts.split(",") if h.strip()}
        self.token_check = token_check

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope.get("method", "GET") if scope["type"] == "http" else "WEBSOCKET"
        reason = decide(method, scope.get("path", "/"), headers, self.origins, self.extra_hosts, self.token_check)
        if reason is None:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": reason}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
