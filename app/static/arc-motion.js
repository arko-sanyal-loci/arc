/* ─────────────────────────────────────────────────────────────────────────────
   arc-motion.js — Arc weightless motion layer
   Two canvases:
     ArcMotion.field   full-screen ambient particle field (negative gravity,
                       cursor orbit well, "energy" that rises while Arc thinks)
     ArcMotion.tiers   sidebar tier stream: VRAM / DDR5 / NVMe lanes, with
                       particles that travel between lanes when nodes migrate
   No dependencies. Spec: docs/UI_MOTION_SOUND.md
   ───────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const css = (name, fallback) =>
    (getComputedStyle(document.documentElement).getPropertyValue(name) || '').trim() || fallback;

  // ── Schema: the single source of truth for motion numbers ─────────────────
  const SCHEMA = {
    field: {
      density:      1 / 9000,  // particles per px²
      maxParticles: 220,
      buoyancy:     0.012,     // upward accel per frame (px/frame²) — the "antigravity"
      drag:         0.985,     // velocity retained per frame
      wander:       0.04,      // random lateral nudge
      well: { radius: 170, ring: 86, swirl: 0.32, pull: 0.012 },
      energy: { idle: 1.0, thinking: 2.3, ease: 0.04 },
    },
    spark: { life: 70, rise: 0.9, spread: 1.6 },
    tiers: { order: ['vram', 'ddr5', 'nvme'], maxFlow: 14, travelFrames: 70 },
  };

  // Deterministic-ish palette pulled from CSS tokens
  function palette() {
    return [
      css('--accent', '#6c6cff'),
      css('--tier-mild', '#9f6cff'),
      css('--tier-cold', '#5cc8ff'),
      css('--accent', '#6c6cff'),
      css('--tier-hot', '#ffb35c'),
    ];
  }

  // rAF loop that pauses while the tab is hidden and never double-schedules
  function loop(draw) {
    let pending = false;
    const tick = (t) => { pending = false; draw(t); if (!document.hidden) schedule(); };
    const schedule = () => { if (!pending) { pending = true; requestAnimationFrame(tick); } };
    document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(); });
    schedule();
  }

  function fitCanvas(canvas, w, h) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  // ── Ambient field ─────────────────────────────────────────────────────────
  function createField(canvas) {
    const S = SCHEMA.field;
    let ctx, W = 0, H = 0, parts = [], sparks = [], colors = palette();
    let energy = S.energy.idle, energyTarget = S.energy.idle;
    const mouse = { x: -9999, y: -9999, active: false };

    function spawn(randomY) {
      return {
        x: Math.random() * W,
        y: randomY ? Math.random() * H : H + 10,
        vx: (Math.random() - 0.5) * 0.3,
        vy: -Math.random() * 0.4,
        len: 3 + Math.random() * 5,
        a: 0.25 + Math.random() * 0.45,
        c: colors[(Math.random() * colors.length) | 0],
      };
    }

    function resize() {
      W = window.innerWidth; H = window.innerHeight;
      ctx = fitCanvas(canvas, W, H);
      const n = Math.min(S.maxParticles, Math.round(W * H * S.density));
      while (parts.length < n) parts.push(spawn(true));
      parts.length = n;
    }

    function step(p, e) {
      // buoyancy: everything slowly rises
      p.vy -= S.buoyancy * e;
      p.vx += (Math.random() - 0.5) * S.wander * e;
      // cursor orbit well: tangential swirl + spring toward a ring radius
      if (mouse.active) {
        const dx = p.x - mouse.x, dy = p.y - mouse.y;
        const d = Math.hypot(dx, dy);
        if (d < S.well.radius && d > 0.001) {
          const f = 1 - d / S.well.radius;
          const nx = dx / d, ny = dy / d;
          p.vx += -ny * S.well.swirl * f;           // tangent
          p.vy +=  nx * S.well.swirl * f;
          const k = (S.well.ring - d) * S.well.pull * f;
          p.vx += nx * k; p.vy += ny * k;           // settle on the ring
        }
      }
      p.vx *= S.drag; p.vy *= S.drag;
      p.x += p.vx * e; p.y += p.vy * e;
      if (p.y < -12) { Object.assign(p, spawn(false)); }
      if (p.x < -12) p.x = W + 10; else if (p.x > W + 12) p.x = -10;
    }

    function drawDash(p, alpha) {
      const sp = Math.hypot(p.vx, p.vy) || 1;
      const ux = p.vx / sp, uy = p.vy / sp;
      const L = p.len * (0.6 + Math.min(sp, 2) * 0.4);
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = p.c;
      ctx.beginPath();
      ctx.moveTo(p.x - ux * L * 0.5, p.y - uy * L * 0.5);
      ctx.lineTo(p.x + ux * L * 0.5, p.y + uy * L * 0.5);
      ctx.stroke();
    }

    function frame() {
      energy += (energyTarget - energy) * S.energy.ease;
      ctx.clearRect(0, 0, W, H);
      ctx.lineCap = 'round';
      ctx.lineWidth = 1.6;
      for (const p of parts) { step(p, energy); drawDash(p, p.a); }
      for (let i = sparks.length - 1; i >= 0; i--) {
        const s = sparks[i];
        s.vy -= 0.02; s.vx *= 0.97; s.vy *= 0.97;
        s.x += s.vx; s.y += s.vy; s.t++;
        const k = 1 - s.t / s.life;
        if (k <= 0) { sparks.splice(i, 1); continue; }
        ctx.lineWidth = 2;
        drawDash(s, k * 0.9);
        ctx.lineWidth = 1.6;
      }
      ctx.globalAlpha = 1;
    }

    window.addEventListener('resize', resize);
    window.addEventListener('pointermove', (e) => { mouse.x = e.clientX; mouse.y = e.clientY; mouse.active = true; });
    document.addEventListener('pointerleave', () => { mouse.active = false; });

    resize();
    if (!REDUCED) loop(frame);

    return {
      /** 'idle' | 'thinking' — eases the whole field's speed */
      setMood(mood) { energyTarget = S.energy[mood] ?? S.energy.idle; },
      /** Emit rising sparks from a screen point (e.g. a streaming bubble) */
      spark(x, y, n = 3, color) {
        if (REDUCED) return;
        const C = SCHEMA.spark;
        for (let i = 0; i < n; i++) {
          sparks.push({
            x: x + (Math.random() - 0.5) * 12, y,
            vx: (Math.random() - 0.5) * C.spread, vy: -C.rise - Math.random() * C.rise,
            len: 4 + Math.random() * 4, c: color || colors[(Math.random() * colors.length) | 0],
            t: 0, life: C.life * (0.6 + Math.random() * 0.6),
          });
        }
      },
      /** Spark from an element's rect (top edge, random x) */
      sparkFrom(el, n = 2, color) {
        if (!el) return;
        const r = el.getBoundingClientRect();
        this.spark(r.left + Math.random() * r.width, r.top + 4, n, color);
      },
    };
  }

  // ── Tier stream ───────────────────────────────────────────────────────────
  function createTierStream(canvas, onFlow) {
    const T = SCHEMA.tiers;
    const lanesColor = { vram: css('--tier-hot', '#ffb35c'), ddr5: css('--tier-mild', '#9f6cff'), nvme: css('--tier-cold', '#5cc8ff') };
    const label = { vram: 'VRAM', ddr5: 'DDR5', nvme: 'NVMe' };
    let ctx, W = 0, H = 0, last = null;
    let counts = { vram: 0, ddr5: 0, nvme: 0 };
    let motes = [], flows = [];

    const laneY = (tier) => {
      const i = T.order.indexOf(tier);
      return (H / 3) * (i + 0.5);
    };

    function resize() {
      const r = canvas.getBoundingClientRect();
      W = r.width || 230; H = r.height || 84;
      ctx = fitCanvas(canvas, W, H);
    }

    function syncMotes() {
      // ambient motes per lane, log-scaled to node count so 10k nodes stays calm
      const want = {};
      for (const t of T.order) want[t] = Math.min(18, Math.round(Math.log2(1 + counts[t]) * 2));
      for (const t of T.order) {
        const have = motes.filter(m => m.tier === t);
        for (let i = have.length; i < want[t]; i++) {
          motes.push({ tier: t, x: Math.random() * W, phase: Math.random() * 6.28, sp: 0.15 + Math.random() * 0.35 });
        }
        if (have.length > want[t]) {
          let drop = have.length - want[t];
          motes = motes.filter(m => !(m.tier === t && drop-- > 0));
        }
      }
    }

    function frame(time) {
      ctx.clearRect(0, 0, W, H);
      ctx.font = '9px ui-monospace, monospace';
      for (const t of T.order) {
        const y = laneY(t);
        ctx.globalAlpha = 0.18; ctx.strokeStyle = lanesColor[t]; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(40, y); ctx.lineTo(W, y); ctx.stroke();
        ctx.globalAlpha = 0.75; ctx.fillStyle = lanesColor[t];
        ctx.fillText(label[t], 0, y + 3);
        ctx.globalAlpha = 0.5;
        ctx.fillText(String(counts[t]), W - ctx.measureText(String(counts[t])).width, y - 5);
      }
      // ambient motes drift right along their lane and bob
      for (const m of motes) {
        m.x += m.sp; if (m.x > W) m.x = 40;
        if (m.x < 40) m.x = 40 + Math.random() * (W - 40);
        const y = laneY(m.tier) + Math.sin(time / 700 + m.phase) * 2.5;
        ctx.globalAlpha = 0.55; ctx.fillStyle = lanesColor[m.tier];
        ctx.beginPath(); ctx.arc(m.x, y, 1.3, 0, 6.283); ctx.fill();
      }
      // migrating particles: arc from one lane to another (promote = rise)
      for (let i = flows.length - 1; i >= 0; i--) {
        const f = flows[i]; f.t++;
        if (f.t <= 0) continue;                          // still waiting (stagger)
        const k = Math.min(1, f.t / T.travelFrames);
        const e = 1 - Math.pow(1 - k, 3);                 // ease-out cubic
        const x = f.x0 + (f.x1 - f.x0) * e;
        const y = f.y0 + (f.y1 - f.y0) * e - Math.sin(Math.PI * e) * 6;
        const c = k < 0.5 ? lanesColor[f.from] : lanesColor[f.to];
        ctx.globalAlpha = 0.95 * (1 - Math.max(0, k - 0.85) / 0.15);
        ctx.fillStyle = c;
        ctx.beginPath(); ctx.arc(x, y, 2, 0, 6.283); ctx.fill();
        if (k >= 1) flows.splice(i, 1);
      }
      ctx.globalAlpha = 1;
    }

    function launch(from, to, n) {
      for (let i = 0; i < n; i++) {
        const x0 = 40 + Math.random() * (W - 60);
        flows.push({
          from, to, t: -i * 4,                             // stagger
          x0, y0: laneY(from), x1: x0 + 20 + Math.random() * 30, y1: laneY(to),
        });
      }
    }

    window.addEventListener('resize', resize);
    resize();
    loop(frame);

    return {
      /** Feed /api/status → memory_tiers ({vram:{node_count},...}) */
      update(tiers) {
        if (!tiers) return;
        const next = {};
        for (const t of T.order) next[t] = (tiers[t] && tiers[t].node_count) || 0;
        if (last) {
          const delta = {};
          for (const t of T.order) delta[t] = next[t] - last[t];
          const sources = T.order.filter(t => delta[t] < 0);
          const sinks = T.order.filter(t => delta[t] > 0);
          for (const s of sources) for (const d of sinks) {
            const n = Math.min(T.maxFlow, -delta[s], delta[d]);
            if (n > 0 && !REDUCED) launch(s, d, n);
            if (n > 0 && onFlow) onFlow(T.order.indexOf(d) < T.order.indexOf(s) ? 'promote' : 'demote', s, d, n);
          }
        }
        last = next; counts = next; syncMotes();
      },
    };
  }

  window.ArcMotion = { SCHEMA, REDUCED, createField, createTierStream };
})();
