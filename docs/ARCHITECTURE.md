# Arc Runtime — Architecture Reference
*2026-11-18*

---

## System Overview

Arc is a local AI inference runtime built on the **Loci Engine** — a heat-driven, three-tier memory system that places node context in VRAM, DDR5, or NVMe based on a continuously updated "heat" score. The server exposes a FastAPI chat interface (port 7860), a WebSocket streaming endpoint, and a Model Context Protocol (MCP) endpoint, allowing any MCP-compatible client to call Arc as a tool. All routing is fully local-first: completions are attempted on-device via Arc B70 before falling through to nexos.ai (compute offset) and xAI Grok (failsafe).

---

## Hardware Topology

Arc runs on a two-GPU pool connected by `/dev/shm`:

```
  ┌────────────────────────┐       /dev/shm        ┌────────────────────────┐
  │   Intel Arc B70        │  ◄─── 7.9 GB/s ───►   │   NVIDIA RTX 5060 Ti   │
  │   Y=1  (sovereignty)   │                        │   Y=0  (fast lane)     │
  │   32 GB GDDR6          │                        │   16 GB GDDR7          │
  │   608 GB/s internal BW │                        │   448 GB/s internal BW │
  └────────────────────────┘                        └────────────────────────┘
                                                    
         ┌───────────────────────────────────────┐
         │   Host DDR5 32 GB  (XMP1 5200 MT/s)   │  WARM tier
         └───────────────────────────────────────┘
                                                    
         ┌───────────────────────────────────────┐
         │   NVMe SSD                            │  COLD tier
         └───────────────────────────────────────┘
```

**Inter-GPU channel** is a 4 MiB `mmap`-ed ring at `/dev/shm/arc_offload_ring` (pre-allocated by `start.sh`) paired with a staging directory at `/dev/shm/arc_offload_stage/` managed by `SHMStager`. Sustained cross-GPU bandwidth is 7.9 GB/s — not the internal per-card bandwidth.

The Arc B70 carries a **sovereignty lock**: its lane is designated `Y=1`, and nodes with `pinned=True` always reside on Arc and are never migrated. Routing decisions for sovereign nodes always return `OffloadDir.NONE`.

---

## Three-Tier Memory Hierarchy

Memory placement is driven entirely by the SLP (SymLinkPrev) **heat score** `H ∈ [0.0500, 0.9999]`.

| Tier | Heat threshold | Physical storage | Capacity |
|---|---|---|---|
| HOT | H ≥ 0.70 | VRAM (B70 + RTX aggregate) | 48 GB |
| WARM | 0.35 ≤ H < 0.70 | DDR5 host RAM | 32 GB |
| COLD | H < 0.35 | NVMe SSD | unbounded |

`TieredMemoryManager` maintains an in-process placement ledger (`dict[str, NodePlacement]`). It is not a DMA engine — it tracks *where* nodes should be; `GPUOffloadManager` performs the actual cross-GPU transfers via `SHMStager`. Tier capacity is enforced with a 5 % headroom reserve: a promotion is skipped if `used_mb + node_mb > 0.95 × capacity_mb`.

The current ledger state is published to `/dev/shm/arc_tier_state.json` on every `place()` or `update_heat()` call. The `/api/status` endpoint reads this file at zero-copy cost to surface tier utilisation.

---

## Heartbeat Engine

`HeartbeatEngine` ticks every **140 ms**. On each tick it:

1. Writes a 12-byte `TICK_RECORD_FMT = "<QHH"` record (timestamp_µs, tick_id, lane_mask) into the `SHMRing` circular buffer at `/dev/shm/arc_heartbeat`.
2. Checks every registered worker's `last_seen_tick`; workers silent for 3 missed ticks enter `WorkerStatus.WARN`, and 10 missed ticks trigger `WorkerStatus.DEAD`.
3. Every `sync_every_n=50` ticks (~7 s), fires all handlers registered via `heartbeat.on_sync(fn)`.

Two `on_sync` handlers are registered at server startup:

- **`engine_sync.sync_tick`** — synchronises the Loci node graph into SQLite and `/dev/shm`.
- **`_tier_sync`** — calls `plan_migrations(tier_manager)` and logs any pending promotions or demotions.

Three lane bits are defined: `LANE_RTX = 0x01`, `LANE_ARC = 0x02`, `LANE_CPU = 0x04`. Each worker registers with a lane mask so the heartbeat knows which hardware context missed a tick.

---

## Provider Routing

Completion requests follow a three-tier fallback chain, resolved by `APIProvisioner`:

```
LOCAL (Arc B70 / on-device)
  ↓  if unavailable or key absent
NEXOS (nexos.ai compute-offset proxy)
  ↓  if unhealthy
XAI (Grok failsafe)
```

Provider health is polled every **30 seconds** by `health_check_all()`, which fires `_ping_provider()` concurrently for all three providers. Each `ProviderHealth` record stores the last latency, status code, and a boolean `healthy` flag.

nexos.ai requires **both** `Authorization: Bearer <key>` and `X-Api-Key: <key>` headers on every request — a plain `Bearer`-only call will be rejected.

---

## Startup Sequence

`start.sh` runs six phases before handing off to uvicorn:

1. Load `.env` → export all `ARC_*` variables to the shell environment.
2. Create `data/` and `logs/` directories; pre-allocate `/dev/shm/arc_offload_ring` (4 MiB) and `/dev/shm/arc_heartbeat` (4 KB) if absent.
3. Install `requirements.txt` if core imports are missing.
4. Bootstrap the SQLite schema via `EngineSync.ensure_db()` (WAL, three tables: `nodes`, `ledger`, `engine_syncs`).
5. Probe GPUs: `nvidia-smi` for RTX, Intel Xe sysfs for Arc B70.
6. Launch a background heartbeat daemon, then `exec uvicorn app.server:app`.

Inside the FastAPI process, `lifespan()` runs the in-process startup in this order:

```
HeartbeatEngine.__init__()
EngineSync.__init__()  +  ensure_db()
GPUOffloadManager.__init__()
TieredMemoryManager.__init__()
APIProvisioner.__init__()  →  start()  →  aiohttp.ClientSession
heartbeat.on_sync(engine_sync.sync_tick)
heartbeat.on_sync(_tier_sync)
asyncio.create_task(heartbeat.run())          # background tick loop
```

Shutdown (on `yield` return) calls `APIProvisioner.stop()`, then `asyncio.wait_for(_hb_task, timeout=2.0)`.

---

## Key Data Flow: Chat Request End-to-End

```
Client  POST /api/chat  {"messages": [...], "stream": false}
  │
  ▼
chat()  →  ChatRequest validated by Pydantic
  │
  ▼
APIProvisioner.complete(messages, model)
  │
  ├─ Check provider chain: LOCAL → NEXOS → XAI
  │
  ├─ [if LOCAL healthy]  POST http://localhost:<local_port>/v1/chat/completions
  │
  ├─ [else NEXOS]  POST https://api.nexos.ai/v1/chat/completions
  │                     Authorization: Bearer <key>
  │                     X-Api-Key: <key>
  │
  └─ [else XAI]   POST https://api.x.ai/v1/chat/completions
  │
  ▼
CompletionResponse  →  {"role": "assistant", "content": "..."}
  │
  ▼
Client  200 {"response": "...", "provider": "LOCAL|NEXOS|XAI", ...}
```

For streaming (`stream: true` or `/ws/chat`), `APIProvisioner.stream()` yields SSE chunks; the WebSocket handler wraps each `data:` line into a `{"type": "token", "content": "..."}` frame.

---

## SLP Fidelity Model

Node fidelity scales with heat — not raw byte count:

| Heat range | Fidelity | Approximate size |
|---|---|---|
| H ≈ 0.05 (COLD floor) | Stub | ~20 bytes |
| H ≈ 0.35 (WARM threshold) | Compressed summary | ~1–4 KB |
| H ≈ 0.70 (HOT threshold) | Full context | up to 32 KB |
| H = 1.0 (sovereignty) | Full context, immovable | up to 32 KB, `pinned=True` |

The 20-byte SLP register encodes `node_id`, `category_id`, `timestamp_s`, `heat_u16`, `class_flags`, `routing_hint`, and `content_sha_lo32`. Three registers pack into one 64-byte L1 cache line.
