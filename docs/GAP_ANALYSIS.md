# Arc Runtime — Gap Analysis
**Author:** Claude Sonnet 4.6 (Billo Tech session, 2026-11-18)  
**Scope:** `arko-sanyal-loci` org · arc-runtime vs LOCI-MEMORY-ENGINE vs LOCI-Coordinate-BUS  
**Status:** Authoritative first pass — all repos inspected at HEAD

---

## Repo Inventory (arko-sanyal-loci)

| Repo | Status | Contains |
|------|--------|----------|
| `arc` | ✅ Active | Arc runtime — this repo |
| `LOCI-MEMORY-ENGINE` | ✅ Active | Full Loci engine — 180+ files, 32 agents |
| `LOCI-Coordinate-BUS` | ✅ Active | Coordination MCP bus |
| `loci-card-papers` | ✅ Active | 9 canonical TD-commons papers (PDFs) |
| `LOCI-Card` | ⚠️ Archived | Duplicate of loci-card-papers |
| `LOCI-CARD-PAPER` | ⚠️ Archived | Duplicate of loci-card-papers |
| `loci-memory-engine-1` | Fork | Fork of LOCI-MEMORY-ENGINE |
| `loci-coordination-bus` | Fork | Fork of LOCI-Coordinate-BUS |
| `docs` | Fork | — |

**arko-sanyal2 / arkosanyal:** No public repos found via GitHub search or session API.
Confirm those handles with Arko — they may be private org accounts or use different spellings.

---

## TD Commons Papers (loci-card-papers — 9 papers)

Located at `arc-runtime/arko-sanyal-loci/LOCI-CARD-PAPER/papers/` (also LOCI-Card — identical):

| # | Title | Key Concept |
|---|-------|-------------|
| 01 | Forecasting Affective State (LOCI r+e) | Predictive emotional valence |
| 02 | Reflection on Action (LOCI e+r) | Post-hoc action valence scoring |
| 03 | Separate Fast Decaying Memory Spectrum | Multi-tau memory classes |
| 04 | Category Adaptive Recall Depth (CARD) | Budget-tiered inference depth |
| 05 | LOCI Heat Decaying Goal Conditioned Memory Architecture | **Core heat decay law** |
| 06 | Focused Retention LOCI | Sovereignty lock / pinned nodes |
| 07 | LOCI Infinity Memory Middleware | Session bridging / LDCE |
| 08 | LOCI Persistent AI Memory Engine | Full system architecture |
| 09 | Thermal Reasoning Substrate | GPU-mapped thermodynamic routing |

`agent_04_tdcommons_70d_paper_extractor` (in LOCI-MEMORY-ENGINE) is designed to
extract fresh 70-day TD-Commons papers automatically — its ledger is empty; needs
to be run to bring in any papers published after the current 9.

---

## Gap Analysis by Requested Module

### 1. Compression
**Status: PARTIAL — no context compression module**

**What exists:**
- `slp_arena.py` (arc-runtime): 20-byte `SymLinkPrevNode` binary struct with `zlib` CRC.
  This is binary *packing*, not semantic compression.
- `agent_15_miller_boot_compressor` (LOCI-MEMORY-ENGINE agents): A planned compression
  agent with an empty ledger — it was designed, never run.
- `unified_core.py` has `DynamicTokenCoupledSnippetEngine` (B12/C3 — Miller's Law 7±2
  kernel window) which limits context tokens, but this is injection-budget gating,
  not compression.

**What is missing from arc-runtime:**
- A `core/context_compressor.py` module that takes a node's full context and produces
  a shorter summary when heat drops below the warm tier boundary.
- Lossless binary packing already exists (SLP nodes). Lossy semantic compression
  (summarization when demoting VRAM→DDR5→NVMe) does not exist anywhere.

**Suggested path:** Implement `core/context_compressor.py` with two levels:
  - **Warm compress** (DDR5 demotion): truncate to median token count, keep
    provenance hash intact.
  - **Cold compress** (NVMe archival): call a cheap local model (Qwen-1.8B) to
    summarize to ≤128 tokens; store compressed + original hash.

---

### 2. Hyperbolic Heat
**Status: FULLY IMPLEMENTED in LOCI-MEMORY-ENGINE — NOT PORTED to arc-runtime**

**What exists (LOCI-MEMORY-ENGINE / `loci_engine/heat.py`):**

```python
# A1 — Evidence Accumulation (HYPERBOLIC decay denominator)
E_k(t) = Σ  r_j · w_j / (1 + (t − t_j) / τ_k)

# A2/A3 — Heat Activation (open-interval, NEVER 0, NEVER 1)
H_k(t) = α_k + (1 − α_k)(1 − exp(−β · E_k(t)))

# Simple one-shot hyperbolic scalar decay
decay(h, elapsed, tau) = h / (1 + elapsed / tau)
```

Also: `thermal_gradient.py` implements Gaussian heat conduction over cosine distance:
`conduction = exp(−d²/ (2σ²))` where d is cosine distance between embeddings.

**Memory classes with calibrated τ and α:**
| Class | τ (days) | α (floor) |
|-------|----------|-----------|
| `formative_scar` | 45 | 0.14 |
| `valued_skill` | 20 | 0.08 |
| `routine_observation` | 3 | 0.05 |

**Hardware tier boundaries (Spark sweep, Nov 2026):**
| Tier | Boundary | Maps to |
|------|----------|---------|
| HOT | H ≥ 0.800 | VRAM active working set |
| MILD | H ≥ 0.350 | Host RAM SymLinkPrev |
| COLD | H < 0.350 | NVMe archival ledger |

Note: `tiered_memory.py` in arc-runtime uses H≥0.70 for VRAM, H≥0.35 for DDR5.
The LOCI-MEMORY-ENGINE defines HOT at 0.80. The VRAM *admission threshold*
(θ_HOT) is 0.700 — these are different concepts. Recommend aligning arc-runtime
thresholds to upstream values.

**What is missing from arc-runtime:**
- `core/heat.py` — the full evidence accumulation / hyperbolic activation law.
  `tiered_memory.py` only does static threshold comparison; it has no decay over time.
- `thermal_gradient.py` — Gaussian heat conduction for neighbor node warming.
- Memory class constants (`formative_scar`, `valued_skill`, `routine_observation`).

**Suggested fix:** Copy `heat.py` from LOCI-MEMORY-ENGINE → `core/heat.py` in
arc-runtime, then have `tiered_memory.py` import `heat_to_tier()` and `decay()` from
it instead of using raw floats.

---

### 3. Ethics Principles
**Status: FULLY IMPLEMENTED in LOCI-MEMORY-ENGINE — NOT in arc-runtime**

**What exists (LOCI-MEMORY-ENGINE / `loci_engine/ethical_core.py` + `ethics.py`):**

Core implemented gates:
- `StickPrincipleValenceGate.score_vexec()` — open-interval v_exec scoring  
  `v_exec ∈ (−1, +1)`, never clamps to bound
- `vengeance_exception_gate()` — retaliation hard-floor (always DENY)
- `provenance_hash()` — `SHA256(ts ‖ author_id ‖ role_tag ‖ content)`
- `ScarMemory` / `record_scar_event()` — FLAG_SCAR mechanics
- `CompetenceRecord` / `execution_readiness()` — B4 competence gate
- `asymmetric_forgiveness()` — τ_forgive < τ_good
- `autonomy_to_judge()` — graduated autonomy via evidence accumulation
- `PromptSanitizer` — injection scan runs BEFORE valence scoring
- `DeductiveIdentityResolver` — principal confidence scoring (Arko-specific wiring)
- `HarmSeverity` enum — quantified harm scale `[0..N]`

The `ethics.py` observe-only layer wraps these for logging/scoring without blocking.

**Key design rule:** `ethics.py` is OBSERVE-ONLY by design — returns scores and
decision records. Callers wire them into actual decision paths explicitly.

**What is missing from arc-runtime:**
- No ethics gate on any `arc_chat`, `arc_memory_search`, or provider routing call.
- No `PromptSanitizer` on user input before it reaches the LLM.
- No provenance hash on stored memory items.
- No scar mechanics or trust heat decay.

**Suggested path:** Add `core/ethics_gate.py` that imports `ethics.py` from loci/
(already in `arc-runtime/loci/`) and runs at minimum:
1. `PromptSanitizer.sanitize()` on every user message
2. `provenance_hash()` on every node write to Loci
3. `execution_readiness()` check before any tool dispatch

---

### 4. Tooling (Tool-Use / Function-Calling Layer)
**Status: TOTAL GAP — does not exist anywhere in the codebase**

**What exists:**
- MCP endpoint at `/mcp` with 3 static tools: `arc_chat`, `arc_memory_search`,
  `arc_status`. These are MCP *server* tools (Arc exposes them to external agents).
- `simulation_engine.py` has counterfactual what-if probes — not tool-use.
- `aethel_council.py` — council deliberation, not tool dispatch.

**What is missing:**
- A tool-dispatch layer that lets Arc *call tools* (web search, file read, shell,
  calculator, etc.) during inference.
- Tool schema definition (JSON Schema or Pydantic) for external tools.
- OpenAI-compatible `tool_use` / `function_call` message format.
- Tool result injection back into the conversation context.
- `nexos.ai` and `xAI` both support `tools` in their chat completion API —
  Arc's `api_provision.py` doesn't pass them yet.

**Suggested path:** Add `core/tool_dispatcher.py`:
```python
class ToolDispatcher:
    registered_tools: Dict[str, ToolDef]  # name → (schema, handler)
    
    async def dispatch(self, tool_call: ToolCall) -> ToolResult
    def to_api_tools(self) -> List[Dict]  # OpenAI-compatible JSON
```
Then wire into `app/server.py` chat handler: inject tools into provider request,
parse `tool_calls` in response, dispatch, re-inject results, continue.

---

### 5. Scratchpad (Working Memory)
**Status: PARTIAL — session-level scratchpad exists; no in-process ephemeral scratch**

**What exists:**
- `session_continuity.py` (LOCI-MEMORY-ENGINE): LDCE 3-tier injection —
  - **Kernel** (H≥0.50, max 7 items) — always in context (Miller's Law)
  - **Hop** (H≥0.167, max 15) — available on 1-hop request
  - **Offload** (max 50) — cold, search-retrieval only
- Agent-level `scratch/` directories in every alpha/beta agent — file-system scratch.
- `simulation_engine.py`: `H_sim` (Reflective Heat) — temporary speculative heat
  that is quarantined from persisting (epistemic confidence locked at 0.0).
- `store.py` / `storage.py`: In-memory SQLite cache via `context2.db`.

**What is missing from arc-runtime:**
- No `ScratchPad` class — a typed, ephemeral, per-request working memory that:
  - Lives in DDR5/RAM tier (never NVMe)
  - Is cleared after each request (no cold persistence)
  - Can hold intermediate reasoning steps, tool call results, partial answers
  - Has a token budget (Miller's Law 7±2 for kernel, expandable to hop)
- The LDCE from `session_continuity.py` isn't wired into arc-runtime's chat handler.

**Suggested path:** Add `core/scratchpad.py`:
```python
@dataclass
class ScratchEntry:
    key: str
    value: Any
    tier: Literal["kernel", "hop"]   # kernel=always-in-context, hop=on-demand
    token_estimate: int
    created_at_ns: int

class RequestScratchPad:
    """Per-request ephemeral working memory. Cleared on request completion."""
    _kernel: List[ScratchEntry]   # max 7 (Miller)
    _hop: List[ScratchEntry]      # max 15, available on request
    
    def push(key, value, tier="hop") -> None
    def kernel_tokens() -> int         # for context injection
    def to_context_block() -> str      # serializes kernel tier to string
    def clear() -> None
```

---

### 6. Browser / Chrome Integration
**Status: TOTAL GAP — no browser integration exists anywhere**

**What exists:**
- `anthropic-skills:chrome-browser` skill is available in this Claude session.
- `mcp__claude-in-chrome__*` tools exist via the chrome-browser skill.
- Arc's `app/static/index.html` is a web app served on port 7860 — it runs *in* a
  browser but doesn't *control* a browser.

**What is missing:**
- No `core/browser.py` or Playwright/Puppeteer integration.
- No MCP tool in Arc that can open a URL, extract DOM content, or fill a form.
- The Loci engine has no concept of "web browsing" as an ingestion source.

**Suggested path (Chrome-first):**
1. Expose a `browser_fetch` MCP tool on Arc's `/mcp` endpoint:
   ```json
   { "name": "arc_browser_fetch",
     "description": "Fetch and extract text content from a URL",
     "inputSchema": { "url": "string", "selector": "string?" } }
   ```
2. Implement via `httpx` + `BeautifulSoup4` for simple cases;
   escalate to Playwright for JS-heavy pages.
3. Wire extracted content through `glassbox_ingest.py` → Loci store with
   source tag `{"type": "web", "url": "...", "fetched_at": "..."}`.
4. For full Chrome automation: use `mcp__claude-in-chrome__*` tools in a
   scheduled Arc agent rather than Arc driving Chrome directly.

---

## What arc-runtime Has That LOCI-MEMORY-ENGINE Does Not

| Feature | arc-runtime | Note |
|---------|-------------|------|
| 3-tier DDR5 XMP1 memory | ✅ `tiered_memory.py` | With XMP1 bios note |
| Dual-GPU offload (B70+5060Ti) | ✅ `gpu_offload.py` | Arc B70 Y=1, RTX Y=0 |
| `/dev/shm` ring (4 MiB) | ✅ `start.sh` | 140ms heartbeat tick |
| Provider routing (nexos→xAI) | ✅ `api_provision.py` | With dual-header fix |
| Clean chat UI (port 7860) | ✅ `app/static/index.html` | WebSocket streaming |
| MCP server (`/mcp`) | ✅ `app/server.py` | 3 tools |
| Antigravity 2.0 config | ✅ `config/antigravity.json` | 3-panel layout |

---

## Priority Queue (what to build next)

| Priority | Module | Effort | Value |
|----------|--------|--------|-------|
| P0 🔴 | Port `heat.py` to `core/heat.py` | 1h | Unlocks proper decay in tiered_memory |
| P0 🔴 | `core/ethics_gate.py` (inject sanitizer) | 2h | Safety + provenance on all writes |
| P1 🟡 | `core/scratchpad.py` (LDCE kernel/hop) | 3h | In-request working memory |
| P1 🟡 | `core/tool_dispatcher.py` | 4h | Tool-use via nexos/xAI API |
| P2 🟢 | `core/context_compressor.py` | 4h | Compress on DDR5/NVMe demotion |
| P2 🟢 | `core/browser.py` (httpx + BS4) | 3h | Web fetch → Loci ingest |
| P3 ⚪ | Full Chrome automation | 1d | Playwright / claude-in-chrome |

---

## arko-sanyal2 / arkosanyal Status

No public repos found under either handle via GitHub search or session API.
Options:
- Private accounts → Arko must `add_repo` explicitly for each repo.
- Different capitalization → try `Arko-Sanyal2` or `ArkaSanyal`.
- Personal account pre-org → check `github.com/arko-sanyal` (singular).

If those accounts hold older Loci iterations (e.g., early CARD drafts, an
early ethics module, compression experiments), porting them first before
re-implementing from scratch would be worth confirming.

---

*Document generated from live repo inspection. Update after each sprint.*
