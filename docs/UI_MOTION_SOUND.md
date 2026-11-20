# Arc UI — motion & sound schema ("weightless")

Arc's chat UI (`app/static/`, served on port 7860) takes its visual direction
from Google Antigravity's public site and IDE: a quiet canvas, a field of small
coloured dashes that drift and gather around the cursor, and motion that reads
as *lift* rather than *fall*. That is the reference, not a copy. Arc keeps its
own indigo/violet palette, adds its own tier colours, and every number below
is Arc's own. Antigravity's site has no published motion spec or sound design,
so the sound system here is original.

One rule governs everything: **nothing in Arc falls. Things rise, orbit, and settle.**

## Files

| File | What it does |
|------|--------------|
| `app/static/arc-ui.css` | Motion tokens (easing, durations, lift, bob) and all CSS motion |
| `app/static/arc-motion.js` | `ArcMotion.SCHEMA`, the ambient particle field, the sidebar tier stream |
| `app/static/arc-sound.js` | `ArcSound.SCHEMA`, Web Audio synthesised cues (no audio files) |
| `app/static/index.html` | Wires the cues to chat and status events |

## Motion tokens

| Token | Value | Use |
|-------|-------|-----|
| `--ease-float` | `cubic-bezier(.22,1,.36,1)` | Default. Fast lift, long settle |
| `--ease-drift` | `cubic-bezier(.45,0,.25,1)` | Ambient loops (breathing, bob) |
| `--ease-buoy` | `linear()` spring, ~6% overshoot | Available for elements that should bounce once |
| `--t-touch` | 140 ms | Hover and press |
| `--t-float` | 560 ms | Element entering |
| `--t-settle` | 900 ms | Large surfaces (empty state, cards) |
| `--t-drift` | 6 s | Ambient bob period |
| `--lift` | 14 px | How far an entering element rises |
| `--bob` | 3 px | Ambient float amplitude |

## Behaviours

| Moment | Motion | Sound cue |
|--------|--------|-----------|
| Page idle | Field of dashes drifts upward (buoyancy 0.012 px/frame²). Logo bobs 3 px over 6 s | — |
| Cursor moves | Dashes within 170 px get a tangential swirl and settle on an 86 px orbit ring, then drift free | — |
| Hover / press | Buttons lift 2 px; press compresses to 94% | `touch` (−36 dB blip) on chips/cards |
| Send | Message rises in from 14 px with blur → sharp; send button "launches" (tilt + rise); 5 sparks rise from it | `lift`: 440→880 Hz sine sweep + rising air |
| Reply starts | Field energy eases from 1.0 → 2.3 (everything floats faster); bubble gets a breathing halo | `arrive`: A5 + E6 with a 2.76× glass partial, long tail |
| Each chunk | Every 3rd chunk emits a spark from the bubble top | `tick`: near-silent pentatonic blip, max one per 70 ms |
| Reply done | Field eases back to idle; 6 sparks release from the bubble; provider badge rises in | `settle`: E6 → A5 → D5, coming to rest |
| Error | Field returns to idle | `fault`: low detuned triangles (196/207.6 Hz), short |
| GPU util changes | Bar glides over 1.1 s with a single shimmer pass | — |
| Tier migration | In the sidebar tier stream, particles arc from the source lane to the destination lane, changing colour midway | `drift`: filtered air that rises for promote and falls for demote |

### Tier stream

The sidebar shows three lanes, **VRAM** (amber), **DDR5** (violet) and
**NVMe** (cyan), fed by `/api/status → memory_tiers` (the shm snapshot that
`core/tiered_memory.py` writes). Ambient motes per lane scale with
`log2(node_count)`, so 10 nodes and 10,000 nodes both read calmly. When the
next poll shows counts moved, particles travel from the lanes that lost nodes
to the lanes that gained them (up to 14 per pair). This is **net** flow
between polls: the snapshot does not record individual migrations. Once the
streaming tier work exposes per-migration events, call `tierStream.update`
with those events instead and the animation shows the real path.

## Sound

- Synthesised live with Web Audio. No asset files and no network calls.
- Every note is on D-major pentatonic, so cues overlapping in any order stay consonant.
- Chain: voices → dry + generated 1.8 s reverb (22% wet) → master gain 0.32 → soft compressor.
- **Off by default.** The speaker button in the header turns it on, and the choice persists in `localStorage` (`arc.sound.enabled`). Browsers block audio until a user gesture anyway.
- Per-token ticks are rate-limited so fast streams don't become a buzz.

## Accessibility and performance

- `prefers-reduced-motion: reduce` hides the field, removes travel and loops, and keeps fades. The tier stream still draws, but nothing flows.
- Animation loops pause while the tab is hidden and never schedule twice.
- Particle count is capped at 220 and scaled to window area. DPR is capped at 2.
- Canvases are `pointer-events: none` and `aria-hidden`, and never block input.

## Tuning

Change numbers in one place: CSS tokens in `arc-ui.css`, `ArcMotion.SCHEMA`
for the field and tier stream, and `ArcSound.SCHEMA` for loudness, reverb and
tick rate.
