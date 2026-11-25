"""Loopback/Origin/Host guard: unit tests on decide() and ASGI tests via FastAPI."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.http_guard import LocalOriginGuard, allowed_origins, decide

TOKEN = "test-token-not-secret"


def _token_check(headers: dict) -> bool:
    auth = headers.get("authorization", "")
    return auth == f"Bearer {TOKEN}" or headers.get("x-arc-tooling-token") == TOKEN


ORIGINS = allowed_origins(7860)


def _decide(method, path, **headers):
    return decide(method, path, {k.replace("_", "-"): v for k, v in headers.items()}, ORIGINS,
                  token_check=_token_check)


def test_loopback_same_origin_allowed():
    assert _decide("POST", "/mcp", host="127.0.0.1:7860", origin="http://127.0.0.1:7860") is None
    assert _decide("POST", "/mcp", host="localhost:7860", origin="http://localhost:7860") is None
    assert _decide("POST", "/mcp", host="[::1]:7860", origin="http://[::1]:7860") is None


def test_foreign_origin_blocked():
    assert _decide("POST", "/mcp", host="127.0.0.1:7860", origin="https://evil.example") == "origin not allowed"
    assert _decide("POST", "/mcp", host="127.0.0.1:7860", origin="http://127.0.0.1:9999") == "origin not allowed"
    assert _decide("POST", "/mcp", host="127.0.0.1:7860", origin="null") == "origin not allowed"


def test_non_loopback_host_blocked():
    # DNS rebinding: attacker name resolving to 127.0.0.1
    assert _decide("POST", "/mcp", host="evil.example:7860", origin="http://127.0.0.1:7860") == "host not allowed"
    assert _decide("GET", "/api/status", host="192.168.1.20:7860") == "host not allowed"
    assert _decide("GET", "/api/status") == "host not allowed"  # missing Host


def test_missing_origin_blocked_without_token():
    assert _decide("POST", "/mcp", host="127.0.0.1:7860") == "origin required (or local tooling token)"
    assert _decide("POST", "/api/workspace/write", host="127.0.0.1:7860") is not None
    assert _decide("GET", "/api/memory/search", host="127.0.0.1:7860") is not None


def test_missing_origin_with_token_allowed():
    assert _decide("POST", "/mcp", host="127.0.0.1:7860", authorization=f"Bearer {TOKEN}") is None
    assert _decide("POST", "/mcp", host="127.0.0.1:7860", x_arc_tooling_token=TOKEN) is None
    assert _decide("POST", "/mcp", host="127.0.0.1:7860", authorization="Bearer wrong") is not None


def test_public_reads_without_origin():
    assert _decide("GET", "/api/status", host="127.0.0.1:7860") is None
    assert _decide("GET", "/", host="127.0.0.1:7860") is None
    assert _decide("POST", "/api/status", host="127.0.0.1:7860") is not None


@pytest.fixture()
def client():
    app = FastAPI()

    @app.post("/mcp")
    async def mcp():
        return {"ok": True}

    @app.get("/api/status")
    async def status():
        return {"ok": True}

    @app.websocket("/ws/chat")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    app.add_middleware(LocalOriginGuard, port=7860, token_check=_token_check)
    return TestClient(app, base_url="http://127.0.0.1:7860")


def test_asgi_http(client):
    ok = client.post("/mcp", headers={"Origin": "http://127.0.0.1:7860"})
    assert ok.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in ok.headers}
    assert client.post("/mcp", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/mcp").status_code == 403
    assert client.post("/mcp", headers={"Host": "evil.example:7860", "Origin": "http://127.0.0.1:7860"}).status_code == 403
    assert client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get("/api/status").status_code == 200


def test_asgi_preflight_gets_no_cors(client):
    response = client.options("/mcp", headers={"Origin": "https://evil.example",
                                                "Access-Control-Request-Method": "POST"})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_asgi_websocket(client):
    with client.websocket_connect("/ws/chat", headers={"Host": "127.0.0.1:7860", "Origin": "http://127.0.0.1:7860"}) as ws:
        assert ws.receive_text() == "hi"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat", headers={"Host": "127.0.0.1:7860", "Origin": "https://evil.example"}) as ws:
            ws.receive_text()
