# Arc Runtime — Code Review Pass
*Performed: 2026-11-18 | Session: claude-sonnet-4-6*

---

## Summary

A systematic correctness review of all six core runtime files.  
**9 bugs found, 9 fixes applied.**  
All files pass `python -m py_compile` with no errors or warnings.

---

## Bugs Fixed

### 1 · `core/heartbeat.py` — SHMRing ring-position arithmetic

**Root cause:** `write_tick()` computed the byte offset as:
```python
pos = (self._write_pos * TICK_RECORD_SIZE) % (self._size - TICK_RECORD_SIZE)
```
Subtracting one record width from the effective ring capacity causes writes to
wrap one record too early, producing misaligned writes for all large `write_pos`
values (every record past the penultimate slot).

**Fix:** Capacity-modulo arithmetic:
```python
capacity = self._size // TICK_RECORD_SIZE
pos      = (self._write_pos % capacity) * TICK_RECORD_SIZE
```
With `SHM_RING_SIZE = 4096` bytes and `TICK_RECORD_SIZE = 12` bytes, capacity =
341 records; every slot in `[0, 340]` is now reachable and correctly aligned.

---

### 2 · `core/tiered_memory.py` — Deadlock via non-reentrant Lock

**Root cause:** `threading.Lock` is not reentrant. `place()` and `update_heat()`
acquire `self._lock`, then call `_publish_state()`, which also tries to acquire
`self._lock` on the same thread → permanent deadlock on the second acquire.

**Fix:** Replace with `threading.RLock()` (reentrant; counts acquisitions per
thread):
```python
self._lock = threading.RLock()
```

---

### 3 · `core/engine_sync.py` — `duration_ms` always stored as `0.0`

**Root cause:** The `INSERT INTO engine_syncs` row was written *before*
`state.sync_duration_ms` was calculated. Every row stored `0.0`.

**Fix:** Restructured the try/except so duration is computed first:
```python
state.sync_duration_ms = (time.monotonic() - t0) * 1000
# INSERT comes after, using the real value
```

---

### 4 · `core/gpu_offload.py` — SHMStager `mkdir` on a pre-allocated file

**Root cause:** `start.sh` pre-allocates `/dev/shm/arc_offload_ring` as a 4 MiB
*file* for the DMA ring. `SHMStager.__init__` called `Path(SHM_OFFLOAD_PATH).mkdir()`
on that exact path, hitting "Not a directory" on every cold start.

**Fix:** Separated the two concerns into two distinct constants:
```python
SHM_OFFLOAD_RING  = "/dev/shm/arc_offload_ring"   # raw 4 MiB ring file (DMA)
SHM_OFFLOAD_PATH  = "/dev/shm/arc_offload_stage"  # SHMStager blob directory
```

---

### 5 · `core/api_provision.py` — Env-var name mismatch

**Root cause:** `APIKeys.from_env()` read `NEXOS_API_KEY` / `XAI_API_KEY`, but
the documented config template exports `ARC_NEXOS_KEY` / `ARC_XAI_KEY`. Keys
set via the documented method were silently ignored; every request fell through
to the failsafe route.

**Fix:** Primary read of `ARC_*_KEY` with fallback to legacy names:
```python
nexos = os.environ.get("ARC_NEXOS_KEY") or os.environ.get("NEXOS_API_KEY")
xai   = os.environ.get("ARC_XAI_KEY")   or os.environ.get("XAI_API_KEY")
```

---

### 6 · `core/api_provision.py` — Opaque `AssertionError` in `complete()`

**Root cause:** `assert self._session` raises a bare `AssertionError` (no
message) when `start()` has not been called, making diagnosis difficult.

**Fix:** Explicit `RuntimeError` with a descriptive message in both `complete()`
and `stream()`:
```python
if self._session is None:
    raise RuntimeError(
        "APIProvisioner.start() must be called before complete()"
    )
```

---

### 7 · `app/server.py` — Deprecated FastAPI `on_event` lifecycle hooks

**Root cause:** `@app.on_event("startup")` / `@app.on_event("shutdown")` have
been deprecated since FastAPI 0.95; they generate `DeprecationWarning` on every
worker restart and are scheduled for removal.

**Fix:** Replaced with the `@asynccontextmanager` lifespan pattern:
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    yield
    # shutdown

app = FastAPI(..., lifespan=lifespan)
```

---

### 8 · `app/server.py` — MCP endpoint body deserialization

**Root cause:** `mcp_endpoint(request: dict)` — FastAPI treats a bare `dict`
annotation as a *query-parameter* model, not a request body. All MCP POST calls
failed with `422 Unprocessable Entity`.

**Fix:**
```python
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body   = await request.json()
    method = body.get("method", "")
    tool   = body.get("params", {}).get("name", "")
    args   = body.get("params", {}).get("arguments", {})
```

---

### 9 · `app/server.py` — Inline stdlib imports inside request handler

**Root cause:** `import hashlib, uuid` were placed inside the `memory_ingest`
handler body, executed on every ingest request.

**Fix:** Moved to module-level imports alongside the other stdlib imports.

---

## New Feature: TieredMemoryManager Integration

`TieredMemoryManager` is now wired into the heartbeat tick loop in `server.py`:

```python
tier_manager = TieredMemoryManager()

async def _tier_sync(tick_id: int) -> None:
    if tier_manager is not None:
        migration = plan_migrations(tier_manager)
        if migration.promotions or migration.demotions:
            print(f"[tier_sync] tick={tick_id}  promote={...}  demote={...}")

heartbeat.on_sync(_tier_sync)   # fires every 50 ticks ≈ 7 s
```

Tier utilisation is surfaced via `/api/status` → `memory_tiers` key (reads
`/dev/shm/arc_tier_state.json`; zero-copy, falls back to `{}` on miss).

---

## Files Modified

| File | Bugs Fixed | LoC Changed |
|---|---|---|
| `core/heartbeat.py`    | 1 (ring arithmetic)       | ~4  |
| `core/tiered_memory.py`| 1 (deadlock)              | ~3  |
| `core/engine_sync.py`  | 1 (duration timing)       | ~8  |
| `core/gpu_offload.py`  | 1 (SHM path conflict)     | ~6  |
| `core/api_provision.py`| 2 (env vars + asserts)    | ~12 |
| `app/server.py`        | 3 (lifecycle, MCP, import) + TieredMemory wiring + tier stats | ~80 |

All 6 files: `python -m py_compile` → **0 errors**.
