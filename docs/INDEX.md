# Arc Runtime — Master Index
*Generated: 2026-11-18*

---

## 1. File Map

| File | Purpose | Key symbols |
|---|---|---|
| `app/server.py` | FastAPI server, WebSocket, MCP endpoint, lifespan wiring | `lifespan()`, `ChatRequest`, `IngestRequest`, `mcp_endpoint()` |
| `core/heartbeat.py` | 140ms tick engine, /dev/shm ring, worker health tracking | `HeartbeatEngine`, `SHMRing`, `WorkerState`, `WorkerStatus` |
| `core/tiered_memory.py` | 3-tier VRAM/DDR5/NVMe placement ledger | `TieredMemoryManager`, `plan_migrations()`, `heat_to_tier()`, `target_tier()`, `commit_migration()`, `MigrationPlan`, `NodePlacement`, `TierCapacity`, `TierStats` |
| `core/context_compression.py` | LOCI LDCE context compression: kernel / hop (symbol) / offload, LOCI DB source, VRAM pin bridge | `compute_tiers()`, `render()`, `sync_placements()`, `LociMemorySource`, `build_context()`, `LDCE_SOURCE` |
| `docs/HYBRID_INFERENCE_DESIGN.md` | Hybrid local-first + remote-oversight engine design and plan | — |
| `scripts/arc_repair_and_update.sh` | Diagnose-first dpkg repair; drivers, CUDA, B70 runtimes, firmware (WSL-aware) | — |
| `scripts/setup_terminal.sh` | Friendly terminal: Starship, fzf, zoxide, eza, `arc` status card | — |
| `tests/test_tiered_memory.py`, `tests/test_context_compression.py` | 19 tests incl. LDCE parity with LOCI | — |
| `core/engine_sync.py` | Loci ↔ SQLite ↔ /dev/shm sync on every Nth heartbeat tick | `EngineSync`, `EngineState`, `_ensure_tables()`, `_count_rows()` |
| `core/gpu_offload.py` | VRAM query + RTX↔B70 offload policy + SHM staging | `GPUOffloadManager`, `SHMStager`, `decide_offload()`, `query_vram()`, `VRAMStats`, `OffloadDecision`, `OffloadDir` |
| `core/api_provision.py` | Multi-provider gateway (LOCAL→NEXOS→XAI) | `APIProvisioner`, `APIKeys`, `Provider`, `ProviderHealth`, `ChatMessage`, `CompletionRequest`, `CompletionResponse`, `health_check_all()` |
| `loci/slp_encoding.py` | SLP register layout, heat encode/decode, L1 cache-line packing | `SLPRegister`, `ClassFlags`, `RoutingHint`, `heat_to_u16()`, `ALPHA`, `H_MAX` |
| `loci/glassbox_ingest.py` | Gmail → Glassbox Compendium ingest pipeline | `NormalizedRecord`, `Package`, `dual_write()` |
| `loci/dual_write.py` | Atomic Drive + Compendium storage | `DriveClient`, `CompendiumDB`, `dual_write()`, `DualWriteResult` |
| `loci/cas_router.py` | Content-addressed storage routing | — |
| `loci/card_stream.py` | CARD streaming / token-budget gating | — |
| `loci/car_front.py` | CAR (Content-Addressed Registry) frontend | — |
| `loci/crystallize.py` | Node crystallisation (heat promotion to sovereign) | — |
| `loci/hybrid_compose.py` | Hybrid VRAM+DDR compose for multi-tier contexts | — |
| `loci/overlap.py` | Node overlap / deduplication | — |
| `loci/reconcile_tick.py` | Per-tick reconciliation pass | — |
| `loci/playground.py` | Playground nodes (speculative/ephemeral memory) | — |
| `arko-sanyal-loci/LOCI-MEMORY-ENGINE/coordination_bus/bus.py` | Cross-process coordination bus (canonical) | — |
| `arko-sanyal-loci/LOCI-MEMORY-ENGINE/coordination_bus/mcp_server.py` | MCP server for coordination bus | — |
| `start.sh` | Startup orchestration: env, dirs, shm, DB, GPU check, uvicorn | — |
| `data/review_pass_opus.md` | Code review record — 9 bugs fixed | — |

---

## 2. Public HTTP / WebSocket API Surface

| Method | Path | Handler | Description |
|---|---|---|---|
| `GET` | `/` | `root()` | Serves `app/static/index.html` (chat UI) |
| `GET` | `/api/status` | `status()` | Heartbeat + engine + GPU + memory tier stats |
| `GET` | `/api/health` | `health()` | Provider health (LOCAL / NEXOS / XAI) |
| `POST` | `/api/chat` | `chat()` | Single-turn completion; body: `ChatRequest` |
| `WS` | `/ws/chat` | `ws_chat()` | Streaming chat over WebSocket |
| `GET` | `/api/memory/search` | `memory_search()` | Loci node search; params: `q`, `limit` |
| `POST` | `/api/memory/ingest` | `memory_ingest()` | Ingest document into Loci; body: `IngestRequest` |
| `POST` | `/mcp` | `mcp_endpoint()` | MCP JSON-RPC dispatch |

### Request / Response shapes

**`ChatRequest`**
```python
class ChatRequest(BaseModel):
    messages: list[dict]   # [{"role": "user"|"assistant", "content": str}]
    stream:   bool = False
    model:    str | None = None
```

**`IngestRequest`**
```python
class IngestRequest(BaseModel):
    content:  str
    source:   str = "api"
    metadata: dict = {}
```

---

## 3. MCP Tools

All dispatched through `POST /mcp` with `{"method": "tools/call", "params": {"name": "<tool>", "arguments": {...}}}`.

| Tool name | Handler path | Description |
|---|---|---|
| `arc_chat` | `mcp_endpoint()` → inline | Single-turn chat completion via Arc |
| `arc_memory_search` | `mcp_endpoint()` → inline | Search Loci memory nodes |
| `arc_status` | `mcp_endpoint()` → inline | Returns current engine + GPU + tier status |
| `arc_ingest` | `mcp_endpoint()` → inline | Ingest text into Loci memory |

**MCP `tools/list` response** is served at `method: "tools/list"` and returns all four tool definitions with JSON Schema for their arguments.

---

## 4. Configuration & Environment Variables

| Variable | Default | Effect |
|---|---|---|
| `ARC_NEXOS_KEY` | — | nexos.ai API key (primary name) |
| `NEXOS_API_KEY` | — | nexos.ai API key (legacy fallback) |
| `ARC_XAI_KEY` | — | xAI (Grok) API key (primary name) |
| `XAI_API_KEY` | — | xAI API key (legacy fallback) |
| `ARC_LOCAL_API_KEY` | — | Local model API key |
| `ARC_PORT` | `7860` | Port uvicorn binds on |
| `ARC_DB_PATH` | `data/arc.db` | SQLite database path |

Keys are loaded from `.env` by `start.sh` before the server starts, and read by `APIKeys.from_env()` at provisioner init.

---

## 5. Shared Memory Paths

| Path | Allocated by | Written by | Read by | Format |
|---|---|---|---|---|
| `/dev/shm/arc_heartbeat` | `start.sh` (4096 bytes, zero-filled) | `SHMRing.write_tick()` in `heartbeat.py` | `EngineSync._read_heartbeat_status()` | Circular ring of 12-byte `TICK_RECORD_FMT = "<QHH"` records (timestamp_us, tick_id, lane_mask) |
| `/dev/shm/arc_offload_ring` | `start.sh` (4 MiB, zero-filled) | *reserved for future DMA* | *reserved for future DMA* | Raw binary ring (future zero-copy DMA; not touched by current code) |
| `/dev/shm/arc_offload_stage/` | `SHMStager.__init__()` in `gpu_offload.py` | `SHMStager.stage()` | `SHMStager.fetch()` | Directory of JSON manifest + binary blob files for cross-GPU tensor staging |
| `/dev/shm/arc_tier_state.json` | `TieredMemoryManager._publish_state()` | `TieredMemoryManager` on every `place()` / `update_heat()` | `server.py` `/api/status` handler | JSON: `{"tiers": {"VRAM": {...}, "DDR5": {...}, "NVME": {...}}}` |

---

## 6. Cross-File Call Graph

```
start.sh
  └─ uvicorn app.server:app
        │
        ├─ lifespan()  ──────────────────────────────────────┐
        │     ├─ HeartbeatEngine  (core/heartbeat.py)        │
        │     │     └─ on_sync(_tier_sync) ──────────────────┤
        │     │     └─ on_sync(engine_sync.sync_tick) ───────┤
        │     ├─ EngineSync       (core/engine_sync.py)      │
        │     │     └─ _read_heartbeat_status()              │
        │     │          └─ reads /dev/shm/arc_heartbeat     │
        │     ├─ GPUOffloadManager (core/gpu_offload.py)     │
        │     │     ├─ query_vram(LANE_RTX)                  │
        │     │     │     └─ _nvidia_smi_free_mb()           │
        │     │     └─ query_vram(LANE_ARC)                  │
        │     │           └─ _intel_gpu_free_mb()            │
        │     ├─ TieredMemoryManager (core/tiered_memory.py) │
        │     │     ├─ heat_to_tier()                        │
        │     │     └─ _publish_state()                      │
        │     │           └─ writes /dev/shm/arc_tier_state  │
        │     └─ APIProvisioner  (core/api_provision.py)     │
        │           ├─ APIKeys.from_env()                    │
        │           └─ health_check_all()                    │
        │                 └─ _ping_provider() ×3             │
        │                                                    │
        └─ per-request handlers                             │
              ├─ status()          reads /dev/shm/* + ◄──────┘
              │                   calls query_vram()
              ├─ chat()            → APIProvisioner.complete()
              ├─ ws_chat()         → APIProvisioner.stream()
              ├─ memory_search()   → EngineSync / SQLite
              ├─ memory_ingest()   → EngineSync / SQLite
              └─ mcp_endpoint()   → routes to above handlers

_tier_sync (every 50 ticks, ~7 s):
  plan_migrations(tier_manager) → TieredMemoryManager
        └─ pending_migrations() → sorted list of NodePlacement moves
```

---

## 7. Database Schema

SQLite at `data/arc.db` with WAL journal mode.

| Table | Columns | Purpose |
|---|---|---|
| `nodes` | `id, node_id, category_id, heat, tier, pinned, created_at, updated_at` | Loci node placement ledger |
| `ledger` | `id, node_id, old_tier, new_tier, reason, timestamp` | Migration history |
| `engine_syncs` | `id, tick_id, timestamp, lane_mask, node_count, ledger_rows, drift, duration_ms` | Per-sync telemetry |

---

## 8. SLP Heat Thresholds

| Tier | Condition | Storage |
|---|---|---|
| HOT  | H ≥ 0.70 | VRAM (B70: 32 GB GDDR6 + RTX: 16 GB GDDR7 = 48 GB aggregate) |
| WARM | 0.35 ≤ H < 0.70 | DDR5 32 GB (XMP1: 5200 MT/s) |
| COLD | H < 0.35 | NVMe SSD |

Floor: `ALPHA = 0.0500` (Constant of Hope — heat never reaches 0).  
Ceiling: `H_MAX = 0.9999` (sovereignty exception — `pinned=True` nodes are never migrated).
