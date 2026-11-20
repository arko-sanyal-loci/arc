"""
app/server.py — Arc interaction server

FastAPI + WebSocket backend:
  GET  /                       → serves index.html
  GET  /api/status             → engine status (heartbeat, GPU, API health)
  POST /api/chat               → single-turn completion (JSON)
  WS   /ws/chat                → streaming chat (WebSocket)
  GET  /api/models             → Ollama model list (native /api/tags)
  GET  /api/benchmark          → tok/s benchmark: native vs compat path
  GET  /api/memory/search      → Loci memory search
  POST /api/memory/ingest      → ingest a document into Loci
  GET  /api/health             → provider health (xAI + nexos + local)
  GET  /api/calendar           → CalendarMemoryStore hot entries
  POST /api/calendar/touch     → touch a calendar entry (heat bump)
  POST /mcp                    → MCP protocol endpoint (tool calls)

MCP compatibility:
  Implements a subset of the Model Context Protocol server spec.
  Tools exposed: arc_chat, arc_memory_search, arc_status, arc_ingest
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
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
from core.ollama_client  import OllamaClient
from core.calendar_memory import CalendarMemoryStore


# ── Global state (initialised in lifespan) ────────────────────────────────────

heartbeat:    HeartbeatEngine | None      = None
engine_sync:  EngineSync | None           = None
gpu_manager:  GPUOffloadManager | None    = None
tier_manager: TieredMemoryManager | None  = None
provisioner:  APIProvisioner | None       = None
ollama:       OllamaClient | None         = None
calendar:     CalendarMemoryStore | None  = None
_hb_task:     asyncio.Task | None         = None


# ── Lifespan (replaces deprecated on_event) ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    global heartbeat, engine_sync, gpu_manager, tier_manager, provisioner
    global ollama, calendar, _hb_task

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
        if tier_manager is not None:
            migration = plan_migrations(tier_manager)
            if migration.promotions or migration.demotions:
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

    # Native Ollama client (wraps localhost:11434/api/*)
    ollama = OllamaClient()

    # Calendar memory (time-indexed with hyperbolic heat decay)
    calendar = CalendarMemoryStore(namespace="arc_calendar")

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
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

    hb_status: dict = {}
    try:
        hb_status = json.loads(Path("/dev/shm/arc_status.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass

    tier_snapshot: dict = {}
    try:
        tier_snapshot = json.loads(Path("/dev/shm/arc_tier_state.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass

    return {
        "timestamp":    time.time(),
        "heartbeat":    hb_status,
        "engine": {
            "tick_id":        getattr(state, "tick_id", 0),
            "node_count":     getattr(state, "loci_node_count", 0),
            "ledger_rows":    getattr(state, "loci_ledger_rows", 0),
            "drift_detected": getattr(state, "drift_detected", False),
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


# ── Ollama model list ─────────────────────────────────────────────────────────

@app.get("/api/models")
async def models():
    """List locally available Ollama models via native /api/tags."""
    if not ollama:
        return {"models": [], "error": "ollama client not ready"}
    try:
        model_list = await ollama.list_models()
        return {
            "models": [
                {
                    "name":     m.name,
                    "size_gb":  m.size_gb,
                    "quant":    m.quant,
                    "modified": m.modified,
                }
                for m in model_list
            ]
        }
    except Exception as exc:
        return {"models": [], "error": str(exc)}


# ── Benchmark ─────────────────────────────────────────────────────────────────

@app.get("/api/benchmark")
async def benchmark(
    prompt: str = "Explain what a Loci memory engine does in three sentences.",
    model: str | None = None,
    max_tokens: int = 150,
):
    """
    Run tok/s benchmark: native Ollama path (accurate eval_duration) vs
    OpenAI-compat path (wall-clock). Returns sorted results.

    Hardware reference (design targets):
      RTX 5060 Ti 16 GB GDDR7  (448 GB/s): ~90-100 tok/s for 7B Q4
      Arc Pro B70  32 GB GDDR6 (608 GB/s): ~25-30  tok/s for 32B Q4
      Remote (nexos / xAI):                ~20-50  tok/s + network
    """
    if not ollama:
        return {"results": [], "error": "ollama client not ready"}
    try:
        results = await ollama.benchmark(
            prompt=prompt,
            model=model or None,
            max_tokens=max_tokens,
        )
        return {
            "results": [
                {
                    "provider":    r.provider,
                    "model":       r.model,
                    "tok_per_sec": r.tok_per_sec,
                    "total_tokens": r.total_tokens,
                    "latency_ms":  r.latency_ms,
                    "error":       r.error,
                }
                for r in results
            ],
            "hardware_reference": {
                "rtx_5060_ti_7b_q4":  "90-100 tok/s",
                "arc_b70_32b_q4":     "25-30 tok/s",
                "remote_nexos_xai":   "20-50 tok/s (+ network)",
            },
        }
    except Exception as exc:
        return {"results": [], "error": str(exc)}


# ── Chat (REST) ───────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages:    list[dict]
    model:       str | None = None
    provider:    str = "LOCAL"
    max_tokens:  int = 1024
    temperature: float = 0.7


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not provisioner:
        return JSONResponse({"error": "provisioner not ready"}, status_code=503)

    provider = Provider[req.provider.upper()] if req.provider.upper() in Provider.__members__ \
               else Provider.LOCAL

    completion_req = CompletionRequest(
        messages=[ChatMessage(role=m["role"], content=m["content"]) for m in req.messages],
        model=req.model,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        provider=provider,
    )

    resp = await provisioner.complete(completion_req)

    if resp.error:
        return JSONResponse({"error": resp.error}, status_code=502)

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
            )

            if not provisioner:
                await ws.send_json({"error": "provisioner not ready"})
                continue

            await ws.send_json({"type": "start"})
            async for chunk in provisioner.stream(req):
                await ws.send_json({"type": "chunk", "content": chunk})
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


# ── Calendar memory routes ────────────────────────────────────────────────────

@app.get("/api/calendar")
async def calendar_hot(threshold: float = 0.6, n: int = 20):
    """Return hot calendar entries (heat >= threshold)."""
    if not calendar:
        return {"entries": [], "error": "calendar not ready"}
    entries = calendar.recent(n=n, threshold=threshold)
    return {
        "entries": [
            {
                "id":             e.id,
                "item_type":      e.item_type,
                "canonical_text": e.canonical_text,
                "era":            e.era,
                "heat":           round(e.heat, 4),
                "valid_from":     e.valid_from,
                "valid_until":    e.valid_until,
            }
            for e in entries
        ]
    }


class CalendarTouchRequest(BaseModel):
    entry_id:   str
    relevance:  float = 1.0
    hop_weight: float = 1.0


@app.post("/api/calendar/touch")
async def calendar_touch(req: CalendarTouchRequest):
    if not calendar:
        return JSONResponse({"error": "calendar not ready"}, status_code=503)
    try:
        calendar.touch(req.entry_id, relevance=req.relevance, hop_weight=req.hop_weight)
        return {"status": "touched", "entry_id": req.entry_id}
    except KeyError:
        return JSONResponse({"error": "entry not found"}, status_code=404)


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
    {
        "name":        "arc_benchmark",
        "description": "Run tok/s benchmark for LOCAL Ollama vs remote providers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
            },
        },
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
            )
            r = await chat(chat_req)
            return {"content": [{"type": "text", "text": json.dumps(r)}]}

        if tool == "arc_benchmark":
            r = await benchmark(model=args.get("model"))
            return {"content": [{"type": "text", "text": json.dumps(r)}]}

    return JSONResponse({"error": f"unknown MCP method: {method}"}, status_code=400)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=7860, reload=False, log_level="info")
