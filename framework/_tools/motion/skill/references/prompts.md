# Prompt library

Adapted from Movez, "How to build motion design studio with Opus 5.5" (x.com/0xMovez/status/2104216919033192746)
and Lukas Margerie's walkthrough (youtu.be/747ZnEtsRbg). Commands rewritten for the `motion` CLI.
Fill the [PLACEHOLDERS]; delete what the job doesn't need.

## 1. One-liner (tests the engine, never the idea)
```
make a dynamic 15-second motion graphics video that shows what an incredible motion designer
you are, like it's your showreel for a résumé. go all out.
```
Why it works: "showreel for a résumé" sets a genre with known rules (fast cuts, a new technique
every shot, best work first); "you" as subject means techniques, not product facts to get wrong;
15 s = one pass, 6-8 shots; "go all out" multiplies effort. Weakness: every reel rhymes with every
other reel ("brief contagion"). Variants that shipped:
- Story: `use your showreel energy, but tell a story: the history of [TOPIC] from [START] to today, surprise me with the storyboard. 45 seconds, vertical 9:16.`
- Agency persona: `make a 30-second showreel as if you were a niche branding studio for [AUDIENCE]. Create every graphic from scratch. One accent color. Every shot is a different technique.`
- Anti-slop: `... Avoid frames and text in the corners, the usual giveaways of AI-made video.`
- Sound bar: `S-tier sound design, no generic synth pads. Compose an original score and sync every cut to it.`

## 2. Brand film
```
Make a dynamic [20]-second motion graphics video for [PRODUCT] ([URL]), with the energy of a
motion designer's showreel. Go all out.
Assets
- Visit the site. Use real screenshots, the real logo, real colors and fonts. Save everything to
  ./assets and list what you found before you animate.
- Never redraw the product UI from imagination. Crop and animate the real thing.
Story (one beat each, 2 to 4 seconds)
1. Hook: the problem in 5 words of huge kinetic type.
2. The product appears, the UI assembles itself piece by piece.
3. Three features, each as a UI moment with a cursor doing a real action.
4. One number that proves it works: [METRIC].
5. Logo lockup + [CTA].
Sound: original music, 120 BPM, synthesized in code. UI clicks and whooshes on the beat.
Format: 1080x1920 first, then 1:1 and 16:9 from the same timeline.
```
Keep one session per brand: the second film comes faster because the assets and engine exist.

## 3. Steal a reference's grammar
```
Reference: ./refs/ref.mp4
1. motion sheet --video refs/ref.mp4 --every 0.5 and study the frames.
2. Write docs/style_guide.md: palette (hex), type (family, weight, tracking), shot lengths,
   transition types, camera moves, texture/grain, how text enters and exits.
3. Write docs/shotlist.md for a [DURATION]s video about [SUBJECT] in THAT style.
   Take the grammar of the reference, never its content, logos or characters.
```
Naming a style beats describing one. Give the look and constraints, let the model pick the technique.
A folder of your own images is a reference nobody else can copy.

## 4. Spec: one shape, never cut (UI-morph product film)
```
<inputs>
Product + URL, 8 to 12 UI states that tell its story, the real data shown in each state,
brand colors + fonts + one accent, music (synthesize 120 BPM or a supplied track), formats.
</inputs>
<direction>
Product-film UI motion. One container never cuts: every state is the same element changing size,
radius and fill while its content swaps behind a short blur. A cursor drives every change.
[Warm neutral canvas], one accent. Springs with at most a tiny overshoot.
Banned: bouncy easing, glows, gradients on UI chrome, particle bursts, dead time.
</direction>
<structure>
120 BPM, 8 bars, something happens on every beat.
[logo → CTA button → email field (typed) → loader → success check → dashboard card → chart draws
itself → tooltip on hover → ⌘K palette → toast → logo]
</structure>
<build>
1. index.html, window.seek(t). No CSS transitions, no timers, no carried state.
2. Closed-form springs. A value with many targets = M.track (one spring per change).
3. Text inside a morphing container enters after the morph starts, leaves before the next one (M.swapAlpha).
4. Tab indicators: leading and trailing edges on different springs so they stretch (M.indicator).
5. Start on a downbeat. UI sounds on the beat grid.
6. motion render: 60 fps, 4 subframes blended for motion blur.
</build>
<gotchas>
Never will-change on anything the camera scales. The last frame equals the first, cursor position
and velocity included (motion check reports the loop seam).
</gotchas>
<start>Write the state list on the beat grid (docs/shotlist.md) before code.</start>
```

## 5. Sound pass
```
Score the film to the beat. Synthesize an original [120] BPM track in code on the same timeline as
the picture (FILM.cues: kick/snare/hat, bass line as frequencies, pluck melody). UI sounds: a click
on every cursor press, a whoosh on every container morph, a thump on the logo. Retime any animation
that misses the grid.
```
Supplied track instead: `motion beats audio/track.wav > beats.json`; state changes on beats, big
moments on downbeats, SFX on hits.

Restraint (Cinetic v1.1.0, 2026-10-18, github.com/Leonxlnx/cinetic): one soft sound per resolving text
fragment, near-silence while text is being read, one clean element on the reveal or lockup. Place each SFX
by its audible event, not its file start: a click's onset, a whoosh's peak and a riser's end land on the
picture cue (Earshot `docs/SFX.md`, github.com/Leonxlnx/earshot). Earshot's local listening check (CPU,
MIT) is untested here: try it on one film before relying on it; the user's ear at gate 3 stays the judge.

## 5a. Product film grammar (hardware hero films)
Tumble or slide into view, then macro details of the real parts, then the product doing its job (its own
screen, live), then an exploded view or reveal of the insides, then reassembly into the hero pose that
ends the film (Anatomy Reveal, Movez share 2026-10-15, github.com/annnguyentt/anatomy-reveal-skill).
Copy as 1-4-word fragments with picture-only beats between them; each fragment holds for the readability
minimum. Whips ~0.24 s; detail holds keep a slow drift. Spend motion-blur sub-frames on fast moves and
few on holds.

## 6. Director's brief (long films, overnight runs)
```
You are the director, animator, sound designer and render engineer for a [DURATION] film made in
code. Treat this as a multi-session production. Don't rush to a final render.
## The film in one line
[LOGLINE. What the viewer should feel at the end.]
## References and inputs
- ./refs/ : [video / frames / image library]. Take the grammar, never the content.
- ./audio/track.wav : [use unchanged; measure with motion beats first | none: synthesize].
- Tools: the motion skill + CLI. [Other skills.] Keys in .env by name only. No paid APIs.
## Look
[3-5 lines: palette, type, texture, camera language. Banned looks.]
## Character bible (if characters)
[Proportions, palette sampled from a sheet, expressions, an identity lock that survives every scene.]
## Beat sheet
0:00-0:02  hook: [the single most striking image]
0:02-0:10  [act 1]
...        a new visual payoff every 3-5 seconds
[END]      the last frame sets up the first frame (loop)
## Text on screen
[When captions go huge, when they sit like subtitles. Compose shots to leave room for them.]
## Workflow, with gates (each gate: copy to the Review Queue Inbox, stop, wait for the user's approval)
1. docs/style_guide.md and docs/shotlist.md (every shot: frames, camera, text, SFX).
2. Gate 1, hero still: the main image fully rendered at final size and quality → motion sheet --poster <t> → critique → approval.
3. Gate 2, storyboard: a key-frame still for every shot → motion sheet --at <key times> → critique → approval.
4. Animatic (internal, not a gate): motion render --size 540x960 --sub 1 --fps 30. Fix pacing before polish.
5. Full animation, polish pass, sound pass.
6. Gate 3, 720p preview: the whole film with sound at 720p (motion render --size 720x1280 or 1280x720) → approval. Change rounds re-render here.
7. Gate 4, final: the highest-quality render, only after the 720p cut is approved. Never start it unattended.
8. Split chapters across subagents; write docs/ANIMATION_GUIDE.md first so all code in one style.
## Critique loop (every shot, at least 3 rounds)
Score 1-10: hook, readability at 360 px wide, motion, composition, depth, sound sync, polish.
Log scores + 3 biggest problems in docs/review_log.md. Fix. Repeat until all are 8+.
## Deliverables
out/final.mp4 · out/poster.png · out/contact.png · docs/* · a README on how to re-render.
```

## 7. Critique pass (paste each round)
```
Open out/contact.png, out/phone.png and out/strip.png and look at them properly.
Be a harsh motion director, not a proud author.
Score 1-10: hook in first 2s · readability at phone size · motion quality (springs, no dead
frames) · variety (new thing every 2-4s) · composition · brand accuracy · sound sync.
List the 3 biggest problems with timestamps. Hunt specifically for: text that leaves before it can be read (motion check), assets that jitter, text overlapping during swaps,
anything sliding instead of easing, corner labels and frame borders, centered-on-gradient shots,
blurry scaled text, a dead beat with nothing happening, a stutter at the loop seam.
Fix them, preview only the affected seconds (motion render --from A --to B), then show the new
contact sheet and new scores.
```
