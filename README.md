<div align="center">

```
   ╔══════════════════════════════════════════╗
   ║                                          ║
   ║          ▄▄▄    ██████  ██████           ║
   ║         ██ ██   ██  ██  ██               ║
   ║         ██████  ██████  ██████           ║
   ║         ██  ██  ██  ██  ██               ║
   ║         ██  ██  ██  ██  ██████           ║
   ║                                          ║
   ║      Loci-Powered Local AI Runtime       ║
   ╚══════════════════════════════════════════╝
```

**Arc** is a local AI runtime built on the [Loci memory engine](https://github.com/arko-sanyal-loci/LOCI-MEMORY-ENGINE).  
It runs your model locally with persistent memory, dynamic GPU offloading, and a clean chat interface.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Billo Tech](https://img.shields.io/badge/Billo%20Tech-arc-6c6cff?style=flat-square)](https://billo.tech)

</div>

---

## What Arc is

Arc is the runtime layer that connects the Loci memory engine to a running language model.  
It gives a local model the same persistent memory, context compression, and failover routing that cloud models take for granted.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Arc Runtime                              │
│                                                                 │
│   Chat UI (port 7860)  ←→  FastAPI + WebSocket                 │
│          │                        │                             │
│          ▼                        ▼                             │
│   Provider Router         Loci Memory Engine                   │
│   LOCAL → NEXOS → XAI     SLP nodes · CAR · CARD · CAS        │
│          │                        │                             │
│          ▼                        ▼                             │
│   ┌──────────────────────────────────────────┐                 │
│   │           Tiered Memory                  │                 │
│   │  HOT  (H≥0.70) → VRAM  48 GB            │                 │
│   │  WARM (H≥0.35) → DDR5  32 GB (XMP1)     │                 │
│   │  COLD (H<0.35) → NVMe  SSD              │                 │
│   └──────────────────────────────────────────┘                 │
│          │                        │                             │
│   Arc B70 (Y=1)           RTX 5060 Ti (Y=0)                   │
│   32 GB GDDR6             16 GB GDDR7                          │
│   608 GB/s internal       448 GB/s internal                    │
│          └────────────────────────┘                             │
│               7.9 GB/s inter-bus (/dev/shm)                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Hardware target

| Component | Spec | Role |
|---|---|---|
| **Intel Arc B70** | 32 GB GDDR6 · 608 GB/s · 367 AI TOPS | Deep lane (Y=1) — model weights |
| **RTX 5060 Ti** | 16 GB GDDR7 · 448 GB/s · 759 AI TOPS | Fast lane (Y=0) — active inference |
| **DDR5-5200** | 32 GB · 41.6 GB/s real (XMP1 required) | Warm node buffer |
| **NVMe SSD** | PCIe 4.0+ · ~7 GB/s | Cold node persistence |
| **Inter-bus** | `/dev/shm` · 7.9 GB/s sustained | Cross-GPU channel |

> **XMP1 note**: DDR5 defaults to 4800 MT/s (JEDEC) until XMP1 is enabled.  
> BIOS path: `AI Tweaker → Ai Overclock Tuner → XMP I`  
> Verify: `sudo dmidecode -t memory | grep Speed` — should show `5200 MHz`.

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/arko-sanyal-loci/arc
cd arc

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# edit .env — add ARC_XAI_KEY and/or ARC_NEXOS_KEY

# 4. Launch
bash start.sh
# → http://localhost:7860
```

---

## Directory structure

```
arc/
├── core/
│   ├── heartbeat.py        # 140ms tick engine · /dev/shm ring · lane mask
│   ├── engine_sync.py      # Loci ↔ SQLite WAL sync (every 50 ticks)
│   ├── gpu_offload.py      # Dynamic VRAM offload Arc B70 ↔ RTX 5060 Ti
│   ├── tiered_memory.py    # 3-tier placement: VRAM / DDR5 / NVMe by heat
│   └── api_provision.py    # Provider router: LOCAL → NEXOS → XAI
│
├── app/
│   ├── server.py           # FastAPI + WebSocket server (port 7860)
│   └── static/
│       └── index.html      # Chat UI — dark theme · GPU status · MCP badges
│
├── loci/                   # Loci memory engine (copied from LOCI-MEMORY-ENGINE)
│   ├── slp_encoding.py     # SLP register (20 bytes, L1-aligned)
│   ├── car_front.py        # CAR triage: DROP · SCAR · GOAL · PLAYGROUND · STANDARD
│   ├── card_stream.py      # CARD streaming with budget tiers
│   ├── cas_router.py       # CAS affinity learning (η=0.1 / η=0.3 boost)
│   ├── dual_write.py       # 3-phase dual-write: SQLite WAL + Drive
│   ├── playground.py       # Heat decay and rerun logic
│   ├── overlap.py          # Overlap scalars (w, c, d) — tiebreakers only
│   ├── reconcile_tick.py   # Drift detection (no auto-correction)
│   ├── crystallize.py      # Cross-model agreement (THETA_AGREE=0.65)
│   ├── hybrid_compose.py   # ρ routing matrix
│   └── glassbox_ingest.py  # Gmail → Glassbox Compendium pipeline
│
├── config/
│   ├── arc_config.yaml     # Full runtime config (ring=4 MiB, tiers, keys)
│   └── antigravity.json    # Antigravity 2.0 IDE panels + tasks
│
├── tests/
│   └── bench_bus_fidelity.py  # SHM ring sizing benchmark
│
├── data/
│   └── bench_bus_fidelity.json  # Benchmark results (4 MiB recommendation)
│
├── requirements.txt
├── start.sh
└── .env.example
```

---

## Memory tiers

Arc maps every Loci SLP node to a storage tier by its heat value `H ∈ [0, 1]`:

| Tier | Heat range | Storage | Why |
|---|---|---|---|
| **VRAM** | H ≥ 0.70 | 48 GB aggregate | Active inference — sub-ms latency |
| **DDR5** | 0.35 ≤ H < 0.70 | 32 GB DDR5-5200 | Recent context — µs latency |
| **NVMe** | H < 0.35 | SSD | Cold history — ms latency, unlimited depth |

Heat increases on access and decays over time (`DECAY_GAMMA = 0.98` per tick).  
Nodes are promoted and demoted automatically every 50 heartbeat ticks.

### SLP fidelity levels

| Level | Heat | Size | Tier |
|---|---|---|---|
| `α_floor` | 0.05 | 32 B | NVMe |
| `low` | 0.15 | 76 B | NVMe |
| `warm` | 0.35 | 268 B | DDR5 |
| `active` | 0.55 | 2 KB | DDR5 |
| `hot` | 0.70 | 8 KB | VRAM |
| `burning` | 0.85 | 16 KB | VRAM |
| `sovereign` | 0.9999 | 32 KB | VRAM (pinned) |

---

## Provider routing

Arc routes completions through a priority chain:

```
LOCAL (Ollama / llama.cpp)
  ↓  if unreachable
nexos.ai  (compute offset — 200+ models, OpenAI-compatible)
  ↓  if unreachable
xAI Grok  (failsafe)
```

Health is polled every 30 seconds. Routing switches automatically.

---

## MCP compatibility

Arc exposes an MCP server at `POST /mcp`. Tools available to any MCP client:

| Tool | Description |
|---|---|
| `arc_chat` | Single-turn or streaming chat |
| `arc_memory_search` | Search Loci SQLite nodes by content |
| `arc_status` | Heartbeat tick, GPU utilisation, provider health |

Antigravity 2.0 IDE connects automatically via `config/antigravity.json`.

---

## Bus sizing

The `/dev/shm` inter-bus runs at **7.9 GB/s sustained**.  
Mixed-fidelity tick traffic is **333.2 KB / 140ms** — the ring drains in **0.494ms**.

| Ring size | Tick buffer | Drain time |
|---|---|---|
| 4 MiB ✓ | 12.3× | 0.494ms |

Benchmark: `python tests/bench_bus_fidelity.py`

---

## Part of the Loci ecosystem

```
Glassbox Compendium  ←─────────────────────────────┐
(universal knowledge)                               │
       │                                            │
       ▼                                            │
Loci Memory Engine   ←── Arc Runtime  ←── Chat UI  │
(SLP · CAR · CARD)        (this repo)              │
       │                       │                    │
       ▼                       ▼                    │
  SQLite WAL             GPU Offload ───────────────┘
  Google Drive           Tiered Memory
```

[LOCI-MEMORY-ENGINE](https://github.com/arko-sanyal-loci/LOCI-MEMORY-ENGINE) (engine + coordination bus) ·
[loci-card-papers](https://github.com/arko-sanyal-loci/loci-card-papers) (research)

---

## License

MIT © [Billo Tech](https://billo.tech) · Built by [Arko Sanyal](https://github.com/arko-sanyal-loci)

<div align="center">
<img alt="Billo Tech" src="https://raw.githubusercontent.com/arko-sanyal-loci/LOCI-MEMORY-ENGINE/main/brand/billo-tech-mark.svg" width="48">
</div>
