<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/arko-sanyal-loci/LOCI-MEMORY-ENGINE/main/brand/billo-tech-logo-dark.svg">
  <img alt="Billo Tech" src="https://raw.githubusercontent.com/arko-sanyal-loci/LOCI-MEMORY-ENGINE/main/brand/billo-tech-logo-light.svg" width="560">
</picture>

### Memory that behaves like a mind. Context that never forgets what matters.

</div>

---

**Billo Tech** builds **LOCI**, a heat-tiered, goal-conditioned memory architecture for AI agents,
and **Arc**, the local-first intelligence runtime that puts it to work. LOCI's modules connect under
**The Glassbox** — a transparent, append-only knowledge compendium that any agent or model can
learn from and contribute to.

Arc is both a runtime and a co-author: it runs your models on your own hardware, holds your context
in tiered memory across VRAM, RAM and NVMe, and over time builds the Glassbox alongside you.

## Canonical repositories

| | Repository | What it is |
|---|---|---|
| 🧠 | **[LOCI Memory Engine](https://github.com/arko-sanyal-loci/LOCI-MEMORY-ENGINE)** | Heat-decaying memory tiers, CARD, Glassbox ledger, ethics gate, inter-agent coordination bus |
| ⚡ | **[Arc](https://github.com/arko-sanyal-loci/arc)** | Local AI runtime: RTX 5060 Ti + Intel Arc B70 Pro, LOCI memory, tiered offload, gradient model router, chat interface |
| 📄 | **[LOCI / CARD papers](https://github.com/arko-sanyal-loci/loci-card-papers)** | Nine papers — eight on Technical Disclosure Commons (CC BY 4.0), one preprint |

## Architecture

```
                 The Glassbox  (shared, transparent knowledge compendium)
                       ▲
                       │ learn · recall · verify
   Agents & models ──► LOCI Memory Engine ◄── Arc runtime (local GPUs)
                       │
            HOT (VRAM) · MILD (RAM) · COLD (NVMe) — nothing ever deleted
```

## Principles

- **Local first.** Your memory runs on your hardware.
- **Nothing is deleted.** Facts cool; they are never discarded.
- **Trust through transparency.** Every learning is hash-chained and readable by every agent.

---

<div align="center">
<sub>Founded by <a href="https://github.com/arko-sanyal-loci">Arko Sanyal</a> · <a href="https://billo.tech">billo.tech</a> · Dhaka, Bangladesh</sub>
</div>
