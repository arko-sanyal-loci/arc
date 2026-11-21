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
import hmac
import hashlib
import json
import math
import os
import sqlite3
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx

# Arc core
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.heartbeat      import HeartbeatEngine, LANE_RTX, LANE_ARC, LANE_CPU
from core.engine_sync    import EngineSync
from core.gpu_offload    import GPUOffloadManager, query_vram, LANE_RTX as _LANE_RTX, LANE_ARC as _LANE_ARC
from core.api_provision  import APIProvisioner, CompletionRequest, ChatMessage, Provider
from core.tiered_memory  import TieredMemoryManager, plan_migrations


# ── Global state (initialised in lifespan) ────────────────────────────────────

heartbeat:    HeartbeatEngine | None      = None
engine_sync:  EngineSync | None           = None
gpu_manager:  GPUOffloadManager | None    = None
tier_manager: TieredMemoryManager | None  = None
provisioner:  APIProvisioner | None       = None
_hb_task:     asyncio.Task | None         = None
_lane_monitor_task: asyncio.Task | None   = None
_lane_health: dict[str, dict] = {
    "rtx": {"ready": False, "models": [], "checked_at": None},
    "arc": {"ready": False, "models": [], "checked_at": None},
}


async def _lane_probe(client: httpx.AsyncClient, lane: str, base_url: str) -> tuple[str, list[str]]:
    """Treat an inference lane as alive only when its local Ollama API answers."""
    try:
        response = await client.get(f"{base_url.rstrip('/')}/api/tags")
        response.raise_for_status()
        models = response.json().get("models", [])
        names = sorted({item["name"] for item in models if isinstance(item, dict) and item.get("name")})
        return lane, names
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return lane, []


async def _monitor_inference_lanes() -> None:
    """Feed real GPU-service readiness into the heartbeat worker states."""
    if heartbeat is None:
        return
    backends = {
        "rtx": ("rtx-worker-0", os.environ.get("ARC_RTX_BACKEND_URL", "http://127.0.0.1:11435")),
        "arc": ("arc-worker-0", os.environ.get("ARC_ARC_BACKEND_URL", "http://127.0.0.1:8000")),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(0.75), trust_env=False) as client:
        while True:
            results = await asyncio.gather(*(
                _lane_probe(client, lane, base_url)
                for lane, (_worker_id, base_url) in backends.items()
            ))
            checked_at = time.time()
            for lane, models in results:
                _lane_health[lane] = {"ready": bool(models), "models": models,
                                      "checked_at": checked_at}
            await asyncio.sleep(2.0)


async def _ack_ready_lanes(_tick_id: int, _now: float) -> None:
    """Keep each heartbeat lane live only while its most recent backend probe passed."""
    if heartbeat is None:
        return
    if _lane_health.get("rtx", {}).get("ready"):
        heartbeat.worker_seen("rtx-worker-0")
    if _lane_health.get("arc", {}).get("ready"):
        heartbeat.worker_seen("arc-worker-0")


# ── Lifespan (replaces deprecated on_event) ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    global heartbeat, engine_sync, gpu_manager, tier_manager, provisioner, _hb_task, _lane_monitor_task

    # Heartbeat
    heartbeat = HeartbeatEngine()
    heartbeat.register_worker("rtx-worker-0", LANE_RTX)
    heartbeat.register_worker("arc-worker-0", LANE_ARC)

    # These are service-readiness workers, not placeholders: the monitor below
    # acknowledges a lane only while its GPU-backed Ollama endpoint answers.
    for lane, (_worker_id, base_url) in {
        "rtx": ("rtx-worker-0", os.environ.get("ARC_RTX_BACKEND_URL", "http://127.0.0.1:11435")),
        "arc": ("arc-worker-0", os.environ.get("ARC_ARC_BACKEND_URL", "http://127.0.0.1:8000")),
    }.items():
        _lane_health[lane] = {"ready": False, "models": [], "checked_at": None,
                              "backend_url": base_url}
    heartbeat.on_tick(_ack_ready_lanes)

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
    _lane_monitor_task = asyncio.create_task(_monitor_inference_lanes())

    yield  # ── server is running ──────────────────────────────────────────────

    # Shutdown
    if heartbeat:
        heartbeat.stop()
    if _lane_monitor_task:
        _lane_monitor_task.cancel()
        try:
            await asyncio.wait_for(_lane_monitor_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
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
        "inference_lanes": {
            lane: {**state,
                   "backend_url": os.environ.get(
                       "ARC_RTX_BACKEND_URL" if lane == "rtx" else "ARC_ARC_BACKEND_URL",
                       "http://127.0.0.1:11435" if lane == "rtx" else "http://127.0.0.1:8000",
                   )}
            for lane, state in _lane_health.items()
        },
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


# ── Chat (REST) ───────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages:    list[dict]           # [{role, content}]
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


class AgentFeedbackRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=80)
    project: str = Field(default="general", min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=8000)
    kind: str = Field(default="observation", pattern="^(observation|question|suggestion|issue)$")


class SemanticStreamRequest(BaseModel):
    stream_id: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=240)
    source_label: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=20000)
    consent_scope: str = Field(pattern="^(user_supplied|explicit_opt_in|derived_metadata)$")
    source_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    frame_references: list[dict] = Field(default_factory=list, max_length=200)

    class Config:
        extra = "forbid"


def _feedback_db() -> sqlite3.Connection:
    path = Path(__file__).parent.parent / "data" / "agent_feedback.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY, created_at REAL NOT NULL, agent TEXT NOT NULL,
        project TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
    )""")
    return conn


@app.get("/api/feedback")
async def list_feedback(limit: int = 50):
    """Local-only feedback inbox for the Arc agent panel."""
    if not 1 <= limit <= 200:
        return JSONResponse({"error": "limit must be between 1 and 200"}, status_code=422)
    try:
        with closing(_feedback_db()) as conn, conn:
            rows = conn.execute(
                "SELECT id,created_at,agent,project,kind,message FROM feedback ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {"items": [dict(zip(("id", "created_at", "agent", "project", "kind", "message"), row)) for row in rows]}
    except sqlite3.Error:
        return JSONResponse({"error": "feedback store unavailable"}, status_code=503)


@app.post("/api/feedback")
async def submit_feedback(req: AgentFeedbackRequest, request: Request):
    """Append a feedback item; API is intentionally restricted to loopback clients."""
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"error": "loopback client required"}, status_code=403)
    item_id = str(uuid.uuid4())
    try:
        with closing(_feedback_db()) as conn, conn:
            conn.execute(
                "INSERT INTO feedback (id,created_at,agent,project,kind,message) VALUES (?,?,?,?,?,?)",
                (item_id, time.time(), req.agent.strip(), req.project.strip(), req.kind, req.message.strip()),
            )
        return {"id": item_id, "status": "saved"}
    except sqlite3.Error:
        return JSONResponse({"error": "feedback store unavailable"}, status_code=503)


def _streams_db() -> sqlite3.Connection:
    path = Path(os.environ.get(
        "ARC_STREAMS_DB", str(Path(__file__).parent.parent / "data" / "semantic_streams.sqlite3")
    )).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS semantic_stream_versions (
        stream_id TEXT NOT NULL, version INTEGER NOT NULL, source_id TEXT NOT NULL,
        source_label TEXT NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL,
        source_sha256 TEXT, consent_scope TEXT NOT NULL, start_ms INTEGER, end_ms INTEGER,
        frame_references_json TEXT NOT NULL, embedding_model TEXT NOT NULL,
        embedding_json TEXT NOT NULL, indexed_at REAL NOT NULL,
        PRIMARY KEY (stream_id, version)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS semantic_stream_source ON semantic_stream_versions(source_id, indexed_at)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS semantic_stream_versions_no_update
        BEFORE UPDATE ON semantic_stream_versions BEGIN SELECT RAISE(ABORT,'append-only journal'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS semantic_stream_versions_no_delete
        BEFORE DELETE ON semantic_stream_versions BEGIN SELECT RAISE(ABORT,'append-only journal'); END""")
    return conn


async def _nomic_embedding(text: str, task: str) -> list[float]:
    endpoints = [os.environ.get("ARC_NOMIC_EMBED_BASE_URL", "http://127.0.0.1:11435").rstrip("/")]
    fallback = os.environ.get("ARC_NOMIC_FALLBACK_URL", "http://127.0.0.1:11438").rstrip("/")
    if fallback and fallback not in endpoints:
        endpoints.append(fallback)
    last_error: Exception | None = None
    for base_url in endpoints:
        try:
            async with httpx.AsyncClient(timeout=45.0, trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/api/embed",
                    json={"model": "nomic-embed-text", "input": f"{task}: {text}"},
                )
                response.raise_for_status()
                vector = response.json()["embeddings"][0]
            if len(vector) != 768 or not all(math.isfinite(float(value)) for value in vector):
                raise ValueError("unexpected Nomic embedding vector")
            return [float(value) for value in vector]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            last_error = exc
    raise ValueError("all Nomic embedding endpoints failed") from last_error


@app.post("/api/streams/index")
async def semantic_stream_index(req: SemanticStreamRequest, request: Request):
    """Append one explicitly consented, text-derived stream version and embedding."""
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"error": "loopback client required"}, status_code=403)
    if req.start_ms is not None and req.end_ms is not None and req.end_ms < req.start_ms:
        return JSONResponse({"error": "end_ms must not precede start_ms"}, status_code=422)
    frame_refs = json.dumps(req.frame_references, ensure_ascii=False, separators=(",", ":"))
    if len(frame_refs.encode("utf-8")) > 32768:
        return JSONResponse({"error": "frame reference metadata is too large"}, status_code=422)
    try:
        vector = await _nomic_embedding(req.content, "search_document")
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return JSONResponse({"error": "Nomic embedding service unavailable or returned an invalid vector"}, status_code=503)
    content_hash = hashlib.sha256(req.content.encode("utf-8")).hexdigest()
    try:
        with closing(_streams_db()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM semantic_stream_versions WHERE stream_id=?",
                (req.stream_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO semantic_stream_versions
                (stream_id,version,source_id,source_label,content,content_sha256,source_sha256,
                 consent_scope,start_ms,end_ms,frame_references_json,embedding_model,embedding_json,indexed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (req.stream_id, version, req.source_id, req.source_label, req.content, content_hash,
                 req.source_sha256, req.consent_scope, req.start_ms, req.end_ms, frame_refs,
                 "nomic-embed-text", json.dumps(vector, separators=(",", ":")), time.time()),
            )
        return {"stream_id": req.stream_id, "version": version, "source_id": req.source_id,
                "content_sha256": content_hash, "embedding_model": "nomic-embed-text", "status": "indexed"}
    except sqlite3.Error:
        return JSONResponse({"error": "semantic stream journal unavailable"}, status_code=503)


@app.get("/api/streams/search")
async def semantic_stream_search(request: Request, q: str = "", limit: int = 10,
                                 min_similarity: float = 0.2):
    """Return semantically ranked latest stream versions with source/frame references."""
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"error": "loopback client required"}, status_code=403)
    q = q.strip()
    if not q or len(q) > 4000 or not 1 <= limit <= 50 or not -1.0 <= min_similarity <= 1.0:
        return JSONResponse({"error": "q is required, limit must be 1..50, and min_similarity must be -1..1"}, status_code=422)
    try:
        query_vector = await _nomic_embedding(q, "search_query")
        with closing(_streams_db()) as conn, conn:
            rows = conn.execute("""SELECT s.stream_id,s.version,s.source_id,s.source_label,s.content,
                s.content_sha256,s.source_sha256,s.consent_scope,s.start_ms,s.end_ms,
                s.frame_references_json,s.embedding_json
                FROM semantic_stream_versions s
                WHERE NOT EXISTS (SELECT 1 FROM semantic_stream_versions n
                    WHERE n.stream_id=s.stream_id AND n.version>s.version)""").fetchall()
        ranked = []
        for row in rows:
            vector = json.loads(row[11])
            norm = math.sqrt(sum(value * value for value in vector))
            qnorm = math.sqrt(sum(value * value for value in query_vector))
            score = sum(a * b for a, b in zip(query_vector, vector)) / (qnorm * norm) if norm and qnorm else 0.0
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        results = []
        relevant = [(score, row) for score, row in ranked if score >= min_similarity]
        for score, row in relevant[:limit]:
            results.append({"stream_id": row[0], "version": row[1], "source_id": row[2],
                "source_label": row[3], "excerpt": row[4][:600], "content_sha256": row[5],
                "source_sha256": row[6], "consent_scope": row[7], "start_ms": row[8],
                "end_ms": row[9], "frame_references": json.loads(row[10]),
                "embedding_model": "nomic-embed-text", "similarity": round(score, 6)})
        return {"query": q, "embedding_model": "nomic-embed-text",
                "min_similarity": min_similarity, "match_count": len(relevant), "results": results}
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return JSONResponse({"error": "Nomic embedding service unavailable or returned an invalid vector"}, status_code=503)
    except sqlite3.Error:
        return JSONResponse({"error": "semantic stream journal unavailable"}, status_code=503)


class IngestRequest(BaseModel):
    content:  str
    category: str = "STANDARD"
    project:  str = "default"


class EvidenceReferenceRequest(BaseModel):
    event_id: str
    recorded_at: str
    source_label: str
    size_bytes: int
    sha256: str
    vps_object: str
    frame_references: list[dict] = Field(default_factory=list)

    class Config:
        extra = "forbid"


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


@app.post("/api/evidence/reference")
async def evidence_reference(req: EvidenceReferenceRequest, request: Request):
    """Store a local-only, metadata-only pointer to encrypted evidence."""
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"error": "loopback client required"}, status_code=403)
    secret = os.environ.get("ARC_EVIDENCE_TOKEN", "")
    if not secret:
        return JSONResponse({"error": "evidence reference is not configured"}, status_code=503)
    if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}"):
        return JSONResponse({"error": "invalid authorization"}, status_code=401)
    try:
        uuid.UUID(req.event_id)
        if not re.fullmatch(r"[0-9a-f]{64}", req.sha256) or req.vps_object != req.sha256:
            raise ValueError("invalid source hash")
        if req.size_bytes < 0 or len(req.source_label) > 120 or len(req.frame_references) > 100:
            raise ValueError("invalid evidence reference bounds")
        safe_proxies = []
        for proxy in req.frame_references:
            if not isinstance(proxy, dict):
                raise ValueError("invalid proxy reference")
            path = proxy.get("proxy", "")
            frame_map = proxy.get("frame_map", "")
            if not re.fullmatch(r"proxies/[0-9a-f]{64}_\d+-\d+\.mp4", path):
                raise ValueError("proxy path is invalid")
            if not re.fullmatch(r"proxies/[0-9a-f]{64}_\d+-\d+\.frames\.json", frame_map):
                raise ValueError("frame-map path is invalid")
            for field in ("proxy_sha256", "frame_map_sha256"):
                if not re.fullmatch(r"[0-9a-f]{64}", str(proxy.get(field, ""))):
                    raise ValueError("proxy hash is invalid")
            if proxy.get("audio_included") is not False:
                raise ValueError("Arc references must use video-only review proxies")
            safe_proxies.append({key: proxy[key] for key in (
                "start_seconds", "end_seconds", "proxy", "proxy_sha256",
                "frame_map", "frame_map_sha256", "frame_count", "audio_included"
            ) if key in proxy})
    except (ValueError, TypeError, KeyError) as exc:
        return JSONResponse({"error": str(exc)[:180]}, status_code=400)
    if not engine_sync or not engine_sync._conn:
        return JSONResponse({"error": "db not ready"}, status_code=503)
    content = json.dumps({
        "schema": "loci.arc.evidence_reference.v1",
        "event_id": req.event_id,
        "recorded_at": req.recorded_at,
        "source_label": req.source_label,
        "size_bytes": req.size_bytes,
        "sha256": req.sha256,
        "encrypted_vps_object": req.vps_object,
        "frame_references": safe_proxies,
    }, sort_keys=True, separators=(",", ":"))
    node_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "loci-evidence:" + req.event_id))
    drive_hash = hashlib.sha256(content.encode()).hexdigest()
    now = time.time()
    try:
        row = engine_sync._conn.execute(
            "SELECT content, drive_hash FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row:
            if row != (content, drive_hash):
                return JSONResponse({"error": "event ID already exists with different content"}, status_code=409)
            return {"node_id": node_id, "status": "already_present"}
        engine_sync._conn.execute(
            "INSERT INTO nodes (node_id, category, project, content, drive_hash, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (node_id, "EVIDENCE_REFERENCE", "private-evidence", content, drive_hash, now, now),
        )
        engine_sync._conn.commit()
        return {"node_id": node_id, "status": "referenced"}
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:180]}, status_code=500)


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
        "name":        "arc_stream_search",
        "description": "Semantically search explicitly indexed, consent-scoped stream text and return source/time/frame references",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "maxLength": 4000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "min_similarity": {"type": "number", "minimum": -1, "maximum": 1, "default": 0.2},
        }, "required": ["query"]},
    },
    {
        "name":        "arc_stream_index",
        "description": "Append an explicitly consented text segment as a new semantic stream version",
        "inputSchema": {"type": "object", "properties": {
            "stream_id": {"type": "string", "maxLength": 120},
            "source_id": {"type": "string", "maxLength": 240},
            "source_label": {"type": "string", "maxLength": 120},
            "content": {"type": "string", "maxLength": 20000},
            "consent_scope": {"type": "string", "enum": ["user_supplied", "explicit_opt_in", "derived_metadata"]},
            "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "start_ms": {"type": "integer", "minimum": 0},
            "end_ms": {"type": "integer", "minimum": 0},
            "frame_references": {"type": "array", "maxItems": 200, "items": {"type": "object"}},
        }, "required": ["stream_id", "source_id", "source_label", "content", "consent_scope"], "additionalProperties": False},
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

        if tool == "arc_stream_search":
            r = await semantic_stream_search(request, q=args.get("query", ""), limit=args.get("limit", 10),
                                             min_similarity=args.get("min_similarity", 0.2))
            if isinstance(r, JSONResponse):
                r = json.loads(r.body)
            return {"content": [{"type": "text", "text": json.dumps(r)}]}

        if tool == "arc_stream_index":
            try:
                stream_request = SemanticStreamRequest(**args)
            except Exception as exc:
                return JSONResponse({"error": f"invalid stream request: {str(exc)[:160]}"}, status_code=422)
            r = await semantic_stream_index(stream_request, request)
            if isinstance(r, JSONResponse):
                r = json.loads(r.body)
            return {"content": [{"type": "text", "text": json.dumps(r)}]}

        if tool == "arc_chat":
            chat_req = ChatRequest(
                messages=[{"role": "user", "content": args.get("message", "")}],
                provider=args.get("provider", "LOCAL"),
            )
            r = await chat(chat_req)
            return {"content": [{"type": "text", "text": json.dumps(r)}]}

    return JSONResponse({"error": f"unknown MCP method: {method}"}, status_code=400)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=7860, reload=False, log_level="info")
