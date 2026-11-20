// arc-sound.js — Synthesised Web Audio cues for Arc UI events
// window.ArcSound = { SCHEMA, enabled, setEnabled, toggle, play }
(function (global) {
  'use strict';

  const SCHEMA = '1.0';
  let _ctx = null, _reverb = null, _enabled = false;

  // D-major pentatonic: D4 E4 F#4 A4 B4
  const SCALE = [293.66, 329.63, 369.99, 440.00, 493.88];

  function getCtx() {
    if (!_ctx) {
      _ctx = new (window.AudioContext || window.webkitAudioContext)();
      _buildReverb();
    }
    if (_ctx.state === 'suspended') _ctx.resume();
    return _ctx;
  }

  function _buildReverb() {
    const dur = 1.5, decay = 2.0, rate = _ctx.sampleRate;
    const len = Math.floor(rate * dur);
    const buf = _ctx.createBuffer(2, len, rate);
    for (let ch = 0; ch < 2; ch++) {
      const d = buf.getChannelData(ch);
      for (let i = 0; i < len; i++) {
        d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay);
      }
    }
    _reverb = _ctx.createConvolver();
    _reverb.buffer = buf;
    _reverb.connect(_ctx.destination);
  }

  function _tone(freq, type, gain, attack, sustain, release, delay) {
    const ctx = getCtx();
    const now = ctx.currentTime + (delay || 0);
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, now);
    env.gain.setValueAtTime(0, now);
    env.gain.linearRampToValueAtTime(gain, now + attack);
    env.gain.setValueAtTime(gain, now + attack + sustain);
    env.gain.exponentialRampToValueAtTime(0.0001, now + attack + sustain + release);
    osc.connect(env);
    env.connect(ctx.destination);
    if (_reverb) {
      const wet = ctx.createGain();
      wet.gain.value = 0.18;
      env.connect(wet);
      wet.connect(_reverb);
    }
    osc.start(now);
    osc.stop(now + attack + sustain + release + 0.05);
  }

  const CUES = {
    // Reply start — single lift tone
    lift: () => { _tone(SCALE[2], 'sine', 0.12, 0.02, 0.05, 0.3); },

    // Reply done — ascending triad
    arrive: () => {
      _tone(SCALE[0], 'sine', 0.10, 0.01, 0.02, 0.25, 0.00);
      _tone(SCALE[2], 'sine', 0.08, 0.01, 0.02, 0.25, 0.08);
      _tone(SCALE[4], 'sine', 0.07, 0.01, 0.02, 0.35, 0.16);
    },

    // Stream chunk tick
    tick: () => { _tone(SCALE[1], 'triangle', 0.05, 0.004, 0.008, 0.10); },

    // Settle / save
    settle: () => {
      _tone(SCALE[3], 'sine', 0.09, 0.01, 0.04, 0.4, 0.00);
      _tone(SCALE[1], 'sine', 0.06, 0.01, 0.04, 0.4, 0.06);
    },

    // Ambient drift (idle)
    drift: () => { _tone(SCALE[0], 'sine', 0.04, 0.10, 0.25, 0.7); },

    // Touch / click feedback
    touch: () => { _tone(SCALE[2], 'triangle', 0.08, 0.005, 0.015, 0.16); },

    // Error / fault
    fault: () => {
      _tone(110, 'sawtooth', 0.10, 0.005, 0.03, 0.2, 0.00);
      _tone(100, 'sawtooth', 0.08, 0.005, 0.03, 0.2, 0.05);
    },
  };

  function play(cue) {
    if (!_enabled) return;
    try { (CUES[cue] || (() => {}))(); } catch (_) {}
  }

  function setEnabled(val) { _enabled = !!val; }
  function toggle() { _enabled = !_enabled; return _enabled; }

  global.ArcSound = {
    SCHEMA,
    get enabled() { return _enabled; },
    setEnabled,
    toggle,
    play,
  };
})(window);
