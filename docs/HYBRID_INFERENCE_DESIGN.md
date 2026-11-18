# Arc Hybrid Inference Engine: Design and Plan

*Draft 2, 2026-11-18. Status: proposal for Arko's review.*

> **Reconciled with prior verified work.** Draft 1 proposed a cross-vendor
> draft (RTX) / target (B70) split in one process. That contradicts measured
> findings in `loci-memory-engine-1/docs/dual-gpu-coding-intelligence-blueprint.md`
> (§4: one process with CUDA and SYCL is ruled out on this box; the
> `SYCL_UR_USE_LEVEL_ZERO_V2=0` fix is per-process; Level Zero Sysman
> free-memory queries fail here). Draft 2 follows the blueprint. A parallel
> draft with the same core idea exists on `LOCI-MEMORY-ENGINE` branch
> `arc/hybrid-inference-design` (`docs/hybrid-metal-remote-inference-design.md`);
> section 10 lists the differences so the two can be merged into one.

## 1. What we are building

Arc becomes a **hybrid bare-metal + remote-node inference engine**:

- **Local first.** The two GPUs in the workstation do almost all of the work.
- **Remote is expensive, so it is used sparingly** and only in two shapes:
  speculative (fire early, cancel if not needed) and parallel oversight (a
  second opinion that runs beside the local answer, never in front of it).
- **Telemetry decides.** Every remote call is justified by a signal the local
  engine measured (confidence, queue depth, VRAM pressure, a risk flag), and is
  charged against a budget.
- **One bus standard.** Remote-node traffic uses the same signed envelope
  and security rules as the LOCI coordination bus
  (`LOCI-MEMORY-ENGINE/coordination_bus/bus.py`, `architecture/SECURE-BUS.md`).
  The local lanes keep the 140 ms heartbeat and the `/dev/shm` ring.

## 2. Hardware lanes

| Lane | Device | Memory | Stack | Role in this design |
|---|---|---|---|---|
| Y=0 | RTX 5060 Ti (Blackwell, sm_120) | 16 GB GDDR7, 448 GB/s | CUDA 12.8+ | Draft model, embeddings, small fast models, reranker |
| Y=1 | Intel Arc Pro B70 (Sparkle, BMG-G31) | 32 GB GDDR6 ECC, 608 GB/s | xe driver + Level Zero / SYCL | Target model (the "real" Arc), long context KV cache |
| Y=2 | CPU + DDR5 | 32 GB | host | Orchestrator, telemetry, bus, WARM tier |
| R | Remote nodes (nexos.ai, xAI) | n/a | HTTPS, OpenAI-compatible | Oversight and escalation only |

### The key physical constraint

The two GPUs are from different vendors. There is no NVLink and no practical
PCIe peer-to-peer path between a CUDA device and a Level Zero device, so any
data that crosses between them goes through host memory. That makes
**tensor-level splitting across the two cards (moving layers or activations
every token) the wrong abstraction**. It is also what `core/gpu_offload.py`
currently pretends to do: it only moves layer *names* between two lists.

The right split is at the **token level**: the lanes exchange token IDs and
log-probabilities, which are a few bytes per step. Between the lanes, only requests and
results cross (section 3), which fits easily inside the existing 4 MiB ring
and 140 ms heartbeat.

## 3. Local engine: two independent lanes, speculation inside the B70

```
  Y=0 RTX 5060 Ti  (compact lane, always warm)     Y=1 Arc Pro B70  (expand lane)
  ─ completions, routing, embeddings,              ─ target model, e.g. 30B-A3B MoE
    reranking, local self-check of drafts          ─ + small same-tokenizer draft model
                                                     (llama-server --model-draft)
           │                                                   │
           └──────── separate processes, each its own backend ─┘
                     only requests/results cross the bus
```

- **Two lanes, two processes.** CUDA on the RTX, SYCL on the B70, each in its
  own server. This isolates the B70's SYCL quirk
  (`SYCL_UR_USE_LEVEL_ZERO_V2=0`, mandatory for the expand-lane server) and
  keeps the compact lane warm while the expand lane is busy. This matches the
  blueprint's verified design.
- **Speculative decoding inside the B70.** Draft and target share the card and
  the process (`llama-server --model-draft`, explicit `--device SYCL0`). 32 GB
  holds both. `gpu_governor.py` shows a `loci-arc-35b` (qwen3.6-35b-a3b MoE) on
  llama.cpp SYCL already on this card; the blueprint's candidate is
  `qwen3-coder:30b` (30B-A3B). The draft must share the target's tokenizer.
- **The RTX checks, it does not draft.** It scores expand-lane output (tests,
  diff applies, confidence from token log-probs) before anything reaches the
  oversight gate. That is cheap work that suits an always-warm small model.
- **Cross-vendor drafting is a later experiment only.** If ever tried, use two
  processes with a shared-memory token channel or llama.cpp's RPC backend
  (token IDs are bytes per step), never one binary with both backends.
- **Measured, not assumed.** Record acceptance rate and tokens/s for target
  alone versus target + draft. If acceptance falls below a threshold (start at
  0.5) speculation turns off for that request type.

The RTX also keeps embeddings (nomic-embed via Ollama/CUDA) so retrieval never
competes with the target model for B70 memory.

## 4. Remote nodes: when and how they are used

Remote is never on the critical path of a normal answer. Three modes, cheapest first:

### 4.1 Parallel oversight (default remote mode)
The local answer streams to Arko immediately. In parallel, *only when a
trigger fires*, a remote model receives a **compact digest** (question, the
local answer, the IDs and hashes of the LOCI context used, not the full
context) and returns a short verdict: agree, minor fix, or disagree with
reason. A disagreement shows as a flag on the answer and is written to LOCI
as a memory event, so the system learns where the local model is weak.

Triggers (all measured locally):
- low confidence: mean token log-prob or entropy over the answer past a threshold;
- the ethics gate (`core/ethics_gate.py`) flags the request;
- the request class is marked high-stakes (code that will be pushed, money, external messages);
- Arko asks for a second opinion;
- random sampling at a low rate (start at 2 percent) to keep measuring local quality.

### 4.2 Speculative escalation (hedged requests)
For requests the router predicts the local model will struggle with (very long
context, a class with a poor oversight record), the remote request starts *at
the same time* as the local one, with a small `max_tokens` first. If the local
answer finishes with good confidence first, the remote request is cancelled
and costs only what it streamed. If the local model is struggling, the remote
answer is already on its way.

### 4.3 Capacity escalation
When both local lanes are saturated (queue depth or VRAM telemetry over the
limit for N heartbeats), batch or background work goes to nexos.ai. Interactive
work waits for local first, unless Arko marks it urgent.

### 4.4 Budget governor
- Daily and monthly spend caps, a per-request cap, and a per-mode cap.
  Defaults to propose: oversight ≤ 20 percent of the daily cap, escalation the rest.
- Cost is computed from the provider's returned `usage` fields and a price table
  in config; it is never estimated from guesses.
- When a cap is hit, remote modes switch off and the UI says so. Local keeps working.
- Ledger lives in `data/arc.db` and is mirrored to LOCI as `memory.summary` events.

Provider order stays as today (nexos.ai, then xAI as failsafe).

## 5. Telemetry

One `TelemetrySnapshot` per heartbeat sync (about 7 s) and on demand:

| Signal | Source | If unavailable |
|---|---|---|
| RTX VRAM used/free, temp, power | NVML (`nvidia-smi` / pynvml) | `null` |
| B70 VRAM used/free | `xpu-smi`; else `llama-server /metrics`; else per-process `drm-resident-vram0` in fdinfo. Level Zero Sysman is tried last: its free-memory query is confirmed to fail on this box | `null`, never a placeholder |
| B70 temp, power | Sysman / `xpu-smi` | `null` |
| tokens/s, TTFT, queue depth per lane | engine metrics endpoints | `null` |
| speculative acceptance rate | local engine | `null` |
| answer confidence | token log-probs from the target | `null` (then oversight trigger is off, not on) |
| remote spend | budget ledger | always present |

Rule: **unknown is reported as unknown.** The current `_intel_gpu_free_mb()`
reads a sysfs file that does not exist and then returns a hardcoded 24 GB free.
That fallback goes away. Hardware totals (32 GB B70, 16 GB RTX) stay as known
spec constants; only *live usage* can be unknown.

## 6. Communication: one standard, two planes

**Control plane: the LOCI secure bus.** Every remote-node job and result is an
`EventEnvelope`, signed with HMAC-SHA256, checked for expiry, sequence and
idempotency, with payloads that can never carry commands, credentials or tool
grants. This is the same standard as the coordination bus, so a remote node
is just another registered agent with a narrow policy.

- New schema `loci.infer.v1` alongside `loci.task.v1`, with events
  `infer.oversight.requested`, `infer.oversight.verdict`,
  `infer.escalation.started`, `infer.escalation.cancelled`, `infer.budget.state`,
  `infer.telemetry`.
- The bus's 16 KB payload cap is a feature here: it forces oversight to use
  digests and hashes rather than shipping whole contexts.
- Remote transport follows SECURE-BUS.md: mTLS or short-lived credentials,
  default-deny authorization. API keys stay in env vars, never in payloads
  (the bus already rejects `api_key`, `token`, `secret` fields).
- Change needed in `bus.py`: today `SCHEMA` and `EVENTS` are single frozen
  constants. Make the bus accept a registered set of schemas, each with its
  own event list. That is a change to the canonical repo, so it goes through
  review there.

**Data plane: local heartbeat and shared memory.** The 140 ms heartbeat, lane
masks and `/dev/shm` ring stay as they are for lane-to-lane traffic on the
box (token IDs for speculation, telemetry snapshots). Nothing on this plane
leaves the machine.

## 6b. Context: LOCI LDCE compression (implemented)

What goes into the prompt now follows the LOCI engine's LDCE
(`core/context_compression.py`, parity-tested against
`loci_engine.session_continuity.compute_ldce_tiers`): kernel items in full,
hop items as short symbols with `arc_memory_expand` for the full text,
offload behind `arc_memory_search`. Kernel items are pinned in Arc's VRAM
placement tier because they form the stable prompt prefix, which the
expand-lane server keeps in its KV prefix cache. Remote oversight digests
reuse the same symbols, so the remote sees IDs and gists, not the memory store.

## 7. Defects found while reading (fix as part of this work)

1. `core/gpu_offload.py`: B70 live VRAM read is wrong (nonexistent
   `mem_busy` file, hardcoded fallback). Replace with the sources in section 5.
2. `core/gpu_offload.py`: cross-vendor layer shuffling does not move any
   weights and cannot work as designed (section 2). Replace with lane
   placement at model level.
3. `scripts/install_drivers.sh`: calls the RTX 5060 Ti "Ada Lovelace"; it is
   Blackwell (sm_120). It installs CUDA **12.4**, which does not support
   Blackwell (12.8 is the minimum), and it falls back to driver 570 when
   Blackwell needs the *open* kernel modules (`nvidia-driver-XXX-open`).
4. `scripts/install_drivers.sh`: installs `intel-level-zero-gpu` *and*
   `libze-intel-gpu1` from the legacy Intel repo. These two packages ship the
   same files in newer releases, which is a common source of
   `dpkg ... returned an error code (1)`. It also asks for
   `firmware-linux-nonfree` (a Debian name, not Ubuntu), and appends to
   `/etc/profile.d/oneapi.sh` on every run.
5. `gpu_governor.py` (LOCI-MEMORY-ENGINE) labels the B70 entry
   `intel_arc_b580_pro` and uses a 330 W TDP. The Sparkle B70 board power is
   275 W (230 W on Intel's reference card). The 5 percent safety margin should
   be computed from 275 W.

## 8. Phased plan

**Phase 0: workstation health (this week).** Repair dpkg, bring drivers,
firmware and runtimes current (`scripts/arc_repair_and_update.sh`), verify both
GPUs with `arc-verify-gpus`. Nothing below works on a half-configured box.

**Phase 1: honest telemetry.** `core/telemetry.py` with NVML + Level Zero
Sysman / `xpu-smi` readers, `null` for unknowns, published on the heartbeat
and `/api/status`. Remove the hardcoded fallbacks. Tests use fake readers.

**Phase 2: real local serving.** Pick and stand up the target engine on the
B70 and the draft/embedding engine on the RTX. Turn on speculative
decoding on the B70 (section 3). Record tokens/s and acceptance rate for:
target alone, target + draft. Keep whichever wins, with the numbers in
this doc.

**Phase 3: router v2.** Replace `_pick_provider` health-only fallback with a
policy that reads telemetry and the request class. Add the budget ledger and
caps. Remote is off by default until Phase 4.

**Phase 4: remote oversight and hedging over the bus.** `loci.infer.v1`
schema in the canonical bus, a remote-node adapter that signs and verifies
envelopes, oversight digests, hedged requests with cancellation, verdicts
written to LOCI.

**Phase 5: tuning.** Use the oversight disagreement log and spend ledger to
tune thresholds, sampling rate and which request classes go local-only.

Each phase lands as its own pull request in `arc` (and, for the bus schema,
in `LOCI-MEMORY-ENGINE`), with tests, after Arko approves.

## 9. Open questions for Arko

1. Target model on the B70: keep qwen3.6-35b-a3b, or something else?
   The draft model must share its tokenizer.
2. Monthly remote budget, and the daily cap.
3. Which request classes count as high-stakes (always get oversight)?
4. Should oversight ever *block* an answer, or only flag it? Proposal: flag only.
5. Which remote is the overseer: a nexos.ai route (which model?) or xAI?

6. Is the GPU box Windows + WSL2 (as in the September blueprint) or native
   Linux now? `scripts/arc_repair_and_update.sh` handles both, but telemetry
   tooling and the bus transport differ.

## 10. Differences from the parallel draft (`arc/hybrid-inference-design`)

Both drafts agree: local does the work; the remote reviews drafts rather than
generating; oversight is gated (critical, failed, sampled, urgent) under a
daily budget with step-down modes; verdicts go back over the LOCI secure bus
into memory; telemetry is advisory and never grants authority.

| Topic | This doc | Parallel draft | Proposal |
|---|---|---|---|
| Event names | `infer.*` under a new `loci.infer.v1` schema | `infer.*`, `oversight.*`, `node.telemetry`, `budget.state` | Use the parallel draft's six names, inside a separate `loci.infer.v1` schema so `loci.task.v1` stays frozen |
| Oversight sample rate | 2 % | 10 % | Start at 10 % for the first week, then tune from data |
| Hedged remote requests | Section 4.2, opt-in by predicted difficulty | "Parallel race", `urgent` only | Keep `urgent`-only until the budget ledger has a week of data |
| Where the code lives | `arc` (`core/`) | `rag/` in the LOCI repo | Arc runtime in `arc`; bus schema in `LOCI-MEMORY-ENGINE` |
| Context to the remote | digest + hashes | task summary + draft + test output | Same thing; adopt the parallel draft's wording |

Once Arko picks, fold both into one doc in `LOCI-MEMORY-ENGINE/docs/` and
leave a pointer here.
