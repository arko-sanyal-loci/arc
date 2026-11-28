"""
app/server.py — Arc interaction server

FastAPI + WebSocket backend:
  GET  /                     → serves index.html
  GET  /api/status           → engine status (heartbeat, GPU, API health)
  POST /api/chat             → single-turn completion (JSON)
  WS   /ws/chat              → streaming chat (WebSocket)
  GET  /api/memory/search    → Loci memory search
  POST /api/memory/ingest    → ingest a document into Loci
  GET  /api/health           → provider health (xAI + nexos + local)
  POST /mcp                  → MCP protocol endpoint (tool calls)

MCP compatibility:
  Implements a subset of the Model Context Protocol server spec.
  Tools exposed: arc_chat, arc_memory_search, arc_status, arc_ingest
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Arc core
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.heartbeat      import HeartbeatEngine, LANE_RTX, LANE_ARC, LANE_CPU
from core.engine_sync    import EngineSync
from core.gpu_offload    import GPUOffloadManager, query_vram, LANE_RTX as _LANE_RTX, LANE_ARC as _LANE_ARC
from core.api_provision  import APIProvisioner, CompletionRequest, ChatMessage, Provider
from core.tiered_memory  import TieredMemoryManager, plan_migrations
from core.gpu_telemetry  import collect_gpu_telemetry
from core.orientation    import collect_orientation
from core.profiles       import get_profile, load_profiles
from core import offline_ledger


# ── Global state (initialised in lifespan) ────────────────────────────────────

heartbeat:    HeartbeatEngine | None      = None
engine_sync:  EngineSync | None           = None
gpu_manager:  GPUOffloadManager | None    = None
tier_manager: TieredMemoryManager | None  = None
provisioner:  APIProvisioner | None       = None
_hb_task:     asyncio.Task | None         = None


# ── Lifespan (replaces deprecated on_event) ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    global heartbeat, engine_sync, gpu_manager, tier_manager, provisioner, _hb_task

    # Heartbeat
    heartbeat = HeartbeatEngine()
    heartbeat.register_worker("rtx-worker-0", LANE_RTX)
    heartbeat.register_worker("arc-worker-0", LANE_ARC)
    heartbeat.register_worker("cpu-worker-0", LANE_CPU)

    # Engine sync (every 50 ticks)
    engine_sync = EngineSync()
    engine_sync.ensure_db()
    heartbeat.on_sync(engine_sync.sync_tick)

    # GPU offload manager
    gpu_manager = GPUOffloadManager()

    # Tiered memory manager — wired to run a migration plan on every sync tick
    tier_manager = TieredMemoryManager()

    async def _tier_sync(tick_id: int) -> None:
        """Run SLP heat-driven tier migration plan on every heartbeat sync cycle."""
        if tier_manager is not None:
            migration = plan_migrations(tier_manager)
            if migration.promotions or migration.demotions:
                # Log summary; actual DMA transfer driven by gpu_offload.py
                total = len(migration.promotions) + len(migration.demotions)
                print(
                    f"[tier_sync] tick={tick_id}"
                    f"  promote={len(migration.promotions)}"
                    f"  demote={len(migration.demotions)}"
                    f"  skipped={len(migration.skipped)}"
                    f"  total={total}"
                )

    heartbeat.on_sync(_tier_sync)

    # API provisioner
    provisioner = APIProvisioner.from_config()
    await provisioner.start()

    # Start heartbeat in background
    _hb_task = asyncio.create_task(heartbeat.run())

    yield  # ── server is running ──────────────────────────────────────────────

    # Shutdown
    if heartbeat:
        heartbeat.stop()
    if _hb_task:
        _hb_task.cancel()
        try:
            await asyncio.wait_for(_hb_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    if provisioner:
        await provisioner.stop()
    if engine_sync:
        engine_sync.close()


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Arc",
    description="Arc — Loci-powered local AI with dynamic GPU offloading",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.environ.get(
        "ARC_CORS_ORIGINS",
        "http://localhost:7860,http://127.0.0.1:7860",
    ).split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>Arc</h1><p>Static files not found.</p>")


@app.get("/api/status")
async def status():
    state = engine_sync.state if engine_sync else {}
    rtx   = query_vram(_LANE_RTX)
    arc   = query_vram(_LANE_ARC)

    # Read shm status files
    hb_status = {}
    try:
        hb_status = json.loads(Path("/dev/shm/arc_status.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass

    # Tier stats (read shm snapshot — zero-copy)
    tier_snapshot: dict = {}
    try:
        tier_snapshot = json.loads(Path("/dev/shm/arc_tier_state.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass

    return {
        "timestamp":       time.time(),
        "heartbeat":       hb_status,
        "engine": {
            "tick_id":         getattr(state, "tick_id", 0),
            "node_count":      getattr(state, "loci_node_count", 0),
            "ledger_rows":     getattr(state, "loci_ledger_rows", 0),
            "drift_detected":  getattr(state, "drift_detected", False),
        },
        "gpu": {
            "rtx": {
                "total_mb": rtx.total_mb,
                "used_mb":  rtx.used_mb,
                "free_mb":  rtx.free_mb,
                "util_pct": round(rtx.utilisation * 100, 1),
            },
            "arc": {
                "total_mb": arc.total_mb,
                "used_mb":  arc.used_mb,
                "free_mb":  arc.free_mb,
                "util_pct": round(arc.utilisation * 100, 1),
            },
        },
        "memory_tiers": tier_snapshot.get("tiers", {}),
    }


@app.get("/api/health")
async def health():
    if not provisioner:
        return {"error": "provisioner not started"}
    h = await provisioner.refresh_health()
    return {
        p.name: {
            "reachable":  ph.reachable,
            "latency_ms": round(ph.latency_ms, 1),
            "error":      ph.error,
        }
        for p, ph in h.items()
    }


@app.get("/api/gpu-telemetry")
async def gpu_telemetry():
    """Live, read-only GPU metrics for the Arc dashboard overlay."""
    return collect_gpu_telemetry()


@app.get("/api/orientation")
async def orientation():
    """Fresh, read-only local network and hardware snapshot for Arc's console."""
    return collect_orientation()


@app.get("/api/profiles")
async def profiles():
    """Conversation-derived interaction profiles and linked project references."""
    return {"identity_note": "Profiles are reference labels, not authenticated identities or authorship claims.",
            "profiles": load_profiles()}


@app.get("/api/ledger")
async def ledger(request: Request):
    """Read the append-only local cache from the local command center."""
    if not _local_writer(request):
        return JSONResponse({"error": "ledger reads are limited to loopback"}, status_code=403)
    return {"cache": offline_ledger.cache_status(),
            "work": offline_ledger.recent_work(50),
            "liabilities": offline_ledger.current_liabilities(100)}


def _local_writer(request: Request) -> bool:
    """Restrict ledger writes to loopback clients; this ledger has no login layer."""
    return bool(request.client and request.client.host in {"127.0.0.1", "::1", "localhost"})


class WorkRecordRequest(BaseModel):
    title: str
    detail: str = ""
    project: str = "Arc"
    evidence_uri: str = ""


@app.post("/api/work")
async def work_record(req: WorkRecordRequest, request: Request):
    if not _local_writer(request):
        return JSONResponse({"error": "ledger writes are limited to loopback"}, status_code=403)
    if not req.title.strip() or len(req.title) > 200 or len(req.detail) > 2000:
        return JSONResponse({"error": "title/detail length is invalid"}, status_code=400)
    return offline_ledger.record_work(actor="Arko", event_type="manual_note",
        title=req.title.strip(), detail=req.detail.strip(), project=req.project[:100],
        evidence_uri=req.evidence_uri[:1000])


class LiabilityRecordRequest(BaseModel):
    liability_ref: str
    creditor: str
    debtor: str
    description: str
    state: str = "proposed"
    amount_minor: int | None = None
    currency: str = ""
    evidence_uri: str = ""
    evidence_sha256: str = ""
    source_note: str = ""


@app.post("/api/liabilities")
async def liability_record(req: LiabilityRecordRequest, request: Request):
    if not _local_writer(request):
        return JSONResponse({"error": "ledger writes are limited to loopback"}, status_code=403)
    if not (req.evidence_uri or req.evidence_sha256 or req.source_note.strip()):
        return JSONResponse({"error": "provide an evidence URI, SHA-256, or source note"}, status_code=400)
    try:
        return offline_ledger.record_liability(**(req.model_dump() if hasattr(req, "model_dump") else req.dict()))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ── Chat (REST) ───────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages:    list[dict]           # [{role, content}]
    model:       str | None = None
    provider:    str = "LOCAL"
    max_tokens:  int = 1024
    temperature: float = 0.7
    profile_id: str | None = None


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not provisioner:
        return JSONResponse({"error": "provisioner not ready"}, status_code=503)

    provider = Provider[req.provider.upper()] if req.provider.upper() in Provider.__members__ \
               else Provider.LOCAL

    profile = get_profile(req.profile_id)
    if req.profile_id and profile is None:
        return JSONResponse({"error": "unknown interaction profile"}, status_code=400)

    completion_req = CompletionRequest(
        messages=[ChatMessage(role=m["role"], content=m["content"]) for m in req.messages],
        model=req.model,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        provider=provider,
        metadata={"interaction_profile": profile},
    )

    resp = await provisioner.complete(completion_req)

    if resp.error:
        return JSONResponse({"error": resp.error}, status_code=502)

    # Cache useful work provenance offline without retaining prompt or response text.
    content_hash = hashlib.sha256("\n".join(m.get("content", "") for m in req.messages).encode()).hexdigest()
    offline_ledger.record_work(actor="Arc", event_type="inference", title="Inference request completed",
        detail="Prompt/response content omitted; request fingerprint retained.", project="Arc inference",
        metadata={"provider": resp.provider.name, "model": resp.model,
                  "latency_ms": round(resp.latency_ms, 1), "tokens_used": resp.tokens_used,
                  "request_sha256": content_hash, "profile_id": req.profile_id})

    return {
        "content":     resp.content,
        "provider":    resp.provider.name,
        "model":       resp.model,
        "tokens_used": resp.tokens_used,
        "latency_ms":  round(resp.latency_ms, 1),
    }


# ── Chat (WebSocket streaming) ────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)

            provider_name = data.get("provider", "LOCAL").upper()
            provider = Provider[provider_name] if provider_name in Provider.__members__ \
                       else Provider.LOCAL

            profile_id = data.get("profile_id")
            profile = get_profile(profile_id)
            if profile_id and profile is None:
                await ws.send_json({"type": "error", "error": "unknown interaction profile"})
                continue

            req = CompletionRequest(
                messages=[
                    ChatMessage(role=m["role"], content=m["content"])
                    for m in data.get("messages", [])
                ],
                model=data.get("model"),
                max_tokens=data.get("max_tokens", 1024),
                temperature=data.get("temperature", 0.7),
                provider=provider,
                stream=True,
                metadata={"interaction_profile": profile},
            )

            if not provisioner:
                await ws.send_json({"error": "provisioner not ready"})
                continue

            started_at = time.perf_counter()
            request_sha256 = hashlib.sha256("\n".join(
                str(m.get("content", "")) for m in data.get("messages", [])
            ).encode()).hexdigest()
            await ws.send_json({"type": "start"})
            async for chunk in provisioner.stream(req):
                await ws.send_json({"type": "chunk", "content": chunk})
            offline_ledger.record_work(actor="Arc", event_type="inference", title="Streaming inference completed",
                detail="Prompt/response content omitted; request fingerprint retained.", project="Arc inference",
                metadata={"provider": provider.name, "model": req.model,
                          "latency_ms": round((time.perf_counter() - started_at) * 1000, 1),
                          "request_sha256": request_sha256, "profile_id": profile_id,
                          "streaming": True})
            await ws.send_json({"type": "done"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await ws.send_json({"type": "error", "error": str(exc)})
        except Exception:
            pass


# ── Memory routes ─────────────────────────────────────────────────────────────

@app.get("/api/memory/search")
async def memory_search(q: str = "", limit: int = 10):
    if not engine_sync or not engine_sync._conn:
        return {"results": [], "error": "db not ready"}
    try:
        rows = engine_sync._conn.execute(
            "SELECT node_id, category, content, heat_u16 FROM nodes WHERE content LIKE ? LIMIT ?",
            (f"%{q}%", limit),
        ).fetchall()
        return {
            "query":   q,
            "results": [
                {"node_id": r[0], "category": r[1], "content": r[2][:200], "heat_u16": r[3]}
                for r in rows
            ],
        }
    except Exception as exc:
        return {"results": [], "error": str(exc)}


class IngestRequest(BaseModel):
    content:  str
    category: str = "STANDARD"
    project:  str = "default"


@app.post("/api/memory/ingest")
async def memory_ingest(req: IngestRequest):
    if not engine_sync or not engine_sync._conn:
        return JSONResponse({"error": "db not ready"}, status_code=503)
    try:
        node_id = str(uuid.uuid4())
        now     = time.time()
        h       = hashlib.sha256(
            f"{now}{req.category}{req.content}".encode()
        ).hexdigest()
        engine_sync._conn.execute(
            "INSERT INTO nodes (node_id, category, project, content, drive_hash, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (node_id, req.category, req.project, req.content, h, now, now),
        )
        engine_sync._conn.commit()
        return {"node_id": node_id, "status": "ingested"}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── MCP endpoint ──────────────────────────────────────────────────────────────

MCP_TOOLS = [
    {
        "name":        "arc_chat",
        "description": "Send a message to Arc and get a response",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message":  {"type": "string"},
                "provider": {"type": "string", "enum": ["LOCAL", "NEXOS", "XAI"]},
                "profile_id": {"type": "string", "description": "Optional self-selected interaction profile"},
            },
            "required": ["message"],
        },
    },
    {
        "name":        "arc_memory_search",
        "description": "Search Arc's Loci memory for relevant context",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name":        "arc_status",
        "description": "Get Arc engine status (heartbeat, GPU, memory)",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body   = await request.json()
    method = body.get("method", "")

    if method == "tools/list":
        return {"tools": MCP_TOOLS}

    if method == "tools/call":
        tool  = body.get("params", {}).get("name", "")
        args  = body.get("params", {}).get("arguments", {})

        if tool == "arc_status":
            s = await status()
            return {"content": [{"type": "text", "text": json.dumps(s)}]}

        if tool == "arc_memory_search":
            r = await memory_search(q=args.get("query", ""), limit=args.get("limit", 5))
            return {"content": [{"type": "text", "text": json.dumps(r)}]}

        if tool == "arc_chat":
            chat_req = ChatRequest(
                messages=[{"role": "user", "content": args.get("message", "")}],
                provider=args.get("provider", "LOCAL"),
                profile_id=args.get("profile_id"),
            )
            r = await chat(chat_req)
            return {"content": [{"type": "text", "text": json.dumps(r)}]}

    return JSONResponse({"error": f"unknown MCP method: {method}"}, status_code=400)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=os.environ.get("ARC_BIND_HOST", "127.0.0.1"),
                port=int(os.environ.get("ARC_PORT", "7860")), reload=False, log_level="info")
