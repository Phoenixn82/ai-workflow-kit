---
name: motion
description: Motion-graphics lane for Claude Code and Codex. Make launch videos, showreels, product reels, animated explainers, motion ads, kinetic type, UI-morph films, logo stings and social clips rendered from code (seek(t) HTML -> MP4) with synthesized music/SFX and a self-critique loop. Use for "make a video / motion graphics / reel / promo / launch film / animated ad / hero video for the site", "animate this UI", "in the style of this video", "storyboard a video".
---

# motion — the motion-graphics lane

A film is a folder with one `index.html` whose `window.seek(t)` paints any moment. The `motion` CLI
renders it frame by frame in headless Chromium, synthesizes the score from `FILM.cues`, mixes to
-14 LUFS and checks its own frames. Same lane from Claude Code and Codex. Source of the method:
Lukas Margerie's video + Movez's course (vault: `Reference/2026-10-05 - How to Make Insane Motion
Graphics With Opus 5.5.md`); intake interview + storyboard gate from Chase AI's video (vault:
`Reference/2026-10-06 - Claude Now Does Video (FOR FREE) Thanks To JavaScript.md`). Prompt library: `references/prompts.md`. House rules every film obeys:
the template's `AGENTS.md` (copied into each film; `CLAUDE.md` imports it).

The prompt is 10%; the harness is 90%. The harness = real assets + a reference + a state list on a
beat grid + the seek(t) engine + looking at your own frames until every score is 8+.

## Effort
New film: Claude on Opus 5.5 at xhigh (max when the first 3 s carry a launch); Codex at high
reasoning. Fixes and re-renders: medium. Iteration is the method, not a failure.
A weak film is a brief problem before it is an effort problem: the same one-liner makes solid reels at
low and high effort; max costs far more time for a small gain. Fix the brief, references and storyboard first.

## 0. Intake interview (one round, then defaults)
The user likes this round: run it for every new film. Skip it only when the request says "just go" or
the run is unattended (Codex `/goal`, overnight); then the table below fills every gap. If the subject
is missing, ask for it in plain text first. Then ask only what the request hasn't answered, in ONE
round: Claude = one `AskUserQuestion` call (≤4 questions, the default first as "(Recommended)");
Codex = one message listing the questions with their defaults, then wait.
1. **Length + format**: 15 s 9:16 · 30 s 16:9 explainer · 1:1 social clip.
2. **Look**: a named style (kinetic type, UI-morph product film, 3Blue1Brown-style explainer, cut paper,
   risograph, sketchbook, cross-hatch, isometric) or a reference video/image (step 3).
3. **Sound**: licensed track · synth score (drafts) · narration-first (the VO drives the picture, see
   Voice) · silent.
4. **Recurring hero**: a character, object or shape that carries from the first shot to the last, or none.
The answers go into `docs/brief.md` at step 1.

| Input | Default |
|---|---|
| Subject / product + URL | required — the one thing to ask for if absent |
| Duration | 15 s (6-8 shots) |
| Formats | 1080x1920 first; 1:1 and 16:9 from the same timeline if asked |
| Brand | gathered from the URL (step 2) |
| Reference | none → pick a named style; whatships.com, Dribbble motion, competitors' launch films |
| Music | synthesized in code, 120 BPM; a supplied file goes in `audio/` and gets measured |
| Voiceover | none (see Voice) |
| Where | `<project>/motion/<slug>/` inside a project, else `~/Videos/motion/<slug>/` |

## Approval gates (every film)
The user's standing order (2026-10-16): every motion-graphics job reaches them in four stages, and each
stage waits for their approval before the next one starts.
1. **Hero still** (step 3a): one fully rendered main image at final quality: final size, final look,
   real assets, the hero in place. It settles the look before anything is built around it.
2. **Storyboard** (step 5a): one key pose per shot plus the shot list. It settles story, framing and text.
3. **720p preview** (step 8a): the whole film at 720p with sound. Every change round iterates here and
   re-renders at 720p, never at final quality, until they say the cut is right.
4. **Final** (step 8b): the highest-quality render (full size, full sub-samples, every format asked
   for) starts only after they approve the 720p cut.

Each stage's output goes to `$HOME/Review Queue Inbox/` under a dated name that says the stage
(`<slug>-1-hero-<date>.png`, `-2-storyboard-`, `-3-preview-720p-v<n>-`, `-4-final-<size>-`), then STOP and
wait. Log every verdict in `docs/review_log.md`. A change asked at a gate redoes that stage and shows it
again. Cost is why: a change at gates 1-2 costs seconds, at gate 3 up to an hour, at gate 4
hours for a path-traced film. Unattended runs (Codex `/goal`, overnight) and "just go" requests skip
the intake interview, not the gates: they do the work up to the next gate, queue it and stop, and never
start a final render on their own. Skip a gate only when the user says so for that film. Fixes to a
delivered film re-enter at gate 3.

## Pipeline
1. **Scaffold** — `motion new <dir>`. Write `docs/brief.md` from the director's brief in
   `references/prompts.md`, scaled to the job (a 15 s reel needs ~10 lines, a 2 min film all of it).
2. **Assets** (branded work) — pages and screenshots go through the `web` skill:
   `web act open <url>`, `web act screenshot <dir>/assets/<name>.png` (add `--full` for whole page),
   `web read <url> --raw` to find logo, colors and font names. Download direct asset files (logo
   SVG/PNG, woff2 fonts) into `assets/` and declare fonts with local `@font-face`. List what you
   found before animating. Never redraw product UI from imagination. Local design exports (Claude
   Design HTML, Figma exports) can be rendered with the lane's own headless Chromium (playwright-core
   in `_tools/motion`) to crop UI face-on, e.g. flatten a perspective `matrix3d` and element-screenshot
   at 2x. Local files only; websites still go through `web`.
3. **Reference** (optional) — `yt-dlp -o refs/ref.mp4 <url>` (X and YouTube work), then
   `motion sheet --video refs/ref.mp4 --every 0.5` and study `refs/ref-contact.png`. Write
   `docs/style_guide.md`: palette (hex), type (family, weight, tracking), shot lengths, transition
   types, camera moves, texture/grain, how text enters and exits. Grammar only, never content.
   Image references: Read them directly; copy their look, or use them as assets if the user says so.
3a. **Hero still (gate 1)** — make `seek(t)` paint the film's main image (the hero at its best moment,
   in the final palette, type, assets and lighting) and render it full size at final quality:
   `motion sheet --poster <t>` → `out/poster.png` (path-traced films: one frame from their own renderer
   at final samples). Look at it yourself first and fix anything off-brief or off-brand, then copy it to
   `$HOME/Review Queue Inbox/` and STOP for the user's call. The approved still is the look every
   later shot matches. The user's gate-1 set (2026-10-21): the hero, one alternate look of the same frame
   (`-1-hero-alt-<look>-`), and two detail stills (`-1-detail-<what>-`, e.g. a macro of the real hardware and
   the product's screen), so they pick a look and sees close-up quality before the storyboard.
   Hardware from the user's photos: pull them from the user's photo library (search
   by date), model 1:1 from them, and repaint the
   product's own screen from the photo with every label and readout transcribed.
   A concept look over real hardware: keep the real frame as the visible structure and put the concept's
   skin inside it.
4. **Beat grid** — synthesized score: analytic grid (`M.grid(120, DUR)`). Real track (better than synth for
   anything client-facing; the user found the synth score "cringy"): cut the excerpt on a downbeat with ffmpeg
   into `audio/track.wav` (end on the song's real ending when you can, or bake an `afade`), then
   `motion beats audio/track.wav > beats.json` (fits a steady grid for produced music). Author the picture on a
   nominal grid and retime it: `R = M.retime(beats, 0.5)`, `seek(t) => draw(R.toNominal(t))`, cue times
   `R.toReal(n)`. Line up big picture moments with the song's own crashes and final hit. Pick tracks within ~5% of
   the nominal tempo so springs keep their feel; check tags for "ai generated" and skip "corporate/inspiring".
5. **Story + state list** — the logline and what the viewer should feel at the end go at the top of
   `docs/shotlist.md`; under them, a table on the beat grid (beat/time · shot/state · camera ·
   on-screen text · SFX) with one key time per shot (the moment its state has fully landed).
5a. **Storyboard (gate 2)** — before any motion, make `seek(t)` paint each shot's key pose as a still:
   real layout, type, assets, palette, the hero; no springs yet. `motion sheet --at <key times>` →
   `out/storyboard.png` (numbered tiles = shotlist rows). Look at it yourself first and fix anything
   off-brief, then copy it to `$HOME/Review Queue Inbox/`, post the shot list next to it and
   STOP for the user's call. Changes here cost seconds; after animation each one costs a render loop.
   Unattended runs: critique it against the brief yourself, queue it and stop (see Approval gates).
   The approved poses become the frames the animation lands on.
5b. **Footage** (stock, B-roll, screen recordings) — `motion clip <video> --size WxH --from S --to S
   --vf "<grade>"` writes a frame sequence; in the film `c = await M.clip('assets/clips/<name>')`, make `seek`
   async and draw `await c.frame(t - start)` into a canvas. Frame-accurate and codec-free. Stock: Pexels video
   (`web read` the search page with `--raw` for `videos.pexels.com/video-files/...mp4` links); music: Pixabay
   (`web read <track page> --raw --lane scrapling-fetch` exposes the mp3). Log every source + license in
   `docs/SOURCES.md`; never caption stock people as the client's customers.
6. **Build** — edit `index.html`: scenes on the grid, springs from `lib/motion.js`, layout in
   `M.layout` units, cues pushed on the same events (click per cursor press, whoosh per morph,
   thump on the logo, music voices per beat). Long films: write `docs/ANIMATION_GUIDE.md` first and
   split chapters across subagents so they code in one style.
7. **Critique loop** — `motion check` (must pass: contract, determinism, readability, clipping), then `motion sheet [--strip T]`, then LOOK at
   `out/contact.png`, `out/phone.png`, `out/strip.png` (Claude: Read the PNG; Codex: view_image).
   Run the critique pass in `references/prompts.md`, log scores + 3 worst problems in
   `docs/review_log.md`, fix, repeat. 3 rounds minimum; stop when every score is 8+.
   `motion preview` serves a live scrub view if the user wants to watch.
8. **Render** — `motion render` (`--from/--to` for section previews, `--size 1080x1080` /
   `1920x1080` for more formats, `--sub 1 --fps 30` for a quick draft), then
   `motion sheet --poster <best t>`. Final contact sheet: `motion sheet --video out/final.mp4`.
   DOM + CSS-3D films render several times slower than canvas-only films, so run finals in the background. Sound changes after
   picture lock: `motion render --audio-only` (seconds, keeps the rendered picture).
8a. **720p preview (gate 3)** — once the critique loop passes, render the whole film with sound at
   720p: `motion render --size 1280x720` (vertical `720x1280`, square `720x720`) →
   `out/final-1280x720.mp4`, plus `motion sheet --video` of it. Heavy films (path-traced, CSS-3D at
   length) render on thor with nice (`thor-up` first). Watch it yourself for timing and obvious glitches,
   then copy the MP4 and its contact sheet to `$HOME/Review Queue Inbox/` and STOP. Change rounds
   loop here: fix, re-render 720p (a `--from/--to` section when only one shot changed, then the whole cut
   once more), show again, bump `v<n>`.
8b. **Final (gate 4)** — only after the user approves the 720p cut: run the final render at the highest
   quality (full size, full `--sub`, every format asked for), after the preflight in the
   `motion-final-render-preflight` memory for long or thor renders.
9. **Deliver** — copy `out/final.mp4` (+ formats) and `out/contact.png` to
   `$HOME/Review Queue Inbox/`. Report: path, final scores, what you'd improve next.

## Pacing
Readability beats density. `motion check` fails any DOM text visible for less than 1 s + 0.25 s per word.
It also fails any visible DOM text cut for 0.3 s or more by the frame, an `overflow: hidden` card, or its own
`clip-path` edge (script and italic glyphs overhang their box: reveal wipes must run past both edges and end at
`none`). Long lines shrink to fit inside their container; slams and pops must start small enough to stay in frame;
moving labels fade out before they reach an edge.
Fixes, in order: fewer words, fewer items per shot, longer shots. On a fixed beat grid, author the motion
without pauses and add read holds: `HOLDS = [[n, extra]]`, `n = toOld(R.toNominal(t))`, where each hold freezes
the choreography at a moment its text is fully on screen (springs settled, before any exit fade). Keep hold
lengths in whole beats and their total in whole bars, then start the music excerpt that many bars earlier so the
ending still lands. Real-time elements (flag wind, footage, a slow float) keep moving through holds. Reference:
`~/Videos/motion/example-agency-launch-reel/index.html`.

## Sound
`FILM.cues` = `[{ t, type, f?, len?, gain? }]`, synthesized deterministically at render time.
Voices: `kick snare hat bass(f) pluck(f) tone(f)` for music, `click pop thump whoosh riser` for UI.
Write the score as code over the beat grid (bassline as frequencies, plucks for melody/chords)
so picture and sound share one timeline. Extra audio (supplied music, voiceover) goes in
`FILM.tracks = [{ src, at, gain }]`. `motion sfx <dir> out.wav` renders the score alone to audition.

## Voice
Fish Audio (S2.1 Pro, free TTS API with no usage cap through Nov 2026, voice cloning) via its MCP
connector when it's connected — it isn't yet; the user adds it once (fish.audio account → MCP URL →
Claude Code connectors / Codex MCP config).
Save to `audio/vo.wav`, add to `FILM.tracks`, retime shots to the VO. Without it: no voiceover.
Narration-first (the user supplies a VO file, or Fish Audio made one): record the VO before the shot list,
get word times with `uvx whisper-ctranslate2 audio/vo.wav --model small.en --word_timestamps True
--output_format json --output_dir audio`, and put every shot change and caption on the word that names it.
`espeak-ng` is for timing scratch tracks only, never a deliverable.

## Routes
- **A (default)**: one `index.html`, canvas/SVG/DOM, zero dependencies. Opus picks this unprompted
  and it gives the best results; everything above assumes it. DOM films are fine: set transforms
  (including CSS 3D with `perspective`) and text inside `seek(t)`, never via CSS transitions.
- **B (only when asked by name)**: HyperFrames (`npx hyperframes init`, HTML+GSAP) or Remotion
  (React). Not installed; the critique loop and rules still apply (`motion sheet --video`).
- **Mixed**: video-model base shots traced over in code (generate-then-trace). Video generation
  routes through the external LTX-2 action; no paid video APIs.
- **Path-traced product films** (three-gpu-pathtracer, vendored and patched, in `seek(t)`): look-dev and stills
  run on the GPU box (thor), never a machine without a strong GPU (page setup is slow there). Sync the
  film folder there with `rsync` and run one job per GPU (`MESA_VK_DEVICE_SELECT=<vendor:device>` picks the card).
  A GPU can drop out (Vulkan "device lost") and return a blank white frame with exit 0, and a dropout can also
  leave a pure-black block in a still from another GPU running at the same time: check every batch for a white
  frame or a pure-black 1/8 tile, and re-render flagged stills on one card alone.
  Same frame, same pixels on every page load: the tracer orders meshes, materials, textures and lights by
  three.js uuid, which is random per page, so each page sampled the lights in its own order and traced its own
  grain (`motion check` failed determinism). Number every uuid in scene order before `pt.setScene`; hash the
  tracer's uniforms across two pages to find any new source.
  Black bands that come back on the same frames every run are not a dropout: with fixed seeds a NaN/Inf sample
  lands on the same pixel each time and the bloom blur smears it into bands. Zero NaN/Inf before bloom; a spike
  guard on the render (frame-difference luma > 30 inside a shot) catches either kind.
  Basic-material meshes become one-sided emitters in the tracer: a backdrop seen from inside needs its
  normals flipped (`geometry.scale(-1, 1, 1)`). Look-dev loop: a contact sheet of quick stills; 96-128 spp at
  960x540-1600x900 is enough to judge.
  Realism checklist:
  - A part that reads fake is modelled from photos, not memory. Pull every real photo of it first: the user's
    photos, earlier films' `refs/`, the project's own image folders. Write the visible features down (shapes,
    counts, colours, labels, marks) and model each one.
  - Small real-world marks sell it: marker lines, tally marks, data-matrix labels, paint dots, zip ties, warning
    labels. Paint per-unit variation into one canvas atlas spread over all units (per-mesh UVs projected into it).
    The tracer packs textures at `pt.textureSize` (2048): larger canvases are downscaled.
  - Check decals and labels are not hidden under other geometry (a rod over the label).
  - Punched sheet steel gets real openings (`ExtrudeGeometry` holes, metric UVs, the printed texture placed by
    `repeat`/`offset`): hole walls catch light, painted dots never do. Rounded edges on every moulded part.
  - Zinc/steel hardware under a strong softbox blows out: base colour ~`#76797d`, roughness ~0.5.
  - Black product panels: satin (effective roughness ~0.43) plus a strip softbox above the front for a gradient
    down the face and a line on every top edge. Change one light at a time with exposure fixed (Saviano method).
  - Comment placement: a `//` comment appended to a line of statements silently disables everything after it.
  Renderer options checked 2026-10-22 (research/report2.md): MuTracer (github.com/pissang/mutracer, MIT,
  OpenPBR + OIDN denoise, 0.1.0 Oct 14): try as a side-by-side test renderer, not a drop-in; pmndrs/denoiser
  needs WebGPU and three r185+; cc-blender-skill 1.4.0 (Multires normal bakes) for authoring moulded detail
  offline; SUPRA-Tex non-commercial, skip.

## Hard rules
- Real product UI and real logos only. Never invent screens or claims; metrics come from the user or the site.
- No Math.random, timers, clocks, CSS transitions/animations in film code (`motion check` enforces).
- Banned looks: centered title on gradient, everything fading in, corner labels, frame borders,
  glow on UI chrome, generic particles. Something new every 2-4 s, hook in the first 2 s.
- API keys only in `.env` by name; never in prompts, film code or screenshots.
- No paid APIs, no purchases. The motion kit sold in the source video is not needed.

## Cross-agent use
Claude dispatching Codex: say "use the `motion` skill and `motion` CLI" (tool-parity entry
`motion`); good fits are long critique loops and multi-format batches (`codex-goal-dispatcher`).
Codex handing to Claude: `.codex-claude-handoff/<id>.request.md` with `skill: motion`.
