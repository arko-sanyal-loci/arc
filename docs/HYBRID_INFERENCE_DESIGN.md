# Arc Hybrid Inference Engine

**The canonical design lives in the LOCI engine repo:**
[`LOCI-MEMORY-ENGINE/docs/hybrid-metal-remote-inference-design.md`](https://github.com/arko-sanyal-loci/LOCI-MEMORY-ENGINE/blob/arc/hybrid-inference-design/docs/hybrid-metal-remote-inference-design.md)
(PR #3, with the Arc addendum in PR #5).

In short: the RTX 5060 Ti is the always-warm compact lane, and the Arc Pro B70 is
the expand lane, with speculative decoding (draft and target on the same card).
The metered remote model reviews local drafts through a gate (critical,
failed, sampled, urgent) under a daily budget. Comms and telemetry go over the
LOCI secure coordination bus.

## What this repo implements from it

| Piece | Where | Status |
|---|---|---|
| Prompt context via LOCI LDCE (kernel / hop symbols / offload) | `core/context_compression.py` | Done; parity-tested against `loci_engine.session_continuity` |
| Context on/offload ledger (VRAM / DDR5 / NVMe) | `core/tiered_memory.py` | Fixed: migrations are now planned and committed, and decay no longer compounds |
| GPU-box driver/runtime repair (Blackwell CUDA 12.8+, B70, WSL2-aware) | `scripts/arc_repair_and_update.sh` | Done |
| Honest B70 telemetry (xpu-smi → llama-server metrics → fdinfo → `null`) | `core/gpu_offload.py` | **To do**: still returns a hardcoded 24 GB free |
| Router v2 + budget governor + oversight adapter | `core/api_provision.py` | **To do**, after the bus events land |
