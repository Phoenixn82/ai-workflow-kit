// Motion lane helpers. Classic script: exposes window.M. Everything is a pure function of time.
(() => {
  const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
  const lerp = (a, b, p) => a + (b - a) * p;
  const range = (t, a, b) => clamp((t - a) / (b - a)); // 0..1 progress of t through [a, b]

  // Closed-form damped spring 0 -> 1 (k stiffness, d damping). No simulation, so seek(t) stays O(1).
  function spring(t, k = 170, d = 26) {
    if (t <= 0) return 0;
    const w0 = Math.sqrt(k), z = d / (2 * w0);
    if (z < 1) {
      const wd = w0 * Math.sqrt(1 - z * z);
      return 1 - Math.exp(-z * w0 * t) * (Math.cos(wd * t) + (z * w0 / wd) * Math.sin(wd * t));
    }
    return 1 - Math.exp(-w0 * t) * (1 + w0 * t); // z >= 1 treated as critical
  }
  // Presets: snappy = buttons/toggles/leading edges, base = cards/containers/camera,
  // heavy = big type/logo lockups, playful = mascots/stickers (visible overshoot).
  const S = { snappy: [320, 30], base: [170, 26], heavy: [90, 20], playful: [260, 14] };
  const sp = (t, preset = 'base') => spring(t, ...S[preset]);

  // A value with many targets: sum one spring per change. keys = [[time, value], ...] sorted.
  function track(t, keys, k = 170, d = 26) {
    let v = keys[0][1];
    for (let i = 1; i < keys.length; i++) v += (keys[i][1] - keys[i - 1][1]) * spring(t - keys[i][0], k, d);
    return v;
  }
  // Tab indicator that stretches: leading edge stiffer than trailing edge.
  function indicator(t, stops, width = 120) {
    const lead = track(t, stops, 320, 30), trail = track(t, stops, 140, 22);
    return { left: Math.min(lead, trail), right: Math.max(lead, trail) + width };
  }
  // Text inside a morphing box: enters after the morph starts, leaves before the next one.
  const swapAlpha = (t, tIn, tOut) => Math.min(clamp((t - tIn - 0.08) / 0.12), clamp((tOut - 0.1 - t) / 0.1));
  const loopT = (t, dur) => ((t % dur) + dur) % dur;

  // Seeded noise (mulberry32). Never Math.random: renders must be identical every run.
  function rng(seed) {
    return () => {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  // Beat grid. B = { bpm, beats, downbeats, hits } from beats.json, or analytic from bpm.
  const grid = (bpm, dur, offset = 0) => {
    const beats = [];
    for (let t = offset; t < dur; t += 60 / bpm) beats.push(+t.toFixed(3));
    return { bpm, beats, downbeats: beats.filter((_, i) => i % 4 === 0), hits: [] };
  };
  const beat = (B, i) => B.beats[Math.min(i, B.beats.length - 1)]; // time of beat i
  const bar = (B, i) => B.downbeats[Math.min(i, B.downbeats.length - 1)]; // time of bar i

  // Layout for multi-format renders: scenes use L.u (1% of the short side) and safe areas, not fixed pixels.
  function layout(W, H) {
    const u = Math.min(W, H) / 100;
    return { W, H, u, cx: W / 2, cy: H / 2, vertical: H > W, square: W === H, safe: { x: W * 0.08, y: H * 0.08 } };
  }

  // Picture authored on a nominal grid (beatLen s per beat) played against a measured track grid B:
  // seek(t) { draw(R.toNominal(t)) }, cue times R.toReal(n). Linear between beats, extrapolated past the ends.
  function retime(B, beatLen = 0.5) {
    const b = B.beats, last = b.length - 1;
    const seg = (i) => Math.max(0, Math.min(last - 1, i));
    const toReal = (n) => { const x = n / beatLen, i = seg(Math.floor(x)); return b[i] + (x - i) * (b[i + 1] - b[i]); };
    const toNominal = (t) => { let i = 0; while (i < last - 1 && b[i + 1] <= t) i++; return (i + (t - b[i]) / (b[i + 1] - b[i])) * beatLen; };
    return { toReal, toNominal };
  }

  // Load images/fonts once before any seek. Renderer awaits window.READY.
  const img = (src) => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => no(new Error('image ' + src)); i.src = src; });

  // Footage from `motion clip` (dir/00001.jpg… + meta.json). await c.frame(t) -> <img> for clip time t,
  // held on the first/last frame outside the clip. Make seek async and await every frame it draws.
  async function clip(dir) {
    const meta = await (await fetch(dir + '/meta.json')).json(), cache = new Map();
    const get = (i) => {
      if (!cache.has(i)) {
        cache.set(i, img(`${dir}/${String(i + 1).padStart(5, '0')}.jpg`).then(async (im) => (await im.decode(), im)));
        if (cache.size > 64) cache.delete(cache.keys().next().value);
      }
      return cache.get(i);
    };
    return { ...meta, frame: (t) => get(Math.max(0, Math.min(meta.frames - 1, Math.floor(t * meta.fps + 1e-6)))) };
  }

  window.M = { clamp, lerp, range, spring, sp, S, track, indicator, swapAlpha, loopT, rng, grid, beat, bar, retime, layout, img, clip };
})();
