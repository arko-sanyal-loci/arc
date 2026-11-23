# Glassbox Arc B70 + RTX 5060 Ti — Design Critique
## Gaps, Contradictions, and Latent Risks

---

## 1. HARD GAPS (things that will break in production)

### 1.1  Cross-GPU channel is a single point of failure with no backpressure
The design routes all Y=1↔Y=0 traffic through `/dev/shm` at ~7 GB/s.
There is no mention of:
- **Flow control** — what happens when the RTX 5060 Ti falls behind consuming?
  The Arc queue fills, then what? Drop? Block? Rotate to disk?
- **Back-pressure propagation** — if the shm ring fills, does CAR start
  dropping STANDARD items or does it stall the ingest thread?
- **Recovery on /dev/shm remount** — if the host reboots mid-write, the shm
  segment is gone. The design has rollback for Drive↔Compendium but not for
  in-flight shm messages.

**Fix needed**: Define explicit backpressure semantics and a drain timeout.
  Add a "shm overflow → disk spill" path with bounded queue.

---

### 1.2  Atomic dual-write rollback cannot be truly atomic across two systems
The design says "rollback both on half-failure" but Drive is an HTTP API and
SQLite is local. There is a window between:
  1. Drive.write_doc succeeds
  2. compendium.insert_node fails

In that window a crash leaves a Drive file with no Compendium counterpart.
`reconcile_tick` will catch this — but it **does not auto-correct**. Meanwhile
CARD activations for that category will return stale context.

**Gap**: The design has no "pending dual-write" staging table that survives
  crashes. Standard fix: write to a local WAL-staged "intent" row first;
  mark it committed only after both sides succeed. reconcile_tick promotes
  unresolved intents.

---

### 1.3  Sovereignty enforcement has no audit trail of enforcement events
When a category is tagged "private" / "private-sovereign", CAS forces
`local-70b` at affinity 1.00 and API models "never see the data."
But there is no ledger entry that records **when sovereignty was enforced**
and **which model request was blocked**.

This matters for compliance (GDPR/PDPO-Bangladesh equivalent) and for
debugging ("why did Opus never learn category X?").

**Fix**: Every sovereignty-forced routing should write a ledger entry of
  type `sovereignty_enforcement` with the blocked model ID.

---

### 1.4  Heat(P) L-component creates a failure trap
The playground failure component is:

    L = 1/(1+0.5·tries) + 0.3·(1 − e^(−failures))

This means more failures **raise** L, which raises Heat(P), which causes
the hypothesis to be **re-run more aggressively**. That is intentional per
the design ("failures RAISE Heat(P)"). But there is no ceiling on this loop.

A hypothesis that fails 20 times in a row will have L ≈ 0.3 + 0.3 ≈ 0.60,
giving Heat(P) ≈ 0.40·E + 0.20·T + 0.25·V + 0.15·0.60 = floor + 0.09.
This is acceptable IF the DECAYING state triggers after `max_tries` is
exceeded. But the design does not specify `max_tries`.

**Gap**: Define `max_tries` before Playground forces EXPIRED regardless of
  Heat(P). Without it, a systematically failing hypothesis loops forever.

---

### 1.5  CARD budget projection discards D-stream (demoted hypotheses) first
The budget projection is a greedy descent by heat. D-stream nodes are
demoted hypotheses — they are intentionally lower heat. This means they
will always be cut first under any budget pressure, making the D-stream
effectively invisible to small agents.

If the design intent is that D-stream provides uncertainty signals to agents,
budget-aware agents (Haiku ≤ 560 tok) will never see them.

**Fix**: Reserve a minimum D-stream slot (e.g. 1 node, ≤ 50 tokens) before
  greedy budget descent, so agents always receive at least one uncertainty
  signal.

---

## 2. CONTRADICTIONS

### 2.1  CAS affinity is clamped to (α, 1.0) but sovereignty forces exactly 1.0
Section CAS: "affinity ∈ (α, 1.0)" (open interval — 1.0 excluded).
Section Sovereignty: "Local 70B at affinity 1.00."

Sovereignty exception writes 1.0 into an affinity slot that is defined to
never reach 1.0. If the affinity update code enforces the open-interval clamp
globally, sovereignty 1.0 will be silently clamped to 0.9999.

**Resolution needed**: Sovereignty affinity should be stored as a sentinel
  (e.g. `SOVEREIGNTY_AFFINITY = None`) to distinguish it from a learned
  affinity, and the update() method should refuse to overwrite a sovereignty
  slot via the normal learning path.

---

### 2.2  Heat invariant says "open interval" but sovereignty exception is "forced 1.0"
Same logical contradiction applies to SLP heat encoding, not just CAS affinity.
If heat can never be exactly 1.0 (`H_MAX = 0.9999`), then a "forced heat = 1.0"
in `with_heat(1.0)` will be silently clamped to 0.9999 by `heat_to_u16`.

The design needs to decide: is 0.9999 "close enough to forced" or is there
a distinct encoding for a truly forced/sovereign state? Suggested: use a
separate ClassFlags bit (`ClassFlags.SOVEREIGN = 0x40`) rather than abusing
the heat channel.

---

### 2.3  Dual-write atomicity assumes Drive file_id is stable across rollbacks
The rollback in dual_write deletes by `file_id`. But if Drive returned a
file_id and then the subsequent Compendium write fails, a retry may create
a **second Drive file** with a new file_id before the first was cleaned up
(race if rollback itself fails or times out).

Result: two Drive files with the same commit_id, one orphaned. The reconciler
would flag DRIVE_ONLY for the orphan but cannot distinguish it from a
legitimate un-synced write.

**Fix**: Drive writes should use an idempotency key (commit_id as the Drive
  file name / metadata key) so a retry returns the existing file_id rather
  than creating a duplicate.

---

## 3. LATENT RISKS (not bugs now, problems at scale)

### 3.1  SQLite WAL is not multi-process safe for concurrent ingest workers
If two ingest workers (Y=1 and Y=2) write to the same SQLite file
simultaneously, WAL mode handles reads safely but write serialization under
high throughput will cause lock-timeout errors. The design does not specify
the SQLite `busy_timeout` or whether workers use a write-serialization queue.

**At current scale**: probably fine.
**At 10K emails/hour**: SQLite WAL will bottleneck. Plan a migration path
  to DuckDB or a lightweight PostgreSQL.

---

### 3.2  provenance_hash uses SHA256(timestamp ‖ author_id ‖ role ‖ content)
If `content` is very large (e.g. a 50KB email thread), hashing it on every
write is cheap (SHA256 is ~1 GB/s). But the hash is NOT a commitment to the
SLP register state — if the SLP heat changes (via decay), the stored hash
no longer reflects the current node state. This could mislead the reconciler
into flagging HASH_MISMATCH for legitimately updated nodes.

**Fix**: Include `heat_raw` in the provenance hash, or store a separate
  "last_heat_update_hash" that tracks post-write mutations.

---

### 3.3  Dream (DSymPrev) + Simulation (SSymPrev) run 24/7 at α floor
Running two perpetual background processes at heat α=0.05 is fine in a
dedicated 48GB pool. But the design gives no **throttle** for Dream/Simulation
when the hardware pool is under heavy ingest load.

If 10K emails arrive during business hours and both DSymPrev + SSymPrev are
running, the Arc B70 (Y=1 deep lane) may be simultaneously handling ingest,
dual-write fan-out, and Dream/Sim rollouts. No priority preemption is defined.

**Fix**: Add a `DREAM_THROTTLE_WHEN` condition (e.g. ingest queue depth > N)
  that pauses Dream/Sim until the ingest backlog clears.

---

### 3.4  overlap_w detection requires exact (model_id, gpu_id, category_id) match
Wasted-overlap detection flags when two dispatches are identical. But if the
same model is routed via two different gpu_id values (Arc vs CPU fallback for
the same category), overlap_w = 0 and the duplicate is invisible.

The scheduler has no other mechanism to detect this class of wasted compute.

**Fix**: overlap_w should additionally check model_id + category_id only
  (without gpu_id) as a "soft wasted" signal fed back to the scheduler.

---

### 3.5  η = 0.3 when affinity < 0.2 creates a "thrash zone" at the boundary
When affinity is at exactly 0.20:
  - A single ACCEPT_WITH_EDITS (+0.5) → new = 0.17·0.3 + 0.3·0.5 = 0.051 + 0.15 = 0.201
  - Affinity crosses back above 0.20 → η drops to 0.1
  - Next update (IGNORE = 0.0) → 0.9·0.201 + 0.1·0.0 = 0.181 → drops below 0.20 again
  - η jumps back to 0.3

This creates a **stability boundary** around 0.20 where affinity oscillates
and η flips back and forth. With enough IGNORE signals, a model gets stuck in
the high-η zone indefinitely.

**Fix**: Smooth the η transition — use `η = 0.1 + 0.2 * max(0, 1 - affinity/0.2)`
  instead of a hard step at 0.20.

---

## 4. MISSING SPECS (design is silent on these)

| Item | Missing spec |
|------|-------------|
| `max_tries` for Playground expiry | No ceiling; see §1.4 |
| shm ring size | Not specified; determines max buffering before backpressure |
| CAR stats endpoint `:8877` | Mentioned but not defined — schema? auth? push or pull? |
| CARD activation rate limiting | Can a single agent hammer CARD in a tight loop? |
| `θ_rerun` / `θ_hold` hysteresis | Tick period not specified — ms, seconds? |
| Ledger rotation policy | ledger.jsonl grows unbounded — when is it archived? |
| Drive folder structure | Which Drive folder? Shared drive or My Drive? ACLs? |
| Local 70B model identity | "local-70b" is never bound to a specific model path |
| Heartbeat tick 140ms | Only the period; no mention of what happens on missed heartbeat |

---

## 5. WHAT IS SOLID

- Heat encoding (open interval, uint16, L1-aligned SLP) is internally consistent.
- CAR triage decisions are mutually exclusive and exhaustive — no ambiguous overlap.
- Dual-write rollback order (ledger → node → Drive) is correct (reverse of write).
- CAS affinity learning formula is well-specified with clear η rules.
- Playground decay toward α (never zero) correctly prevents perpetual loops for most cases.
- Budget tiers (Haiku 560 / 7B 1k / Opus 5k) are sensible and enforceable.
- Hash-chain ledger for CARD activations (D2 Credit-Where-Due) is correctly specified.

---

*Critique generated: 2026-11-18*
