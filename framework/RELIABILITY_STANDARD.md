# Reliability & Continuity Standard

> Extracted from **reference-app** (the app that "just works every time") on 2026-06-03,
> contrasted against **Agentic OS 2.0** (the app that broke after a week — GPU driver
> drift, blank windows, version desync). Apply this to every new desktop/local app.

## The one idea

**An app that works forever owns its entire world and treats everything outside that world
as hostile, absent, or about to change.**

Agentic OS broke because it trusted the *system* to stay constant — the Windows GPU driver
updated underneath it and the Electron GPU process started crash-looping. reference-app can't
break that way because it carries its own Python runtime, its own dependencies, its own
data, and serves itself over `127.0.0.1` with no external moving parts. Nothing the OS does
next week can reach it.

Every rule below is a way of shrinking your dependence on things you don't control, or
of healing/verifying the few you can't avoid.

---

## Pillar A — Environmental continuity ("still opens after a week")

This is the pillar Agentic OS failed. It is the most important one.

- [ ] **Carry your own runtime.** Freeze the interpreter + all deps into the deliverable.
      reference-app ships a PyInstaller `reference-app.exe` (Python 3.12 + 276 packages baked in) *and*
      a committed `venv\`. It never calls system Python or global packages.
- [ ] **Pin and freeze; never float.** Dependencies are a *snapshot*, not a live feed.
      A committed `venv`/lockfile means "what worked at build time keeps working." Updates are
      a deliberate, tested act — never automatic.
- [ ] **Depend on nothing the OS can swap.** Prefer paths that don't need the GPU, a specific
      driver, a system service, or a globally-installed tool. A locally-served web UI needs none
      of these. *(Agentic OS's fatal mistake: it needed the GPU process, which needed the driver,
      which changed.)* If you must use such a thing, see Pillar B (degrade + verify).
- [ ] **Local-first: no network to launch.** Bind `127.0.0.1`, fixed ports, zero cloud calls on
      startup. The app opens with the Wi-Fi off. Cloud APIs and hosted services are continuity
      bombs — they change versions, deprecate, rate-limit, and go down on someone else's schedule.
- [ ] **Self-contained in one folder; shortcut points *into* it.** reference-app' exe, venv, `app.py`,
      and `data\` all live in one dir; the desktop `.lnk` targets that exe directly. There is no
      separate "install" copy to drift out of sync with the source. *(Agentic OS kept an install
      dir ≠ source, paired by an integrity hash — a whole class of version-desync bugs reference-app
      simply doesn't have.)*

## Pillar B — Launch robustness ("opens every time, on the first double-click")

- [ ] **Idempotent launch — safe to double-click repeatedly.** Check `is the port already open?`
      before spawning; if the service is up, just attach to it. No "address already in use," no
      duplicate processes. (`reference-app-start.ps1` literally says "Safe to double-click repeatedly.")
- [ ] **Readiness-gate the window (condition-based waiting, not `sleep 3`).** Poll the port until
      it actually answers (with a timeout) *before* opening the UI. Never show a window pointed at
      a not-yet-ready server — that's the blank-window failure. reference-app waits up to 80s, checking
      every 0.5s.
- [ ] **Self-heal first run.** Create missing dirs, DB, `.env`, and default admin automatically
      (`os.makedirs(..., exist_ok=True)`, `setup.py`, idempotent migrations). Missing state is
      *created*, never a crash.
- [ ] **Degrade gracefully when optional parts are absent.** ChromaDB missing? Skip vector memory,
      run anyway. Git Bash missing? Warn, continue. One absent component never takes down the core.
- [ ] **Harden against known platform gotchas up front.** Force `PYTHONUTF8=1` /
      `PYTHONIOENCODING=utf-8` (Windows cp1252 crashes), tolerate UTF-8-BOM `.env`, fix PATH order,
      register MIME types. Anticipate the OS's sharp edges instead of getting cut by them.
- [ ] **Fail loud, with a map — never silently.** Every run tees subprocess stdout/stderr to
      `logs\`. On failure the launcher prints the exact log path ("Check logs\server-boot.err.log")
      and actionable next steps ("Install Python 3.11+ from …", "run launch-windows.ps1 once").
      Keep the window open on error (`Read-Host "Press Enter to exit"`) so the message is readable.
- [ ] **Clean shutdown.** Terminate child processes when the window closes. No orphan accumulation
      that fights the next launch for the port.

## Pillar C — Internal robustness ("doesn't crash mid-use, doesn't corrupt data")

- [ ] **Fail closed on every input.** Coerce before use: `(x or "").strip()`, `isinstance()` checks,
      safe defaults. Never assume the shape/type of anything from disk, network, or a user. reference-app'
      recent commit wall is ~12 straight "fix: crashes on a non-string input → fail closed" fixes.
- [ ] **Atomic writes for all persistent state.** temp file → `flush` → `fsync` → `os.replace`.
      A crash or power loss mid-write never corrupts `auth.json` / `settings.json` / the DB.
- [ ] **Timeouts + circuit breakers on every external call.** Per-call connect/read timeouts, a
      global request timeout, and a "dead host" cooldown so one slow/broken dependency can't hang
      the whole app or trigger a retry storm.
- [ ] **Migrations are idempotent and non-fatal.** Schema changes auto-run, guarded, logged-but-
      survivable. An upgrade never bricks the existing DB.
- [ ] **Every fixed bug becomes a regression test.** reference-app has ~892 tests; past crashes (corrupt
      JSON, bad keys, IMAP hangs) each have a test that keeps them dead. *This is the real continuity
      multiplier:* a bug with a test can't silently come back next week.

---

## Minimum-viable continuity for a *new* project (do these first)

1. `git init` + first commit before anything else.
2. Decide the runtime-bundling strategy on day one (PyInstaller / committed venv / single-file
   binary). Don't bolt it on later.
3. Write the **idempotent, readiness-gated, self-logging launcher** before the app is even
   interesting — it's the thing you'll double-click 500 times.
4. Bind local, pin deps, create-dirs-on-boot.
5. One folder. Shortcut points into it. No separate install copy.

## Retrofit checklist for an *existing* shaky app (e.g. Agentic OS)

- Move the GPU/driver-dependent path off the critical launch path (done: `disable-gpu-sandbox`
  baked into code), or migrate the UI to a local-served browser/WebView like reference-app.
- Make the launcher idempotent + readiness-gated + logged.
- Collapse install-dir-vs-source into one self-contained folder, or add a startup integrity check
  that *self-repairs* instead of crashing.
- Add a `logs\` trail on every launch so the next "it broke" is a 2-minute read, not an afternoon.
