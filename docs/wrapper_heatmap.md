# Inference Wrapper Heat Map — Arc B70 + RTX 5060 Ti
_arc-runtime · Arko Sanyal · 2026-11-18_

## Decision

**Dual-track. Not one wrapper — two, by GPU brand.**

| GPU | Wrapper | Port | Provider enum |
|-----|---------|------|---------------|
| RTX 5060 Ti (16GB GDDR7) | **Ollama** (CUDA) | 11434 | `LOCAL` |
| Arc B70 (32GB GDDR6) | **Intel LLM-Scaler vLLM PV 1.4** | 8000 | `LOCAL_INTEL` |
| Arc B70 (future/experiment) | **SGLang XPU** | 7000 | `LOCAL_SGLang` |

---

## Heat Map

Each axis scored **HOT / WARM / MILD / COLD**.

| Wrapper | Arc B70 Speed | RTX Speed | Concurrency | Setup Friction | OpenAI-compat | Stability | Cross-GPU | VERDICT |
|---------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **Ollama** (stock) | 🧊 COLD¹ | 🔥 HOT | 🧊 COLD | 🔥 HOT (zero) | 🔥 HOT | 🔥 HOT | 🧊 COLD | RTX only |
| **IPEX-LLM Ollama fork** | 🌡️ WARM² | ⚪ n/a | ⚪ MILD | 🌡️ WARM | 🔥 HOT | ⚪ MILD³ | ⚪ MILD | Arc single-user |
| **llama.cpp SYCL direct** | 🔥 HOT⁴ | 🔥 HOT | 🧊 COLD | ⚪ MILD | 🔥 HOT | 🌡️ WARM | 🧊 COLD | Arc speed ceiling |
| **LLM-Scaler vLLM PV 1.4** | 🔥 HOT⁵ | 🌡️ WARM | 🔥 HOT | 🌡️ WARM⁶ | 🔥 HOT | 🔥 HOT⁷ | ⚪ MILD⁸ | **B70 PRODUCTION** ✅ |
| **SGLang XPU (Xe2)** | 🔥 HOT⁹ | ⚪ MILD | 🔥 HOT | ⚪ MILD¹⁰ | 🔥 HOT | ⚪ MILD¹¹ | 🔥 HOT¹² | **B70 FRONTIER** 🚀 |
| TensorRT-LLM | 🧊 COLD | 🔥 HOT | 🔥 HOT | 🧊 COLD | 🔥 HOT | 🔥 HOT | 🧊 COLD | NVIDIA-only |

---

## Footnotes / Evidence

**¹ Ollama on Arc B70:** Uses llama.cpp Vulkan backend by default — NOT the oneAPI/SYCL path.
Measured ~30-40% lower throughput than SYCL on the same Battlemage hardware.
ReBAR off in BIOS = even worse (50%+ penalty). Zero Arc-specific kernel optimization.

**² IPEX-LLM Ollama fork:** 35–50% faster than upstream SYCL on Battlemage per Intel benchmarks.
Provides an Ollama-compatible API so arc-runtime drops it in with no code change.

**³ IPEX-LLM stability:** Known heavy-load slowdown bug — prompt processing drops 250 → 50 t/s
under concurrent load. Unresolved as of May 2025. Solo-user workloads mostly fine.

**⁴ llama.cpp SYCL direct (`llama-server`):** 2.2× faster than Vulkan on B70.
10–25% faster than Ollama even on CUDA because it removes the wrapper overhead.
Benchmarks: 63 t/s on Qwen 3.6-35B Q4_K_M; 11.5 t/s on Llama-3.3-70B Q4_K_M;
977 tok/s prompt processing on 35B MoE. Same OpenAI-compat endpoint.
Downside: manual model management, no systemd integration out-of-box.

**⁵ LLM-Scaler vLLM PV 1.4 on Arc B70:** Benchmarked ~38 t/s Llama-3 8B FP16;
~18 t/s on Llama-3 70B AWQ-INT4 with two B70s (tensor parallel).
Xe2 SDPA kernel merged, dynamic batching, PagedAttention KV-cache.
INT4 path gets 25% throughput improvement from oneDNN optimizations vs PV 1.3.

**⁶ LLM-Scaler setup friction:** Docker container + Ubuntu 24.04 offline installer.
Requires oneAPI stack, Level Zero, kernel 6.17 or patched 6.6+.
Not a one-liner but Intel ships a working Docker image — `docker pull`.

**⁷ LLM-Scaler stability:** Official Intel product. PV 1.4 (May 2026) promotes Arc Pro B70
from experimental to officially listed. Ubuntu 24.04 offline installer eliminates
OS-distribution risks. The most mature Arc vLLM path.

**⁸ LLM-Scaler cross-GPU:** Mixed-vendor (B70 + RTX in same vLLM instance) is "technically
possible but treats the cards as separate single-vendor pools." Use separate endpoints
per card; tensor-parallel across two B70s works natively.

**⁹ SGLang XPU (Xe2):** Specifically optimized for Intel Arc B-Series and Arc Pro B-Series
(Xe2 architecture). Intel + RadixArk partnership announced Nov 12 2026 — upstreaming
deep optimizations. Prefill-decode disaggregation across two GPUs (ZE_AFFINITY_MASK=0/1).
XPUGraph capture for decode. Community Docker images for B60/B65/B70 already in use.

**¹⁰ SGLang XPU friction:** Source-build only currently ("Docker for XPU is under active
development"). Intel+RadixArk collaboration means Docker will ship — not yet stable as of
Nov 2026. Conda + source install is ~30 min if deps resolve.

**¹¹ SGLang stability:** Newer on Intel XPU than vLLM. Xe2 support merged but Docker not
official yet. For production today use LLM-Scaler vLLM; for best-available throughput
in 30-60 days, SGLang XPU will be the answer.

**¹² SGLang cross-GPU:** Native prefill-decode disaggregation across XPUs with NIXL
KV-transfer backend. Tested: Qwen3-0.6B P/D disaggregated across 2 XPU cards.
Can also do Arc (encoder) + NVIDIA (prefill+decode) in heterogeneous EP/D (llm-d stack).

---

## Arc B70-Specific Performance Numbers

```
Llama-3.3-70B Q4_K_M   (1× Arc Pro B70 SYCL):  11.5 t/s decode
Qwen3.6-35B Q4_K_M     (1× Arc Pro B70 SYCL):  63   t/s decode
35B MoE prompt proc.   (1× Arc Pro B70 SYCL):  977  tok/s prefill
Llama-3 8B FP16        (1× Arc Pro B70 vLLM):   38  t/s decode
Llama-3 70B AWQ-INT4   (2× Arc Pro B70 vLLM):   18  t/s decode
```

The B70's 32GB GDDR6 fits:
- Llama-3.1 70B at Q4 (fits in one card, ~38GB → tight, use Q3_K_M)
- Qwen2.5-72B at Q3_K_M (fits)
- Two × 7B/8B models simultaneously (multi-tenant)
- Full 32B at BF16 (22GB → fits with room)

---

## BIOS Requirements (do these FIRST or nothing matters)

```
1. ReBAR (Resizable BAR) → ENABLED   ← #1 cause of poor Arc performance
2. XMP I / DOCP         → ENABLED   ← DDR5-5200 rated but ships at 4800
3. Above 4G Decoding    → ENABLED   ← required for ReBAR to work
```

Verify after boot:
```bash
# ReBAR
sudo lspci -vvv | grep -A5 "Resizable BAR"

# DDR5 actual speed
sudo dmidecode -t memory | grep Speed

# Arc visible
sudo ze_info | grep "Device Name"
```

---

## Routing Decision in arc-runtime

```
Embed (nomic)              → Provider.LOCAL      (Ollama, RTX side, free tier)
Small models (≤8B)         → Provider.LOCAL      (Ollama CUDA, 16GB GDDR7)
Large models (9B–70B)      → Provider.LOCAL_INTEL (LLM-Scaler vLLM, 32GB GDDR6)
Very large (70B+ or 2×)    → Provider.LOCAL_INTEL (tensor-parallel across 2× B70 if stacked)
Fallback / API             → Provider.NEXOS → Provider.XAI
```

arc-runtime api_provision.py ports:
- `Provider.LOCAL`       → `localhost:11434/v1`  (Ollama)
- `Provider.LOCAL_INTEL` → `localhost:8000/v1`   (LLM-Scaler vLLM)
- `Provider.LOCAL_SGLANG`→ `localhost:7000/v1`   (SGLang XPU — future)

---

## Action Items

| Priority | Action | Script |
|----------|--------|--------|
| P0 | Enable ReBAR + XMP I in BIOS | Manual — BIOS |
| P0 | Run `install_drivers.sh` (oneAPI + CUDA) | `scripts/install_drivers.sh` |
| P1 | Deploy LLM-Scaler vLLM PV 1.4 on Arc B70 | `scripts/setup_intel_vllm.sh` ← NEW |
| P1 | Add `Provider.LOCAL_INTEL` to api_provision.py | `core/api_provision.py` |
| P2 | Experiment with SGLang XPU | `scripts/setup_sglang_xpu.sh` ← NEW |
| P2 | Keep Ollama for RTX 5060 Ti | `scripts/setup_ollama.sh` (existing) |
