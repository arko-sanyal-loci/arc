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
  Tools exposed: chat, memory, status, stream search/index, and bounded workspace operations
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
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx

# Arc core
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.heartbeat      import HeartbeatEngine, LANE_RTX, LANE_ARC, LANE_CPU
from core.engine_sync    import EngineSync
from core.gpu_offload    import GPUOffloadManager, query_vram, LANE_RTX as _LANE_RTX, LANE_ARC as _LANE_ARC
from core.api_provision  import APIProvisioner, CompletionRequest, ChatMessage, Provider
from core.cache_layer    import LociItem
from core.tiered_memory  import TieredMemoryManager, plan_migrations
from core.loci_memory    import LociMemoryBridge
from core.platform_paths import default_shm_path, ensure_runtime_dirs
from core.http_guard import LocalOriginGuard
from core.scratchpad import scratch
from core.workspace import WorkspaceError, list_files, read_file, run_check, write_file


# ── Global state (initialised in lifespan) ────────────────────────────────────

heartbeat:    HeartbeatEngine | None      = None
engine_sync:  EngineSync | None           = None
gpu_manager:  GPUOffloadManager | None    = None
tier_manager: TieredMemoryManager | None  = None
provisioner:  APIProvisioner | None       = None
memory_bridge: LociMemoryBridge | None    = None
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
    global heartbeat, engine_sync, gpu_manager, tier_manager, provisioner, memory_bridge, _hb_task, _lane_monitor_task

    ensure_runtime_dirs()
    memory_bridge = LociMemoryBridge()

    # Heartbeat is optional on platforms without shared-memory support.
    heartbeat = None if os.environ.get("ARC_DISABLE_HEARTBEAT") == "1" else HeartbeatEngine()
    if heartbeat:
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
    if heartbeat:
        heartbeat.on_tick(_ack_ready_lanes)

    # Engine sync (every 50 ticks)
    engine_sync = EngineSync()
    engine_sync.ensure_db()
    if heartbeat:
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

    if heartbeat:
        heartbeat.on_sync(_tier_sync)

    # API provisioner
    provisioner = APIProvisioner.from_config()
    await provisioner.start()

    # Start heartbeat in background
    _hb_task = asyncio.create_task(heartbeat.run()) if heartbeat else None
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
    if memory_bridge:
        memory_bridge.close()


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Arc",
    description="Arc — Loci-powered local AI with dynamic GPU offloading",
    version="0.1.0",
    lifespan=lifespan,
)

# One trust seam for /mcp, /api/*, /v1/* and /ws/*: loopback Host, Arc's own
# Origin, or the local tooling token. No CORS headers are emitted.
app.add_middleware(LocalOriginGuard)

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
        hb_status = json.loads(Path(default_shm_path("arc_status.json")).read_text())
    except (OSError, json.JSONDecodeError):
        pass

    # Tier stats (read shm snapshot — zero-copy)
    tier_snapshot: dict = {}
    try:
        tier_snapshot = json.loads(Path(default_shm_path("arc_tier_state.json")).read_text())
    except (OSError, json.JSONDecodeError):
        pass

    return {
        "timestamp":       time.time(),
        "capabilities": {
            "memory": {
                "backend": "loci",
                "glassbox": True,
                "session_continuity": True,
            },
            "modalities": {
                "text": "native",
                "audio": "local-consent-gated",
                "video": "local-consent-gated",
                "music": "local-consent-gated-acoustic-analysis",
            },
            "tools": {
                "workspace": "bounded",
                "scratchpad": "local",
                "mcp": "enabled",
                "browser": "visible-loopback-only",
            },
            "context_staging": {
                "neighborhood_hops": 1,
                "heat_gradient": 0.85,
                "diminishing_return_floor": 0.0005,
                "token_budget": None,
                "mode": "standby-plan",
            },
            "active_generation": {
                "stop_policy": "semantic-utility-plateau",
                "plateau_window": 3,
                "min_improvement": 0.01,
                "min_score_to_stop": 0.20,
                "token_budget_is_safety_guard": True,
            },
            "staged_orchestration": {
                "mode": "fast-scaffold+parallel-research->heavy-synthesis",
                "fast_model": "qwen3.5:4b-q4_K_M",
                "medium_model": "gemma4:26b-a4b-it-q4_K_M",
                "heavy_model": "qwen3.6:27b-q4_K_M",
                "neighborhood_hop": 1,
            },
        },
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
                "util_pct": round(
                    rtx.gpu_util_pct if rtx.gpu_util_pct is not None else rtx.utilisation * 100,
                    1,
                ),
                "telemetry_available": rtx.telemetry_available,
                "telemetry_source": rtx.telemetry_source,
            },
            "arc": {
                "total_mb": arc.total_mb,
                "used_mb":  arc.used_mb,
                "free_mb":  arc.free_mb,
                "util_pct": round(
                    arc.gpu_util_pct if arc.gpu_util_pct is not None else arc.utilisation * 100,
                    1,
                ),
                "telemetry_available": arc.telemetry_available,
                "telemetry_source": arc.telemetry_source,
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
            "credential_configured": ph.credential_configured,
            "api_ready": ph.api_ready,
            "latency_ms": round(ph.latency_ms, 1),
            "error":      ph.error,
        }
        for p, ph in h.items()
    }


# ── Chat (REST) ───────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages:    list[dict]           # [{role, content}]
    session_id:  str | None = None
    model:       str | None = None
    provider:    str = "LOCAL"
    max_tokens:  int = 1024
    temperature: float = 0.7


class AnthropicMessagesRequest(BaseModel):
    model: str | None = None
    messages: list[dict]
    max_tokens: int = Field(default=1024, ge=1, le=200000)
    system: str | list[dict] | None = None
    temperature: float = Field(default=0.7, ge=0, le=1)
    stream: bool = False
    tools: list[dict] = Field(default_factory=list)
    tool_choice: dict | None = None


def _anthropic_text_content(content: str | list[dict]) -> str:
    """Flatten supported Anthropic content blocks without hiding unsupported media."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("message content must be a string or content block list")
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("message content blocks must be objects")
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(str(block.get("text", "")))
        elif block_type == "tool_result":
            text_parts.append(json.dumps(block, separators=(",", ":")))
        else:
            raise ValueError(f"unsupported Anthropic content block: {block_type}")
    return "\n".join(part for part in text_parts if part)


def _anthropic_messages(req: AnthropicMessagesRequest) -> list[dict]:
    messages: list[dict] = []
    if req.system:
        messages.append({"role": "system", "content": _anthropic_text_content(req.system)})
    for message in req.messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            raise ValueError("messages must use user or assistant roles")
        messages.append({
            "role": role,
            "content": _anthropic_text_content(message.get("content", "")),
        })
    if not messages:
        raise ValueError("at least one message is required")
    return messages


class AudioListenRequest(BaseModel):
    session_id: str | None = None
    duration_seconds: float = Field(default=5.0, ge=1.0, le=15.0)
    consent_scope: str = Field(default="explicit_opt_in", pattern="^explicit_opt_in$")


def _record_and_transcribe(duration_seconds: float) -> str:
    """Capture one transient local microphone clip and return its transcript."""
    recorder = os.environ.get("ARC_MIC_DEVICE", "plughw:1,0")
    transcriber_python = os.environ.get(
        "ARC_TRANSCRIBER_PYTHON",
        str(Path(__file__).resolve().parents[2] / "mr-calendar" / ".venv" / "bin" / "python"),
    )
    model = os.environ.get("ARC_LIVE_WHISPER_MODEL", "tiny")
    audio_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="arc-mic-", suffix=".wav", delete=False) as audio:
            audio_path = audio.name
        record = subprocess.run(
            [
                "arecord", "-q", "-D", recorder, "-f", "S16_LE",
                "-r", "16000", "-c", "1", "-d", str(round(duration_seconds)),
                audio_path,
            ],
            capture_output=True,
            text=True,
            timeout=duration_seconds + 10,
            check=False,
        )
        if record.returncode:
            detail = (record.stderr or record.stdout).strip()
            raise RuntimeError(f"microphone capture failed: {detail[:240]}")

        script = (
            "import json, sys\n"
            "from faster_whisper import WhisperModel\n"
            "model = WhisperModel(sys.argv[2], device='cpu', compute_type='int8', "
            "download_root=None)\n"
            "segments, _ = model.transcribe(sys.argv[1], beam_size=1, "
            "vad_filter=True, condition_on_previous_text=False)\n"
            "print(json.dumps({'transcript': ' '.join(s.text.strip() for s in segments).strip()}))\n"
        )
        transcribe = subprocess.run(
            [transcriber_python, "-c", script, audio_path, model],
            capture_output=True,
            text=True,
            timeout=max(60, int(duration_seconds * 12)),
            check=False,
        )
        if transcribe.returncode:
            detail = (transcribe.stderr or transcribe.stdout).strip()
            raise RuntimeError(f"local transcription failed: {detail[:240]}")
        result = json.loads(transcribe.stdout.strip().splitlines()[-1])
        transcript = str(result.get("transcript", "")).strip()
        if not transcript:
            raise ValueError("no speech detected")
        return transcript[:4000]
    finally:
        if audio_path:
            try:
                Path(audio_path).unlink()
            except OSError:
                pass


@app.post("/api/audio/listen")
async def audio_listen(req: AudioListenRequest):
    """Explicitly capture speech locally, then run the same LOCI recall path."""
    if memory_bridge is None:
        return JSONResponse({"error": "canonical memory is not ready"}, status_code=503)
    try:
        transcript = await asyncio.to_thread(
            _record_and_transcribe, req.duration_seconds
        )
        session_id = _chat_session_id(
            [{"role": "user", "content": transcript}], req.session_id
        )
        memory_bridge.ingest_turn(
            session_id=session_id,
            role="user",
            content=transcript,
            position=0,
        )
        recall = memory_bridge.recall(session_id, transcript, limit=8)
        return {
            "transcript": transcript,
            "session_id": session_id,
            "recall": {
                "archetype": recall.get("archetype"),
                "selected_items": _retrieved_items(recall),
            },
            "raw_audio_persisted": False,
            "consent_scope": req.consent_scope,
        }
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def _complete_dual_gpu(
    completion_req: CompletionRequest,
) -> tuple[object, dict[str, dict]]:
    """Run the fast and deep local lanes concurrently and prefer the deep result."""
    if provisioner is None:
        raise RuntimeError("provisioner not started")
    requests = [
        CompletionRequest(
            messages=completion_req.messages,
            model=completion_req.model,
            max_tokens=completion_req.max_tokens,
            temperature=completion_req.temperature,
            provider=Provider.LOCAL,
            retrieved_items=completion_req.retrieved_items,
        ),
        CompletionRequest(
            messages=completion_req.messages,
            model=completion_req.model,
            max_tokens=completion_req.max_tokens,
            temperature=completion_req.temperature,
            provider=Provider.LOCAL_INTEL,
            retrieved_items=completion_req.retrieved_items,
        ),
    ]
    results = await asyncio.gather(*(provisioner.complete(item) for item in requests))
    lane_ok = [
        not result.error and bool(result.content.strip())
        for result in results
    ]
    lane_results = {
        "rtx": {
            "provider": results[0].provider.name,
            "model": results[0].model,
            "latency_ms": round(results[0].latency_ms, 1),
            "ok": lane_ok[0],
            "error": results[0].error or (None if lane_ok[0] else "empty visible content"),
        },
        "arc": {
            "provider": results[1].provider.name,
            "model": results[1].model,
            "latency_ms": round(results[1].latency_ms, 1),
            "ok": lane_ok[1],
            "error": results[1].error or (None if lane_ok[1] else "empty visible content"),
        },
    }
    successful = [
        result for result, ok in zip((results[1], results[0]), (lane_ok[1], lane_ok[0]))
        if ok
    ]
    if successful:
        return successful[0], lane_results
    results[1].error = "dual GPU lanes failed: " + " | ".join(
        str(result.error) for result in results if result.error
    )
    return results[1], lane_results


async def _complete_chat(
    completion_req: CompletionRequest,
    requested_provider: str,
) -> tuple[object, dict[str, dict] | None]:
    if requested_provider.upper() == "DUAL":
        return await _complete_dual_gpu(completion_req)
    if provisioner is None:
        raise RuntimeError("provisioner not started")
    return await provisioner.complete(completion_req), None


def _chat_session_id(messages: list[dict], requested: str | None) -> str:
    if requested:
        return requested
    return LociMemoryBridge.derive_session_id(messages)


def _memory_augmented_messages(
    messages: list[dict],
    requested_session_id: str | None,
) -> tuple[list[dict], str, dict]:
    if memory_bridge is None:
        raise RuntimeError("canonical LOCI memory is not ready")
    session_id = _chat_session_id(messages, requested_session_id)
    last_user = next(
        (
            str(message.get("content", "")).strip()
            for message in reversed(messages)
            if message.get("role") == "user" and str(message.get("content", "")).strip()
        ),
        "",
    )
    history = messages
    if messages and messages[-1].get("role") == "user":
        history = messages[:-1]
    memory_bridge.ingest_messages(session_id, history)
    recall = memory_bridge.recall(session_id, last_user, limit=8) if last_user else {
        "selected_items": []
    }
    return messages, session_id, recall


def _retrieved_items(recall: dict) -> list[LociItem]:
    """Convert canonical LOCI rows into typed runtime retrieval context."""
    items: list[LociItem] = []
    for item in recall.get("selected_items", []):
        text = str(item.get("canonical_text", "")).strip()
        if not text:
            continue
        items.append(
            LociItem(
                text=text,
                heat=float(item.get("effective_heat", item.get("heat", 0.0))),
                key=str(item.get("id", "")),
                tier=str(item.get("tier", "hop")),
                extra={
                    "namespace": item.get("namespace", "session"),
                    "source_actor": item.get("source_actor", "arc"),
                },
            )
        )
    return items


def _persist_chat_turn(
    messages: list[dict],
    response_content: str,
    session_id: str,
) -> None:
    if memory_bridge is None:
        raise RuntimeError("canonical LOCI memory is not ready")
    memory_bridge.ingest_messages(session_id, messages)
    memory_bridge.ingest_turn(
        session_id=session_id,
        role="assistant",
        content=response_content,
        position=len(messages),
    )


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not provisioner:
        return JSONResponse({"error": "provisioner not ready"}, status_code=503)

    try:
        model_messages, session_id, recall = _memory_augmented_messages(
            req.messages, req.session_id
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        return JSONResponse({"error": f"canonical memory unavailable: {exc}"}, status_code=503)

    provider = Provider[req.provider.upper()] if req.provider.upper() in Provider.__members__ \
               else Provider.LOCAL

    completion_req = CompletionRequest(
        messages=[ChatMessage(role=m["role"], content=m["content"]) for m in model_messages],
        model=req.model,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        provider=provider,
        retrieved_items=_retrieved_items(recall),
    )

    resp, lane_results = await _complete_chat(completion_req, req.provider)

    if resp.error:
        return JSONResponse({"error": resp.error}, status_code=502)

    try:
        _persist_chat_turn(req.messages, resp.content, session_id)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        return JSONResponse({"error": f"response was not persisted to canonical memory: {exc}"}, status_code=503)

    return {
        "content":     resp.content,
        "provider":    resp.provider.name,
        "model":       resp.model,
        "tokens_used": resp.tokens_used,
        "latency_ms":  round(resp.latency_ms, 1),
        **({"coordination": "dual-gpu", "lanes": lane_results} if lane_results else {}),
    }


@app.post("/v1/messages")
async def anthropic_messages(req: AnthropicMessagesRequest, request: Request):
    """Expose Arc's local completion path using the Anthropic Messages shape.

    Tool execution remains MCP-owned; this adapter accepts tool-result blocks so
    an MCP-capable client can continue a conversation without a second format.
    """
    if req.stream:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error",
             "message": "streaming is available through /ws/chat, not /v1/messages"}},
            status_code=400,
        )
    if req.tools and req.tool_choice and req.tool_choice.get("type") in {"any", "tool"}:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error",
             "message": "forced tool use must be sent through Arc MCP"}},
            status_code=400,
        )
    try:
        messages = _anthropic_messages(req)
    except ValueError as exc:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}},
            status_code=400,
        )

    session_id = request.headers.get("x-arc-session-id")
    response = await chat(ChatRequest(
        messages=messages,
        session_id=session_id,
        model=req.model,
        provider="LOCAL",
        max_tokens=req.max_tokens,
        temperature=req.temperature,
    ))
    if isinstance(response, JSONResponse):
        error = json.loads(response.body)
        return JSONResponse(
            {"type": "error", "error": {"type": "api_error",
             "message": error.get("error", "Arc completion failed")}},
            status_code=response.status_code,
        )
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": response["model"],
        "content": [{"type": "text", "text": response["content"]}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": response.get("tokens_used") or 0,
        },
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

            if not provisioner:
                await ws.send_json({"error": "provisioner not ready"})
                continue

            try:
                model_messages, session_id, recall = _memory_augmented_messages(
                    data.get("messages", []), data.get("session_id")
                )
            except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
                await ws.send_json({"type": "error", "error": f"canonical memory unavailable: {exc}"})
                continue
            req_messages = data.get("messages", [])
            requested_provider = data.get("provider", "LOCAL")
            req = CompletionRequest(
                messages=[
                    ChatMessage(role=m["role"], content=m["content"])
                    for m in model_messages
                ],
                model=data.get("model"),
                max_tokens=data.get("max_tokens", 1024),
                temperature=data.get("temperature", 0.7),
                provider=provider,
                stream=True,
                retrieved_items=_retrieved_items(recall),
            )
            await ws.send_json({"type": "start"})
            response_parts: list[str] = []
            if requested_provider.upper() == "DUAL":
                dual_response, lane_results = await _complete_dual_gpu(req)
                if dual_response.error:
                    await ws.send_json({"type": "error", "error": dual_response.error})
                    continue
                response_parts.append(dual_response.content)
                await ws.send_json({
                    "type": "chunk",
                    "content": dual_response.content,
                    "coordination": "dual-gpu",
                    "lanes": lane_results,
                })
            else:
                lane_results = None
                async for chunk in provisioner.stream(req):
                    response_parts.append(chunk)
                    await ws.send_json({"type": "chunk", "content": chunk})
            _persist_chat_turn(req_messages, "".join(response_parts), session_id)
            await ws.send_json({
                "type": "done",
                **({"coordination": "dual-gpu", "lanes": lane_results} if lane_results else {}),
            })

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
    if not memory_bridge:
        return {"results": [], "error": "db not ready"}
    try:
        result = memory_bridge.search(q, limit)
        return {
            "query":   q,
            "results": [
                {
                    "node_id": item["id"],
                    "category": item.get("namespace", "session"),
                    "content": item.get("canonical_text", "")[:1200],
                    "heat": item.get("effective_heat", item.get("heat", 0.0)),
                    "source_actor": item.get("source_actor"),
                }
                for item in result.get("selected_items", [])
            ],
        }
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        return JSONResponse({"results": [], "error": str(exc)}, status_code=503)


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


class WorkspaceWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    content: str = Field(max_length=200000)

    class Config:
        extra = "forbid"


class WorkspaceCheckRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    check: str = Field(default="git-diff-check", pattern="^(git-diff-check|python-compile|bash-syntax|node-syntax)$")

    class Config:
        extra = "forbid"


class WorkspaceListRequest(BaseModel):
    path: str = Field(default="", max_length=400)
    depth: int = Field(default=2, ge=0, le=5)

    class Config:
        extra = "forbid"


class WorkspaceReadRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    max_bytes: int = Field(default=200000, ge=1, le=1000000)

    class Config:
        extra = "forbid"


def _loopback(request: Request) -> bool:
    return bool(request.client and request.client.host in {"127.0.0.1", "::1"})


@app.get("/api/workspace/list")
async def workspace_list(request: Request, path: str = "", depth: int = 2):
    if not _loopback(request):
        return JSONResponse({"error": "loopback client required"}, status_code=403)
    try:
        return json.loads(list_files(path, depth))
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/workspace/read")
async def workspace_read(request: Request, path: str, max_bytes: int = 200000):
    if not _loopback(request):
        return JSONResponse({"error": "loopback client required"}, status_code=403)
    try:
        return json.loads(read_file(path, max_bytes))
    except (OSError, UnicodeDecodeError, WorkspaceError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/workspace/write")
async def workspace_write(request: Request, req: WorkspaceWriteRequest):
    if not _loopback(request):
        return JSONResponse({"error": "loopback client required"}, status_code=403)
    try:
        return json.loads(write_file(req.path, req.content))
    except (OSError, WorkspaceError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/workspace/check")
async def workspace_check(request: Request, req: WorkspaceCheckRequest):
    if not _loopback(request):
        return JSONResponse({"error": "loopback client required"}, status_code=403)
    try:
        return json.loads(run_check(req.path, req.check))
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


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
        loci_item_id = memory_bridge.ingest_document(req.content, req.category, req.project) if memory_bridge else None
        return {"node_id": node_id, "loci_item_id": loci_item_id, "status": "ingested"}
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
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
                "provider": {"type": "string", "enum": ["LOCAL", "DUAL", "ANTHROPIC", "NEXOS", "XAI"]},
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
    {
        "name": "arc_workspace_list",
        "description": "List files inside Arc's configured workspace roots",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "", "maxLength": 400},
                "depth": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_workspace_read",
        "description": "Read a UTF-8 text file inside Arc's configured workspace roots",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "maxLength": 400},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 200000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_workspace_write",
        "description": "Write a bounded UTF-8 text file inside Arc's configured workspace roots",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 400},
                "content": {"type": "string", "maxLength": 200000},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_workspace_check",
        "description": "Run one approved syntax or git diff check on a workspace file",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 400},
                "check": {
                    "type": "string",
                    "enum": ["git-diff-check", "python-compile", "bash-syntax", "node-syntax"],
                    "default": "git-diff-check",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_scratch_write",
        "description": "Write bounded ephemeral context to Arc's in-process scratchpad",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 120},
                "value": {"type": "string", "minLength": 1, "maxLength": 20000},
                "tier": {"type": "string", "enum": ["kernel", "hop"], "default": "hop"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_scratch_read",
        "description": "Read ephemeral context from Arc's in-process scratchpad",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_scratch_clear",
        "description": "Clear Arc's in-process scratchpad",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_browser_navigate",
        "description": "Navigate Arc's visible Chromium browser; adult domains are blocked by the desktop safety policy",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "maxLength": 2000}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_browser_mouse",
        "description": "Move, click, type, or scroll inside Arc's visible Chromium browser only",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["move", "click", "type", "scroll"]},
                "x": {"type": "number", "minimum": 0, "maximum": 10000},
                "y": {"type": "number", "minimum": 0, "maximum": 10000},
                "button": {"type": "string", "enum": ["left", "middle", "right"]},
                "text": {"type": "string", "maxLength": 20000},
                "delta_y": {"type": "number", "minimum": -10000, "maximum": 10000},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arc_browser_sensitive_action",
        "description": "Request one-shot user confirmation for a sensitive browser action",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string", "maxLength": 200}},
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]


async def _browser_bridge_call(path: str, payload: dict | None = None) -> dict:
    """Call the loopback Electron browser bridge; never reaches the network."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.request(
            "POST" if payload is not None else "GET",
            f"http://127.0.0.1:7861{path}",
            json=payload,
        )
        response.raise_for_status()
        return response.json()


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    request_id = body.get("id")
    method = body.get("method", "")

    def mcp_result(value: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": value}

    def error(message: str, code: int = -32600) -> JSONResponse:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id,
             "error": {"code": code, "message": message}},
            status_code=400,
        )

    if method == "initialize":
        return mcp_result({
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "arc", "version": app.version},
        })

    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "tools/list":
        return mcp_result({"tools": MCP_TOOLS})

    if method == "tools/call":
        tool  = body.get("params", {}).get("name", "")
        args  = body.get("params", {}).get("arguments", {})

        if tool == "arc_status":
            s = await status()
            return mcp_result({"content": [{"type": "text", "text": json.dumps(s)}]})

        if tool == "arc_memory_search":
            r = await memory_search(q=args.get("query", ""), limit=args.get("limit", 5))
            return mcp_result({"content": [{"type": "text", "text": json.dumps(r)}]})

        if tool == "arc_stream_search":
            r = await semantic_stream_search(request, q=args.get("query", ""), limit=args.get("limit", 10),
                                             min_similarity=args.get("min_similarity", 0.2))
            if isinstance(r, JSONResponse):
                r = json.loads(r.body)
            return mcp_result({"content": [{"type": "text", "text": json.dumps(r)}]})

        if tool == "arc_stream_index":
            try:
                stream_request = SemanticStreamRequest(**args)
            except Exception as exc:
                return JSONResponse({"error": f"invalid stream request: {str(exc)[:160]}"}, status_code=422)
            r = await semantic_stream_index(stream_request, request)
            if isinstance(r, JSONResponse):
                r = json.loads(r.body)
            return mcp_result({"content": [{"type": "text", "text": json.dumps(r)}]})

        if tool == "arc_chat":
            chat_req = ChatRequest(
                messages=[{"role": "user", "content": args.get("message", "")}],
                provider=args.get("provider", "LOCAL"),
            )
            r = await chat(chat_req)
            return mcp_result({"content": [{"type": "text", "text": json.dumps(r)}]})

        if tool == "arc_workspace_list":
            try:
                workspace_request = WorkspaceListRequest(**args)
                result = list_files(workspace_request.path, workspace_request.depth)
            except (TypeError, ValueError, WorkspaceError) as exc:
                return JSONResponse({"error": f"invalid workspace request: {str(exc)[:160]}"}, status_code=422)
            return mcp_result({"content": [{"type": "text", "text": result}]})

        if tool == "arc_workspace_read":
            try:
                workspace_request = WorkspaceReadRequest(**args)
                result = read_file(workspace_request.path, workspace_request.max_bytes)
            except (TypeError, ValueError, OSError, UnicodeDecodeError, WorkspaceError) as exc:
                return JSONResponse({"error": f"workspace read failed: {str(exc)[:160]}"}, status_code=422)
            return mcp_result({"content": [{"type": "text", "text": result}]})

        if tool == "arc_workspace_write":
            try:
                workspace_request = WorkspaceWriteRequest(**args)
                result = write_file(workspace_request.path, workspace_request.content)
            except (TypeError, ValueError, OSError, WorkspaceError) as exc:
                return JSONResponse({"error": f"workspace write failed: {str(exc)[:160]}"}, status_code=422)
            return mcp_result({"content": [{"type": "text", "text": result}]})

        if tool == "arc_workspace_check":
            try:
                workspace_request = WorkspaceCheckRequest(**args)
                result = run_check(workspace_request.path, workspace_request.check)
            except (TypeError, ValueError, WorkspaceError) as exc:
                return JSONResponse({"error": f"workspace check failed: {str(exc)[:160]}"}, status_code=422)
            return mcp_result({"content": [{"type": "text", "text": result}]})

        if tool in {"arc_scratch_write", "arc_scratch_read", "arc_scratch_clear"}:
            scratch_tool = tool.removeprefix("arc_")
            try:
                if tool == "arc_scratch_write":
                    if not isinstance(args.get("key"), str) or not args["key"].strip():
                        raise ValueError("key is required")
                    if not isinstance(args.get("value"), str) or not args["value"]:
                        raise ValueError("value is required")
                    if len(args["key"]) > 120 or len(args["value"].encode("utf-8")) > 20000:
                        raise ValueError("scratchpad entry exceeds bounds")
                    if args.get("tier", "hop") not in {"kernel", "hop"}:
                        raise ValueError("tier must be kernel or hop")
                elif tool == "arc_scratch_read":
                    if not isinstance(args.get("key"), str) or not args["key"].strip():
                        raise ValueError("key is required")
                result = scratch.handle_tool_call(scratch_tool, args)
            except (TypeError, ValueError) as exc:
                return JSONResponse({"error": f"invalid scratchpad request: {str(exc)[:160]}"}, status_code=422)
            return mcp_result({"content": [{"type": "text", "text": result}]})

        if tool == "arc_browser_navigate":
            try:
                result = await _browser_bridge_call(
                    "/navigate", {"url": args.get("url", "")}
                )
            except (httpx.HTTPError, OSError) as exc:
                return JSONResponse(
                    {"error": f"browser unavailable or navigation blocked: {str(exc)[:160]}"},
                    status_code=503,
                )
            return mcp_result({"content": [{"type": "text", "text": json.dumps(result)}]})

        if tool == "arc_browser_mouse":
            action = args.get("action")
            routes = {
                "move": ("/mouse-move", {"x": args.get("x"), "y": args.get("y")}),
                "click": ("/click", {
                    "x": args.get("x"), "y": args.get("y"),
                    "button": args.get("button", "left"),
                }),
                "type": ("/type", {"text": args.get("text", "")}),
                "scroll": ("/scroll", {
                    "x": args.get("x"), "y": args.get("y"),
                    "deltaY": args.get("delta_y", 0),
                }),
            }
            if action not in routes:
                return JSONResponse({"error": "invalid browser action"}, status_code=422)
            route, payload = routes[action]
            try:
                result = await _browser_bridge_call(route, payload)
            except (httpx.HTTPError, OSError) as exc:
                return JSONResponse(
                    {"error": f"browser unavailable or action rejected: {str(exc)[:160]}"},
                    status_code=503,
                )
            return mcp_result({"content": [{"type": "text", "text": json.dumps(result)}]})

        if tool == "arc_browser_sensitive_action":
            try:
                result = await _browser_bridge_call(
                    "/sensitive-action", {"action": args.get("action", "")}
                )
            except (httpx.HTTPError, OSError) as exc:
                return JSONResponse(
                    {"error": f"browser unavailable: {str(exc)[:160]}"},
                    status_code=503,
                )
            return mcp_result({"content": [{"type": "text", "text": json.dumps(result)}]})

    return error(f"unknown MCP method: {method}", -32601)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=7860, reload=False, log_level="info")
