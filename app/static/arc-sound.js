/* ─────────────────────────────────────────────────────────────────────────────
   arc-sound.js — Arc sound cues, synthesised live with Web Audio (no assets)
   Palette: airy sines + glass partials on a pentatonic scale, a short
   generated reverb, everything through a soft compressor. Off by default;
   the user turns it on (browsers block audio until a gesture anyway).
   Spec: docs/UI_MOTION_SOUND.md
   ───────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  const STORE_KEY = 'arc.sound.enabled';
  // D major pentatonic, two octaves from D5 — consonant whatever order cues land in
  const SCALE = [587.33, 659.25, 739.99, 880.0, 987.77, 1174.66, 1318.51, 1479.98, 1760.0];

  const SCHEMA = {
    master: 0.32,            // overall loudness (0..1)
    reverbSeconds: 1.8,
    wet: 0.22,
    tickMinGapMs: 70,        // rate limit for per-token ticks
    cues: {
      lift:   'send — a rising sine sweep, like something leaving the ground',
      arrive: 'reply starts — two glass partials, long tail',
      tick:   'token chunk — a near-silent pentatonic blip',
      settle: 'reply done — descending dyad that comes to rest',
      drift:  'tier migration — filtered air; rises for promote, falls for demote',
      touch:  'hover/press — tiny, short, very quiet',
      fault:  'error — low, detuned, short; never alarming',
    },
  };

  let ctx = null, out = null, wetBus = null, enabled = false, lastTick = 0, noiseBuf = null;

  try { enabled = localStorage.getItem(STORE_KEY) === '1'; } catch (_) {}

  function ensure() {
    if (ctx) return ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ctx = new AC();
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -18; comp.ratio.value = 3; comp.attack.value = 0.005; comp.release.value = 0.2;
    out = ctx.createGain(); out.gain.value = SCHEMA.master;
    out.connect(comp).connect(ctx.destination);

    // generated impulse response: decaying stereo noise — a soft, open room
    const len = Math.floor(ctx.sampleRate * SCHEMA.reverbSeconds);
    const ir = ctx.createBuffer(2, len, ctx.sampleRate);
    for (let c = 0; c < 2; c++) {
      const d = ir.getChannelData(c);
      for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3);
    }
    const verb = ctx.createConvolver(); verb.buffer = ir;
    wetBus = ctx.createGain(); wetBus.gain.value = SCHEMA.wet;
    wetBus.connect(verb).connect(out);

    noiseBuf = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
    const n = noiseBuf.getChannelData(0);
    for (let i = 0; i < n.length; i++) n[i] = Math.random() * 2 - 1;
    return ctx;
  }

  function live() {
    if (!enabled) return null;
    const c = ensure();
    if (c && c.state === 'suspended') c.resume();
    return c;
  }

  // one enveloped oscillator → dry + wet
  function tone({ freq, to, type = 'sine', at = 0, attack = 0.01, dur = 0.3, gain = 0.2, pan = 0 }) {
    const c = live(); if (!c) return;
    const t0 = c.currentTime + at;
    const o = c.createOscillator(); o.type = type;
    o.frequency.setValueAtTime(freq, t0);
    if (to) o.frequency.exponentialRampToValueAtTime(to, t0 + dur);
    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(gain, t0 + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    const p = c.createStereoPanner ? c.createStereoPanner() : null;
    if (p) { p.pan.value = pan; o.connect(g).connect(p); p.connect(out); p.connect(wetBus); }
    else { o.connect(g); g.connect(out); g.connect(wetBus); }
    o.start(t0); o.stop(t0 + dur + 0.05);
  }

  function air({ from, to, dur = 0.5, gain = 0.08, q = 2.5, at = 0 }) {
    const c = live(); if (!c) return;
    const t0 = c.currentTime + at;
    const src = c.createBufferSource(); src.buffer = noiseBuf; src.loop = true;
    const bp = c.createBiquadFilter(); bp.type = 'bandpass'; bp.Q.value = q;
    bp.frequency.setValueAtTime(from, t0);
    bp.frequency.exponentialRampToValueAtTime(to, t0 + dur);
    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(gain, t0 + dur * 0.35);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    src.connect(bp).connect(g); g.connect(out); g.connect(wetBus);
    src.start(t0); src.stop(t0 + dur + 0.05);
  }

  const pick = (lo, hi) => SCALE[lo + Math.floor(Math.random() * (hi - lo + 1))];

  const cues = {
    lift() {
      tone({ freq: 440, to: 880, dur: 0.28, attack: 0.03, gain: 0.12 });
      air({ from: 900, to: 4200, dur: 0.35, gain: 0.05 });
    },
    arrive() {
      tone({ freq: SCALE[3], dur: 1.2, attack: 0.005, gain: 0.10, pan: -0.15 });
      tone({ freq: SCALE[3] * 2.76, dur: 0.7, attack: 0.003, gain: 0.03, pan: 0.15 }); // glass partial
      tone({ freq: SCALE[5], at: 0.07, dur: 1.0, attack: 0.005, gain: 0.07, pan: 0.2 });
    },
    tick() {
      const now = performance.now();
      if (now - lastTick < SCHEMA.tickMinGapMs) return;
      lastTick = now;
      tone({ freq: pick(4, 8), dur: 0.06, attack: 0.002, gain: 0.012, pan: Math.random() * 0.6 - 0.3 });
    },
    settle() {
      tone({ freq: SCALE[5], dur: 0.9, attack: 0.01, gain: 0.07 });
      tone({ freq: SCALE[3], at: 0.12, dur: 1.4, attack: 0.01, gain: 0.08 });
      tone({ freq: SCALE[0], at: 0.24, dur: 1.8, attack: 0.02, gain: 0.05 });
    },
    drift(direction = 'promote', n = 1) {
      const g = Math.min(0.09, 0.03 + n * 0.006);
      if (direction === 'promote') air({ from: 500, to: 2600, dur: 0.7, gain: g });
      else air({ from: 2600, to: 420, dur: 0.8, gain: g });
    },
    touch() { tone({ freq: SCALE[7], dur: 0.05, attack: 0.002, gain: 0.015 }); },
    fault() {
      tone({ freq: 196, dur: 0.45, type: 'triangle', attack: 0.01, gain: 0.09 });
      tone({ freq: 207.6, dur: 0.45, type: 'triangle', attack: 0.01, gain: 0.06 });
    },
  };

  window.ArcSound = {
    SCHEMA,
    get enabled() { return enabled; },
    setEnabled(on) {
      enabled = !!on;
      try { localStorage.setItem(STORE_KEY, enabled ? '1' : '0'); } catch (_) {}
      if (enabled) { ensure(); cues.settle(); }
    },
    toggle() { this.setEnabled(!enabled); return enabled; },
    play(name, ...args) { const f = cues[name]; if (f && enabled) { try { f(...args); } catch (_) {} } },
  };
})();
