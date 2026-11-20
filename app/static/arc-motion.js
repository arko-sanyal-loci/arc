// arc-motion.js — Arc ambient particle field + tier stream visualisation
// window.ArcMotion = { SCHEMA, REDUCED, createField, createTierStream }
(function (global) {
  'use strict';

  const SCHEMA  = '1.0';
  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── Particle field ─────────────────────────────────────────────────────────
  function createField(canvas) {
    if (!canvas) return null;
    const ctx = canvas.getContext('2d');
    let w, h, particles = [], mouse = { x: -9999, y: -9999 }, raf = null, running = false;

    const MAX_P   = REDUCED ? 40 : 220;
    const BUOY    = 0.012;
    const DRAG    = 0.985;
    const ORBIT_R = 170;
    const CONNECT = 90;

    const TIER_COLORS = { hot: '#ffb35c', mild: '#9f6cff', cold: '#5cc8ff' };

    function resize() {
      w = canvas.width  = canvas.offsetWidth  || window.innerWidth;
      h = canvas.height = canvas.offsetHeight || window.innerHeight;
    }

    function mkParticle() {
      const roll = Math.random();
      const tier = roll < 0.12 ? 'hot' : roll < 0.42 ? 'mild' : 'cold';
      return {
        x:  Math.random() * (w || 800),
        y:  Math.random() * (h || 600),
        vx: (Math.random() - 0.5) * 0.6,
        vy: (Math.random() - 0.5) * 0.6,
        r:  0.9 + Math.random() * 1.8,
        a:  0.25 + Math.random() * 0.55,
        tier,
        color: TIER_COLORS[tier],
      };
    }

    function hex2(n) { return Math.round(n * 255).toString(16).padStart(2, '0'); }

    function step() {
      ctx.clearRect(0, 0, w, h);

      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];

        // Cursor orbit well
        const dx   = mouse.x - p.x, dy = mouse.y - p.y;
        const dist = Math.hypot(dx, dy) || 1;
        if (dist < ORBIT_R) {
          const f = (ORBIT_R - dist) / ORBIT_R * 0.0014;
          p.vx += dx * f;
          p.vy += dy * f;
        }

        // Tier buoyancy
        const buoyMult = p.tier === 'hot' ? 1.6 : p.tier === 'mild' ? 1.0 : 0.6;
        p.vy -= BUOY * buoyMult;
        p.vx *= DRAG;
        p.vy *= DRAG;
        p.x  += p.vx;
        p.y  += p.vy;

        // Wrap edges
        if (p.x < 0) p.x = w; else if (p.x > w) p.x = 0;
        if (p.y < 0) p.y = h; else if (p.y > h) p.y = 0;

        // Draw particle
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = p.color + hex2(p.a);
        ctx.fill();

        // Connect nearby pairs
        for (let j = i + 1; j < particles.length; j++) {
          const q  = particles[j];
          const ex = p.x - q.x, ey = p.y - q.y;
          const ed = Math.hypot(ex, ey);
          if (ed < CONNECT) {
            ctx.beginPath();
            ctx.moveTo(p.x, p.y);
            ctx.lineTo(q.x, q.y);
            ctx.strokeStyle = p.color + hex2((1 - ed / CONNECT) * 0.16);
            ctx.lineWidth = 0.4;
            ctx.stroke();
          }
        }
      }

      raf = requestAnimationFrame(step);
    }

    canvas.addEventListener('mousemove', e => {
      const rect = canvas.getBoundingClientRect();
      mouse.x = e.clientX - rect.left;
      mouse.y = e.clientY - rect.top;
    });
    canvas.addEventListener('mouseleave', () => { mouse.x = -9999; mouse.y = -9999; });
    window.addEventListener('resize', resize);

    function start() {
      if (running) return;
      running = true;
      resize();
      particles = Array.from({ length: MAX_P }, mkParticle);
      step();
    }

    function stop() {
      running = false;
      if (raf) { cancelAnimationFrame(raf); raf = null; }
    }

    return { start, stop };
  }

  // ── Tier stream ────────────────────────────────────────────────────────────
  // update(tiers) expects tiers shaped as /api/status → memory_tiers
  // { vram: { util_pct }, ddr5: { util_pct }, nvme: { util_pct } }
  function createTierStream(container) {
    if (!container) return null;

    const LANES = [
      { key: 'vram', label: 'VRAM', color: 'var(--tier-hot)'  },
      { key: 'ddr5', label: 'DDR5', color: 'var(--tier-mild)' },
      { key: 'nvme', label: 'NVMe', color: 'var(--tier-cold)' },
    ];

    container.innerHTML = `<div class="tier-stream">${LANES.map(l => `
      <div class="tier-lane">
        <span class="tier-label" style="color:${l.color}">${l.label}</span>
        <div class="tier-track">
          <div class="tier-fill" id="ts-fill-${l.key}" style="background:${l.color};width:0%"></div>
        </div>
        <span class="tier-pct" id="ts-pct-${l.key}">—</span>
      </div>`).join('')}
    </div>`;

    function update(tiers) {
      if (!tiers) return;
      LANES.forEach(({ key }) => {
        const t   = tiers[key] || tiers[key.toUpperCase()] || {};
        const pct = t.util_pct ?? t.utilisation_pct ?? t.used_pct ?? 0;
        const fill  = container.querySelector(`#ts-fill-${key}`);
        const label = container.querySelector(`#ts-pct-${key}`);
        if (fill)  fill.style.width = Math.min(pct, 100) + '%';
        if (label) label.textContent = pct.toFixed(0) + '%';
      });
    }

    return { update };
  }

  global.ArcMotion = { SCHEMA, REDUCED, createField, createTierStream };
})(window);
