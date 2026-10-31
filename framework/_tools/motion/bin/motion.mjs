#!/usr/bin/env node
// motion — the motion-graphics lane. A film is a project dir whose index.html exposes
// window.FILM, window.READY and window.seek(t). This CLI renders, scores, sheets and checks it.
import { chromium } from 'playwright-core';
import http from 'node:http';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { cpus, tmpdir } from 'node:os';
import { dirname, extname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const die = (msg, code = 2) => { console.error('motion: ' + msg); process.exit(code); };
const log = (msg) => console.error(msg);

// ---------- static server (ES modules, fetch and fonts don't work over file://) ----------
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.json': 'application/json',
  '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
  '.gif': 'image/gif', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf',
  '.otf': 'font/otf', '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.mp4': 'video/mp4', '.webm': 'video/webm' };
function serve(dir, port = 0) {
  const server = http.createServer((req, res) => {
    const p = resolve(dir, '.' + decodeURIComponent(new URL(req.url, 'http://x').pathname));
    const file = p.endsWith(sep) || (existsSync(p) && statSync(p).isDirectory()) ? join(p, 'index.html') : p;
    if (!(file + sep).startsWith(resolve(dir) + sep) || !existsSync(file)) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'content-type': MIME[extname(file).toLowerCase()] || 'application/octet-stream', 'cache-control': 'no-store' });
    res.end(readFileSync(file));
  });
  return new Promise((ok) => server.listen(port, '127.0.0.1', () => ok({ url: `http://127.0.0.1:${server.address().port}/`, close: () => server.close() })));
}

// ---------- browser + film ----------
async function launch() {
  if (process.env.MOTION_CHROME) return chromium.launch({ executablePath: process.env.MOTION_CHROME });
  try { return await chromium.launch(); } // playwright's cached headless shell
  catch { return chromium.launch({ executablePath: process.env.CHROME_PATH || '/usr/bin/chromium' }); }
}
// Opens the film at its size (or --size), waits for READY, returns { page, film }.
async function openFilm(browser, url, size) {
  let page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const load = async (w, h) => {
    await page.goto(url + (w ? `?w=${w}&h=${h}` : ''), { waitUntil: 'load' });
    await page.waitForFunction(() => window.FILM && typeof window.seek === 'function', null, { timeout: 15000 })
      .catch(() => die('index.html must set window.FILM and window.seek(t)' + (errors.length ? ': ' + errors.join('; ') : ''), 1));
    await page.evaluate(() => window.READY);
    return page.evaluate(() => ({ ...window.FILM, cues: window.FILM.cues || [], tracks: window.FILM.tracks || [] }));
  };
  if (size) await page.setViewportSize({ width: size[0], height: size[1] });
  let film = await load(...(size || []));
  const [w, h] = size || [film.width, film.height];
  if (!w || !h || w % 2 || h % 2) die(`film size must be even numbers, got ${w}x${h}`);
  if (!size) { await page.setViewportSize({ width: w, height: h }); film = await load(w, h); }
  if (errors.length) die('page errors: ' + errors.join('; '), 1);
  return { page, film: { ...film, width: w, height: h } };
}
const shot = async (page, t, type = 'png') => {
  await page.evaluate((t) => window.seek(t), t);
  return page.screenshot(type === 'png' ? { type } : { type, quality: 85 });
};
const still = async (page, t, label) => ({ png: await shot(page, t, 'jpeg'), type: 'jpeg', label }); // sheet tile

// ---------- ffmpeg ----------
function ffmpeg(args, opts = {}) {
  const ff = spawn('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', ...args], { stdio: [opts.stdin ? 'pipe' : 'ignore', 'inherit', 'inherit'] });
  ff.done = new Promise((ok, no) => ff.on('close', (c) => (c ? no(new Error('ffmpeg exited ' + c)) : ok())));
  return ff;
}
const write = (ff, buf) => (ff.stdin.write(buf) ? null : new Promise((r) => ff.stdin.once('drain', r)));

// ---------- synth: cues -> mono 48 kHz WAV ----------
// cue: { t, type, f?, len?, gain? }. Music voices + UI SFX, all deterministic (seeded noise).
const SR = 48000;
const VOICES = {
  click: { len: 0.05, fn: (x, n) => Math.sin(2 * Math.PI * 1800 * x) * Math.exp(-x * 90) * 0.5 },
  pop: { len: 0.15, fn: (x) => Math.sin(2 * Math.PI * (600 + 900 * x) * x) * Math.exp(-x * 30) * 0.4 },
  thump: { len: 0.5, fn: (x) => Math.sin(2 * Math.PI * (90 - 60 * x) * x) * Math.exp(-x * 9) * 0.9 },
  whoosh: { len: 0.35, fn: (x, n, c, len) => n.lp(n(), 0.02 + 0.3 * Math.sin(Math.PI * x / len)) * Math.sin(Math.PI * Math.min(1, x / len)) * 0.9 },
  riser: { len: 1.0, fn: (x, n, c, len) => (n.lp(n(), 0.02 + 0.4 * x / len) * 0.6 + Math.sin(2 * Math.PI * (200 + 600 * x / len) * x) * 0.15) * (x / len) ** 2 },
  kick: { len: 0.4, fn: (x) => Math.sin(2 * Math.PI * (45 * x + 105 * (1 - Math.exp(-x * 30)) / 30)) * Math.exp(-x * 8) * 0.95 },
  snare: { len: 0.2, fn: (x, n) => (n() * 0.6 + Math.sin(2 * Math.PI * 185 * x) * 0.4) * Math.exp(-x * 22) * 0.7 },
  hat: { len: 0.06, fn: (x, n) => { const v = n(); return (v - n.lp(v, 0.15)) * Math.exp(-x * 70) * 0.5; } },
  bass: { len: 0.4, fn: (x, n, c, len) => { const f = c.f || 55, p = (x * f) % 1;
    return Math.tanh(((2 * p - 1) * 0.6 + Math.sin(2 * Math.PI * f * x)) * 1.5) * 0.45 * Math.min(1, x / 0.005) * Math.min(1, (len - x) / 0.04) * Math.exp(-x * 2.5); } },
  tone: { len: 0.5, fn: (x, n, c, len) => Math.sin(2 * Math.PI * (c.f || 440) * x) * Math.min(1, x / 0.01) * Math.exp(-x * 4 / len) * 0.4 },
};
// Karplus-Strong pluck: string-like tone at c.f (Hz), deterministic.
function pluck(c, n) {
  const len = Math.floor((c.len || 0.8) * SR), period = Math.max(2, Math.round(SR / (c.f || 220)));
  const ring = Float32Array.from({ length: period }, () => n()), out = new Float32Array(len);
  for (let i = 0; i < len; i++) { const j = i % period, k = (i + 1) % period; out[i] = ring[j] * 0.4; ring[j] = 0.996 * 0.5 * (ring[j] + ring[k]); }
  return out;
}
function synth(cues, dur) {
  const buf = new Float32Array(Math.ceil(dur * SR));
  cues.forEach((c, idx) => {
    let s = (idx + 1) * 2654435761 >>> 0, lp = 0;
    const n = () => (s = (s * 1664525 + 1013904223) >>> 0) / 2147483648 - 1;
    n.lp = (v, a) => (lp += a * (v - lp)); // one-pole lowpass (stateful per cue)
    let samples;
    if (c.type === 'pluck') samples = pluck(c, n);
    else {
      const v = VOICES[c.type];
      if (!v) die(`unknown cue type "${c.type}" (have: ${[...Object.keys(VOICES), 'pluck'].join(', ')})`, 1);
      const len = c.len || v.len;
      samples = Float32Array.from({ length: Math.floor(len * SR) }, (_, i) => v.fn(i / SR, n, c, len));
    }
    const start = Math.round(c.t * SR), gain = c.gain ?? 1;
    for (let i = 0; i < samples.length && start + i < buf.length; i++) if (start + i >= 0) buf[start + i] += samples[i] * gain;
  });
  return buf;
}
function wav(buf) {
  const n = buf.length, b = Buffer.alloc(44 + n * 2);
  b.write('RIFF', 0); b.writeUInt32LE(36 + n * 2, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(SR, 24); b.writeUInt32LE(SR * 2, 28);
  b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) b.writeInt16LE(Math.round(Math.tanh(buf[i] * 0.8) * 32767), 44 + i * 2); // soft-clip master
  return b;
}

// ---------- commands ----------
const parse = (args, options) => {
  try { return parseArgs({ args, options, allowPositionals: true }); } catch (e) { die(e.message); }
};
const filmDir = (p) => {
  const dir = resolve(p || '.');
  if (!existsSync(join(dir, 'index.html'))) die(`no index.html in ${dir} (run: motion new <dir>)`);
  return dir;
};
const sizeOf = (s) => (s ? (/^\d+x\d+$/.test(s) ? s.split('x').map(Number) : die('--size must be WxH, e.g. 1080x1920')) : null);

async function cmdRender(argv) {
  const { values: o, positionals } = parse(argv, {
    fps: { type: 'string' }, sub: { type: 'string', default: '4' }, size: { type: 'string' },
    from: { type: 'string', default: '0' }, to: { type: 'string' }, out: { type: 'string' },
    silent: { type: 'boolean' }, 'audio-only': { type: 'boolean' }, workers: { type: 'string' }, crf: { type: 'string', default: '16' } });
  const dir = filmDir(positionals[0]), size = sizeOf(o.size), { url, close } = await serve(dir);
  const probe = await launch();
  const { film } = await openFilm(probe, url, size);
  await probe.close();
  const FPS = +(o.fps || film.fps || 60), SUB = Math.max(1, +o.sub), from = +o.from, to = +(o.to ?? film.duration);
  if (!(to > from)) die(`bad range ${from}..${to} (FILM.duration=${film.duration})`);
  const partial = from > 0 || to < film.duration;
  const out = resolve(o.out || join(dir, 'out', partial ? 'preview.mp4' : size ? `final-${film.width}x${film.height}.mp4` : 'final.mp4'));
  mkdirSync(dirname(out), { recursive: true });
  const frames = Math.round((to - from) * FPS), W = Math.max(1, Math.min(+(o.workers || Math.min(6, Math.floor(cpus().length / 3))), frames));
  const tmp = join(tmpdir(), `motion-${process.pid}`); mkdirSync(tmp, { recursive: true });
  const t0 = Date.now(); let done = 0;

  const silent = join(tmp, 'silent.mp4');
  if (o['audio-only']) { // sound pass after picture lock: keep the rendered picture, redo the mix
    if (!existsSync(out)) die(`--audio-only needs an existing ${out}`);
    await ffmpeg(['-i', out, '-map', '0:v', '-c', 'copy', silent]).done;
  } else {
  log(`render ${film.width}x${film.height} ${FPS}fps x${SUB} sub, ${from}-${to}s (${frames} frames) on ${W} workers`);
  const vf = SUB > 1 ? ['-vf', `tmix=frames=${SUB},select='eq(mod(n,${SUB}),${SUB - 1})',setpts=N/${FPS}/TB`] : [];
  const segs = await Promise.all(Array.from({ length: W }, async (_, w) => {
    const a = Math.floor(frames * w / W), b = Math.floor(frames * (w + 1) / W), seg = join(tmp, `seg${w}.mp4`);
    const browser = await launch(), { page } = await openFilm(browser, url, [film.width, film.height]);
    const ff = ffmpeg(['-f', 'image2pipe', '-framerate', String(FPS * SUB), '-i', '-', ...vf, '-r', String(FPS),
      '-c:v', 'libx264', '-preset', 'medium', '-crf', o.crf, '-pix_fmt', 'yuv420p', seg], { stdin: true });
    for (let k = a; k < b; k++) {
      for (let j = 0; j < SUB; j++) await write(ff, await shot(page, from + (k + j / SUB) / FPS));
      if (++done % FPS === 0) log(`  ${(done / FPS).toFixed(0)}s / ${(frames / FPS).toFixed(1)}s  (${((Date.now() - t0) / 1000).toFixed(0)}s elapsed)`);
    }
    ff.stdin.end(); await ff.done; await browser.close();
    return seg;
  }));
  writeFileSync(join(tmp, 'list.txt'), segs.map((s) => `file '${s}'`).join('\n'));
  await ffmpeg(['-f', 'concat', '-safe', '0', '-i', join(tmp, 'list.txt'), '-c', 'copy', silent]).done;
  }
  close();

  // Sound: FILM.cues synthesized + FILM.tracks mixed, shifted into the rendered range, loudnorm -14 LUFS.
  const cues = film.cues.map((c) => ({ ...c, t: c.t - from })).filter((c) => c.t > -2 && c.t < to - from);
  const tracks = film.tracks.map((tr) => ({ ...tr, src: resolve(dir, tr.src), at: (tr.at || 0) - from }));
  for (const tr of tracks) if (!existsSync(tr.src)) die(`FILM.tracks: missing ${tr.src}`, 1);
  if (o.silent || (!cues.length && !tracks.length)) copyFileSync(silent, out);
  else {
    const inputs = [], filters = [];
    if (cues.length) { const score = join(tmp, 'score.wav'); writeFileSync(score, wav(synth(cues, to - from))); inputs.push({ src: score, at: 0, gain: 1 }); }
    inputs.push(...tracks);
    inputs.forEach((tr, i) => {
      const trim = tr.at < 0 ? `atrim=start=${-tr.at},asetpts=PTS-STARTPTS,` : '';
      const delay = tr.at > 0 ? `adelay=${Math.round(tr.at * 1000)}:all=1,` : '';
      filters.push(`[${i + 1}:a]${trim}${delay}volume=${tr.gain ?? 1}[a${i}]`);
    });
    filters.push(`${inputs.map((_, i) => `[a${i}]`).join('')}amix=inputs=${inputs.length}:normalize=0[pre]`);
    // Measure, then static gain + brickwall limiter, re-measured twice so the limiter's loss is made up.
    // Single-pass loudnorm misses -14 LUFS on punchy synthesized scores.
    const ins = ['-i', silent, ...inputs.flatMap((tr) => ['-i', tr.src])];
    const measure = (chain) => {
      const m = spawnSync('ffmpeg', ['-hide_banner', '-nostats', ...ins, '-filter_complex', `${filters.join(';')};[pre]${chain}loudnorm=I=-14:TP=-1:print_format=json[m]`,
        '-map', '[m]', '-f', 'null', '-'], { encoding: 'utf8', maxBuffer: 1 << 26 });
      return Number(JSON.parse(m.stderr.match(/\{[^{}]*"input_i"[^{}]*\}/)?.[0] ?? '{}').input_i);
    };
    const LIM = 'aresample=192000,alimiter=limit=0.75:attack=1:release=60:level=false,aresample=48000'; // 4x-rate limit at -2.5 dBFS leaves room for AAC overshoot: <= -1 dBTP
    let gain = -14 - measure('');
    for (let k = 0; k < 2 && Number.isFinite(gain); k++) gain += -14 - measure(`volume=${gain.toFixed(2)}dB,${LIM},`); // limiter costs loudness: make it up
    if (!Number.isFinite(gain)) gain = 0;
    log(`mix: ${gain.toFixed(1)} dB to -14 LUFS`);
    filters.push(`[pre]volume=${gain.toFixed(2)}dB,${LIM}[mix]`);
    const final = join(tmp, 'final.mp4');
    await ffmpeg([...ins, '-filter_complex', filters.join(';'),
      '-map', '0:v', '-map', '[mix]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', String(to - from), '-movflags', '+faststart', final]).done;
    copyFileSync(final, out);
  }
  rmSync(tmp, { recursive: true, force: true });
  log(`done in ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  console.log(out);
}

// Labeled tile sheet built in the browser: tiles = [{ png: Buffer, label }].
async function tile(browser, tiles, cols, width, outPath) {
  const page = await browser.newPage({ viewport: { width: cols * (width + 8) + 8, height: 200 }, deviceScaleFactor: 1 });
  const cells = tiles.map((x) => `<figure><img src="data:image/${x.type || 'png'};base64,${x.png.toString('base64')}"><figcaption>${x.label}</figcaption></figure>`).join('');
  await page.setContent(`<style>body{margin:0;padding:8px;background:#000;display:grid;grid-template-columns:repeat(${cols},${width}px);gap:8px;font:600 13px monospace;color:#fff}
    figure{margin:0}img{width:${width}px;display:block}figcaption{padding:3px 0}</style>${cells}`);
  await page.evaluate(() => Promise.all([...document.images].map((i) => i.decode())));
  await page.screenshot({ path: outPath, fullPage: true });
  await page.close();
  console.log(outPath);
}

async function cmdSheet(argv) {
  const { values: o, positionals } = parse(argv, {
    every: { type: 'string' }, strip: { type: 'string' }, video: { type: 'string' }, size: { type: 'string' },
    thumb: { type: 'string' }, cols: { type: 'string' }, 'no-phone': { type: 'boolean' },
    poster: { type: 'string' }, at: { type: 'string' } });
  const thumb = +(o.thumb || (o.at ? 360 : 270)), cols = +(o.cols || (o.at ? 4 : 6)), browser = await launch();
  if (o.video) { // sheet from an existing video: reference analysis or a finished render
    const src = resolve(o.video), every = +(o.every || 0.5), tmp = join(tmpdir(), `motion-sheet-${process.pid}`);
    mkdirSync(tmp, { recursive: true });
    await ffmpeg(['-i', src, '-vf', `fps=1/${every},scale=${thumb * 2}:-2`, join(tmp, '%04d.png')]).done;
    const tiles = readdirSync(tmp).sort().map((f, i) => ({ png: readFileSync(join(tmp, f)), label: `${(i * every).toFixed(2)}s` }));
    const outPath = resolve(dirname(src), `${src.split(sep).pop().replace(/\.[^.]+$/, '')}-contact.png`);
    await tile(browser, tiles, cols, thumb, outPath);
    rmSync(tmp, { recursive: true, force: true });
    return browser.close();
  }
  const dir = filmDir(positionals[0]), { url, close } = await serve(dir), { page, film } = await openFilm(browser, url, sizeOf(o.size));
  const outDir = join(dir, 'out'); mkdirSync(outDir, { recursive: true });
  if (o.at) { // storyboard: one key frame per shot, approved before any motion is built
    const keys = o.at.split(',').map(Number);
    if (keys.some((t) => !(t >= 0 && t < film.duration))) die(`--at needs comma-separated times in [0, ${film.duration})`);
    const frames = [];
    for (const [i, t] of keys.entries()) frames.push(await still(page, t, `${i + 1} · ${t.toFixed(2)}s`));
    await tile(browser, frames, Math.min(cols, frames.length), thumb, join(outDir, 'storyboard.png'));
    close(); return browser.close();
  }
  const times = o.every || !film.beats?.length
    ? Array.from({ length: Math.ceil(film.duration / +(o.every || 0.5)) }, (_, i) => i * +(o.every || 0.5))
    : film.beats.map((t, i, b) => t + 0.4 * ((b[i + 1] ?? t + 0.5) - t)).filter((t) => t < film.duration); // just after each beat: states have landed
  const frames = [];
  for (const t of times) frames.push(await still(page, t, `${t.toFixed(2)}s`));
  await tile(browser, frames, cols, thumb, join(outDir, 'contact.png'));
  if (!o['no-phone']) { // readability at phone size: 1 frame per second at 360 px wide
    const phone = [];
    for (let t = 0.6; t < film.duration; t += 1) phone.push(await still(page, t, `${t.toFixed(1)}s`));
    await tile(browser, phone, 5, 360, join(outDir, 'phone.png'));
  }
  if (o.poster) { writeFileSync(join(outDir, 'poster.png'), await shot(page, +o.poster)); console.log(join(outDir, 'poster.png')); }
  if (o.strip) { // 12 consecutive frames around a fast action: catches pops and overlaps
    const T = +o.strip, fps = film.fps || 60, strip = [];
    for (let i = -6; i < 6; i++) strip.push(await still(page, Math.max(0, T + i / fps), `${(T + i / fps).toFixed(3)}s`));
    await tile(browser, strip, 12, Math.min(320, thumb), join(outDir, 'strip.png'));
  }
  close(); await browser.close();
}

// % of pixels whose max channel delta exceeds `tol`, compared in the page (optionally downscaled by `down`).
function pixelDiff(page, x, y, tol, down = 1) {
  return page.evaluate(async ([x, y, tol, down]) => {
    const load = (s) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i); i.src = 'data:image/png;base64,' + s; });
    const [i1, i2] = await Promise.all([load(x), load(y)]), c = new OffscreenCanvas(i1.width / down, i1.height / down), g = c.getContext('2d');
    const px = (i) => { g.drawImage(i, 0, 0, c.width, c.height); return g.getImageData(0, 0, c.width, c.height).data; };
    const p1 = px(i1), p2 = px(i2); let d = 0;
    for (let k = 0; k < p1.length; k += 4) d += Math.max(Math.abs(p1[k] - p2[k]), Math.abs(p1[k + 1] - p2[k + 1]), Math.abs(p1[k + 2] - p2[k + 2])) > tol;
    return 100 * d / (p1.length / 4);
  }, [x.toString('base64'), y.toString('base64'), tol, down]);
}

async function cmdCheck(argv) {
  const { positionals } = parse(argv, {});
  const dir = filmDir(positionals[0]);
  let fail = 0;
  // 1. Lint the render contract in every authored source file.
  const RULES = [[/Math\.random/, 'Math.random (use M.rng(seed))'], [/\bset(Timeout|Interval)\s*\(/, 'timer'],
    [/@keyframes|\banimation\s*:/, 'CSS animation'], [/\btransition\s*:/, 'CSS transition'], [/\bDate\.now\(|new Date\(/, 'wall clock']];
  const WARN = [[/will-change/, 'will-change blurs scaled text']];
  const walk = (d) => readdirSync(d, { withFileTypes: true }).flatMap((e) => e.isDirectory()
    ? (['node_modules', 'out', 'assets', 'refs', '.git'].includes(e.name) ? [] : walk(join(d, e.name)))
    : /\.(html|js|mjs|css)$/.test(e.name) && e.name !== 'motion.js' ? [join(d, e.name)] : []);
  for (const f of walk(dir)) readFileSync(f, 'utf8').split('\n').forEach((line, i) => {
    for (const [re, why] of RULES) if (re.test(line)) { fail++; log(`FAIL ${relative(dir, f)}:${i + 1} ${why}: ${line.trim().slice(0, 90)}`); }
    for (const [re, why] of WARN) if (re.test(line)) log(`warn ${relative(dir, f)}:${i + 1} ${why}`);
  });
  // 2. Determinism: same frames in two fresh loads, second one seeking in reverse (catches carried state).
  const { url, close } = await serve(dir), browser = await launch();
  const a = await openFilm(browser, url), b = await openFilm(browser, url), D = a.film.duration;
  const ts = [0.37, 0.61, 0.89].map((p) => +(p * D).toFixed(3));
  const hash = (buf) => createHash('sha1').update(buf).digest('hex');
  const ia = []; for (const t of ts) ia.push(await shot(a.page, t));
  const ib = []; for (const t of [...ts].reverse()) ib.unshift(await shot(b.page, t));
  for (let i = 0; i < ts.length; i++) {
    if (hash(ia[i]) === hash(ib[i])) continue;
    // Chrome may reuse a cached raster of a layer last drawn at another scale: hairline AA noise, not a bug.
    const d = await pixelDiff(a.page, ia[i], ib[i], 30);
    if (d > 0.3) { fail++; log(`FAIL nondeterministic frame at ${ts[i]}s: ${d.toFixed(2)}% of pixels differ (random, clock, or state carried between frames)`); }
    else log(`warn ${ts[i]}s differs in ${d.toFixed(2)}% of pixels: compositor raster noise, ignored`);
  }
  // 3. Loop seam (info): how far the last frame is from the first.
  const fps = a.film.fps || 60, first = await shot(a.page, 0), last = await shot(a.page, D - 1 / fps);
  const seam = await pixelDiff(a.page, first, last, 12, 4);
  const fonts = await a.page.evaluate(() => [...document.fonts].map((f) => `${f.family}:${f.status}`));
  // 4. Readability: every DOM text must stay fully visible for 1 s + 0.25 s per word (canvas text isn't measured).
  // 5. Clipping: no visible DOM text may have letters cut by the frame or an overflow-hiding ancestor for >= 0.3 s
  //    (shorter runs are text sliding through an edge on purpose).
  const DT = 0.1, runs = new Map(), short = new Map(), cutRuns = new Map(), cuts = [];
  const endCut = (k, c) => { if (c.n * DT >= 0.3 - 1e-6) cuts.push([k, c]); cutRuns.delete(k); };
  for (let t = 0; t <= D + 1e-6; t += DT) {
    await a.page.evaluate((t) => window.seek(t), t);
    const res = await a.page.evaluate(() => {
      const W = innerWidth, H = innerHeight, out = [], cut = [], rg = document.createRange();
      for (const el of document.querySelectorAll('body *')) {
        const nodes = [...el.childNodes].filter((n) => n.nodeType === 3), own = nodes.map((n) => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
        if (!/[\p{L}\p{N}]/u.test(own) || !el.getClientRects().length) continue;
        let op = 1; // effective opacity up the tree
        for (let e = el; e && e.nodeType === 1; e = e.parentElement) { const s = getComputedStyle(e); if (s.visibility === 'hidden') op = 0; op *= +s.opacity; }
        const r = el.getBoundingClientRect(), vis = Math.max(0, Math.min(r.right, W) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, H) - Math.max(r.top, 0));
        const key = own.replace(/\d[\d,.]*/g, '#');
        if (op > 0.6 && vis > 0.5 * r.width * r.height) out.push(key);
        if (op <= 0.6) continue;
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; // glyph box of the element's own text
        for (const n of nodes) { rg.selectNodeContents(n); for (const b of rg.getClientRects()) if (b.width) { x0 = Math.min(x0, b.left); y0 = Math.min(y0, b.top); x1 = Math.max(x1, b.right); y1 = Math.max(y1, b.bottom); } }
        if (!(x1 > x0)) continue;
        let L = 0, T = 0, Rt = W, B = H; // visible box: frame ∩ every overflow-hiding ancestor
        for (let e = el.parentElement; e && e !== document.body; e = e.parentElement) {
          const s = getComputedStyle(e); if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
          const b = e.getBoundingClientRect(); L = Math.max(L, b.left); T = Math.max(T, b.top); Rt = Math.min(Rt, b.right); B = Math.min(B, b.bottom);
        }
        if (x1 <= L || x0 >= Rt || y1 <= T || y0 >= B) continue; // fully hidden is not "cut"
        const fs = parseFloat(getComputedStyle(el).fontSize); // line boxes reach past the ink: ~0.25em above caps, ~0.1em below
        const sides = [['left', L - x0, 4], ['right', x1 - Rt, 4], ['top', T - y0, 4 + 0.25 * fs], ['bottom', y1 - B, 4 + 0.1 * fs]].filter(([, d, tol]) => d > tol);
        const ci = getComputedStyle(el).clipPath.match(/^inset\(([^)]*)\)/); // a clip edge on the text box cuts overhanging glyphs
        if (ci) { const v = ci[1].split(' round')[0].trim().split(/\s+/), r = v[1] ?? v[0], l = v[3] ?? r; // inset(top right bottom left) shorthand
          for (const [s, val] of [['right', r], ['left', l]]) if (parseFloat(val) === 0) sides.push([`clip-path ${s} edge on the text box`, 0]); }
        if (sides.length) cut.push([key, sides.map(([s, d]) => d ? `${s} ${Math.round(d)}px` : s).join(', ')]);
      }
      return { out, cut };
    });
    const seen = new Set(res.out);
    for (const k of seen) runs.set(k, (runs.get(k) || 0) + DT);
    for (const [k, v] of runs) if (!seen.has(k)) { short.set(k, Math.max(short.get(k) || 0, v)); runs.delete(k); }
    const cutNow = new Map(res.cut);
    for (const [k, how] of cutNow) { const c = cutRuns.get(k) || { t, n: 0, how }; c.n++; cutRuns.set(k, c); }
    for (const [k, c] of cutRuns) if (!cutNow.has(k)) endCut(k, c);
  }
  for (const [k, v] of runs) short.set(k, Math.max(short.get(k) || 0, v));
  for (const [k, c] of cutRuns) endCut(k, c);
  for (const [k, c] of cuts) log(`FAIL clipped: "${k.slice(0, 60)}" cut (${c.how}) at ${c.t.toFixed(1)}s for ${(c.n * DT).toFixed(1)}s`);
  fail += cuts.length;
  log(`clipping: ${cuts.length} text item(s) cut by the frame or a container`);
  let slow = 0;
  for (const [text, held] of short) {
    const words = text.split(' ').filter((w) => /[\p{L}\p{N}#]/u.test(w)).length, need = 1 + 0.25 * words;
    if (held + DT / 2 < need) { slow++; log(`FAIL readable: "${text.slice(0, 60)}" holds ${held.toFixed(1)}s, needs ${need.toFixed(1)}s (${words} words)`); }
  }
  fail += slow;
  log(`readability: ${short.size} text items measured, ${slow} too fast`);
  close(); await browser.close();
  log(`fonts: ${fonts.length ? fonts.join(', ') : 'none declared via @font-face (system fallback)'}`);
  if (fonts.some((f) => f.endsWith(':error'))) { fail++; log('FAIL a declared font failed to load'); }
  log(`loop seam: ${seam.toFixed(1)}% of pixels differ between last and first frame (${seam < 1 ? 'seamless' : 'not a loop — fine unless the brief asks for one'})`);
  log(fail ? `check: ${fail} problem(s)` : 'check: ok (render contract, determinism)');
  process.exit(fail ? 1 : 0);
}

function cmdBeats(argv) {
  const { values: o, positionals } = parse(argv, { bpm: { type: 'string' }, dur: { type: 'string' }, offset: { type: 'string', default: '0' } });
  if (positionals[0]) { // measured grid from a supplied track
    const r = spawnSync('uv', ['run', '-q', '--with', 'librosa', '--with', 'numpy', 'python3', join(ROOT, 'bin', 'beats.py'), resolve(positionals[0])], { stdio: ['ignore', 'inherit', 'inherit'] });
    process.exit(r.status ?? 1);
  }
  if (!o.bpm || !o.dur) die('usage: motion beats <track.wav>  |  motion beats --bpm 120 --dur 15 [--offset 0]');
  const bpm = +o.bpm, beats = [];
  for (let t = +o.offset; t < +o.dur; t += 60 / bpm) beats.push(+t.toFixed(3));
  console.log(JSON.stringify({ bpm, beats, downbeats: beats.filter((_, i) => i % 4 === 0), hits: [] }, null, 1));
}

async function cmdSfx(argv) {
  const { values: o, positionals } = parse(argv, { dur: { type: 'string' } });
  const [src, outPath] = positionals;
  if (!src || !outPath) die('usage: motion sfx <cues.json | film-dir> <out.wav> [--dur S]');
  let cues, dur = o.dur && +o.dur;
  if (existsSync(join(resolve(src), 'index.html'))) { // pull FILM.cues straight from the film
    const { url, close } = await serve(resolve(src)), browser = await launch(), { film } = await openFilm(browser, url);
    cues = film.cues; dur ||= film.duration; close(); await browser.close();
  } else cues = JSON.parse(readFileSync(src, 'utf8'));
  writeFileSync(outPath, wav(synth(cues, dur || Math.max(...cues.map((c) => c.t)) + 2)));
  console.log(resolve(outPath));
}

// Footage -> frame sequence + meta.json that M.clip() reads. Frame-accurate and codec-free in the browser.
async function cmdClip(argv) {
  const { values: o, positionals } = parse(argv, { out: { type: 'string' }, fps: { type: 'string', default: '30' },
    from: { type: 'string' }, to: { type: 'string' }, size: { type: 'string' }, vf: { type: 'string' } });
  const src = positionals[0] && resolve(positionals[0]);
  if (!src || !existsSync(src)) die('usage: motion clip <video> [--out assets/clips/<name>] [--fps 30] [--from S --to S] [--size WxH] [--vf "<ffmpeg filters>"]');
  const out = resolve(o.out || join('assets', 'clips', src.split(sep).pop().replace(/\.[^.]+$/, '')));
  rmSync(out, { recursive: true, force: true }); mkdirSync(out, { recursive: true });
  const size = sizeOf(o.size); // fill + center-crop to WxH, else keep aspect at 1080 wide
  const vf = [`fps=${o.fps}`, size ? `scale=${size[0]}:${size[1]}:force_original_aspect_ratio=increase,crop=${size[0]}:${size[1]}` : 'scale=1080:-2', o.vf].filter(Boolean).join(',');
  await ffmpeg([...(o.from ? ['-ss', o.from] : []), ...(o.to ? ['-to', o.to] : []), '-i', src, '-vf', vf, '-q:v', '3', join(out, '%05d.jpg')]).done;
  const frames = readdirSync(out).filter((f) => f.endsWith('.jpg')).length;
  const [width, height] = size || spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', join(out, '00001.jpg')], { encoding: 'utf8' }).stdout.trim().split(',').map(Number);
  writeFileSync(join(out, 'meta.json'), JSON.stringify({ fps: +o.fps, frames, width, height, duration: frames / +o.fps, src: src.split(sep).pop() }, null, 1));
  console.log(out);
  log(`${frames} frames at ${o.fps} fps, ${width}x${height}`);
}

async function cmdPreview(argv) {
  const { values: o, positionals } = parse(argv, { port: { type: 'string', default: '0' } });
  const { url } = await serve(filmDir(positionals[0]), +o.port);
  console.log(`${url}   (?t=3.2 freezes a frame, ?w=1080&h=1080 another format; space pauses, arrows scrub; Ctrl-C stops)`);
}

function cmdNew(argv) {
  const { positionals } = parse(argv, {});
  if (!positionals[0]) die('usage: motion new <dir>');
  const dir = resolve(positionals[0]);
  if (existsSync(dir) && readdirSync(dir).length) die(`${dir} exists and is not empty`);
  cpSync(join(ROOT, 'template'), dir, { recursive: true });
  console.log(dir);
  log('next: write docs/brief.md, then edit index.html. Rules: AGENTS.md (CLAUDE.md imports it).');
}

async function cmdDoctor() {
  const ok = (name, cond, hint = '') => { log(`${cond ? 'ok  ' : 'MISS'} ${name}${cond ? '' : '  ' + hint}`); return cond; };
  const has = (cmd) => spawnSync('sh', ['-c', `command -v ${cmd}`]).status === 0;
  let good = ok('node >= 22', +process.versions.node.split('.')[0] >= 22);
  good = ok('ffmpeg', has('ffmpeg'), 'install ffmpeg') && good;
  good = ok('uv (for beats.py / librosa)', has('uv'), 'install uv') && good;
  ok('yt-dlp (reference videos)', has('yt-dlp'), 'optional');
  ok('web CLI (brand asset capture)', has('web'), 'optional');
  try { const b = await launch(); ok(`chromium ${b.version()}`, true); await b.close(); }
  catch (e) { good = ok('chromium', false, 'npx playwright-core install chromium-headless-shell, or set CHROME_PATH'); }
  process.exit(good ? 0 : 1);
}

const HELP = `motion — motion-graphics lane (seek(t) films rendered from code)

  motion new <dir>                         scaffold a film (index.html, lib/motion.js, house rules)
  motion preview [dir]                     serve the film for live preview in a browser
  motion sheet [dir] [--every S] [--strip T] [--poster T] [--size WxH]
                                           contact sheet (1 frame per beat) + phone test [+ strip, full-res poster] -> out/*.png
  motion sheet [dir] --at T1,T2,...        storyboard: one numbered key frame per shot -> out/storyboard.png
  motion sheet --video file.mp4 [--every S]  contact sheet of any video (reference analysis)
  motion check [dir]                       lint render contract + determinism + loop seam + fonts
  motion render [dir] [--size WxH] [--from S --to S] [--fps 60] [--sub 4] [--workers N] [--silent | --audio-only]
                                           frames -> H.264 + synthesized FILM.cues + FILM.tracks, -14 LUFS
  motion clip <video> [--size WxH] [--from S --to S] [--fps 30] [--vf F]
                                           footage -> assets/clips/<name>/ frames for M.clip() (B-roll, stock, screen recordings)
  motion beats <track.wav> | --bpm N --dur S    beats.json (bpm, beats, downbeats, hits) to stdout
  motion sfx <cues.json|dir> <out.wav>     synthesize cues (kick snare hat bass pluck tone click pop thump whoosh riser)
  motion doctor                            check toolchain`;

const [cmd, ...rest] = process.argv.slice(2);
const CMDS = { new: cmdNew, clip: cmdClip, preview: cmdPreview, sheet: cmdSheet, check: cmdCheck, render: cmdRender, beats: cmdBeats, sfx: cmdSfx, doctor: cmdDoctor };
if (!cmd || cmd === '-h' || cmd === '--help' || cmd === 'help') console.log(HELP);
else if (!CMDS[cmd]) die(`unknown command "${cmd}"\n\n${HELP}`);
else await CMDS[cmd](rest);
