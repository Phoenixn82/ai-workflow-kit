# Motion studio rules

This folder is one film made in code. You are the director, animator, sound designer and render
engineer. The prompt is 10% of the film; these rules and the loop below are the other 90%.
Tooling: the `motion` CLI (`motion --help`). Full workflow: the `motion` skill.

## Render contract
- The film is a pure function of time: `window.seek(t)` paints the exact frame for t.
  `window.FILM` = { duration, width, height, fps, beats, cues, tracks }. `window.READY` resolves
  once fonts/images are loaded (preload with `M.img()`; renderer awaits it).
- No CSS transitions/animations, no setTimeout/setInterval, no requestAnimationFrame outside the
  preview guard, no Date/clock, no state carried between frames. Seeded noise only (`M.rng`).
- Motion uses closed-form springs from `lib/motion.js` (`M.sp`, `M.track`), never fixed easing curves.
  Snappy for UI and leading edges, base for containers/camera, heavy for big type and logos,
  playful only for mascots. Tiny overshoot on UI, none on type. A value with several targets = `M.track`.
- Lay out with `M.layout(W, H)` units and safe areas, never fixed pixels, so 9:16, 1:1 and 16:9
  render from the same timeline (`motion render --size 1080x1080`). Reframe per format; never crop.
- Fonts and assets live in `./assets` (local @font-face). Never depend on a CDN at render time.
- Don't declare a top-level `function seek` in a classic script: it *is* `window.seek`, and
  `window.seek = (t) => seek(t)` then recurses forever. Name the painter `draw`/`frame`.

## Look
- Banned defaults: centered title on a gradient, everything fading in, corner labels and frame
  borders, glow on UI chrome, generic particle bursts, bouncy easing, dead time.
- One display face, one UI face. One accent color unless the brief says otherwise.
- Something new happens on screen every 2-4 seconds. Hook in the first 2 seconds.
- Every line of text stays fully visible for at least 1 s + 0.25 s per word (`motion check` measures
  DOM text and fails anything faster). One idea per shot; when the beat grid is tight, hold the
  choreography while text reads instead of cramming (see "Pacing" in the skill). The user's verdict on
  1-second captions: "almost impossible to read."
- No jitter/"boil" on large elements (maps, photos, cards): even ±1.5 px at 12 fps reads as violent
  wiggle. Texture comes from grain, paper and drawn lines, not shaking assets.
- Real product UI only: crop and animate real screenshots from `./assets`. Never invent screens.
- With a reference: take its grammar (palette, type, pacing, transitions, camera), never its
  content, logos or characters.
- Text inside a morphing container enters after the morph starts and leaves before the next one.
- Never `will-change` on anything the camera scales (blurry text).

## Sound
- Client-facing films use a real licensed track (`FILM.tracks`) with the picture retimed to its beats
  (`M.retime`); synthesized `FILM.cues` are for UI SFX and drafts (voices in `motion --help`). Supplied track: `motion beats audio/track.wav > beats.json`
  and drive the picture from that grid. Synthesized: the grid is analytic (`M.grid(bpm, dur)`).
- State changes land on beats, big moments on downbeats, SFX on hits: a click on every cursor press,
  a whoosh on every container morph, a thump on the logo. The renderer mixes to -14 LUFS.

## Loop before showing anything
0. Four approval gates, in order; each lands in `$HOME/Review Queue Inbox/` and waits for
   The user's approval before the next starts (details: "Approval gates" in the skill):
   gate 1, hero still: the main image fully rendered at final size and quality (`motion sheet --poster T`);
   gate 2, storyboard: one key-frame still per shot (`motion sheet --at T1,T2,...`), approved before any
   motion is built; gate 3, 720p preview of the whole film with sound (`motion render --size 1280x720`),
   where every change round iterates; gate 4, the highest-quality final, only after the 720p cut is
   approved. Unattended runs stop at the next gate and never start a final on their own.
1. `motion check` must pass (contract lint + determinism).
2. `motion sheet` (one frame per beat + phone test; add `--strip T` around fast actions) and LOOK
   at out/contact.png, out/phone.png, out/strip.png. Be a harsh motion director, not a proud author.
3. Score 1-10: hook in first 2s, readability at phone size, motion quality, variety, composition,
   brand accuracy, sound sync. Log scores + the 3 worst problems with timestamps in docs/review_log.md.
4. Fix the 3 worst. Repeat until every score is 8+ (3 rounds minimum on a new film).
5. Only then the gate 3 render at 720p. Preview a section with `--from/--to` while iterating. The
   final render waits for gate 4.

## Deliverables
out/final.mp4 (+ other formats asked for), out/contact.png, out/poster.png (best frame),
docs/style_guide.md, docs/shotlist.md, docs/review_log.md. Say what you would improve next.
