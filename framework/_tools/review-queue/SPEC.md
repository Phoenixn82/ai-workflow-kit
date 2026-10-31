# SPEC — Review Queue: finish the install (frozen work order)

## Goal
A paper icon (`you.review`) sits in the Omarchy 4.0.2 shell bar right section immediately after `omarchy.tailscale`. Clicking it runs `~/.local/bin/rq open`, which opens a floating centered foot window (`app-id review-queue`) running the Textual viewer. A red badge on the icon shows when `~/.local/state/review-queue/queue.json` has unread entries. The queue is seeded with the two Example App plan files. Everything is verified live, not just claimed.

## Already done (do not redo, do not redesign)
- Python package in this repo: `review_queue/{queue.py,viewer.py,cli.py}`; `.venv` managed by uv (`uv sync`); 10 tests pass: `.venv/bin/python -m pytest -q`.
- `~/.local/bin/rq` → `.venv/bin/rq` symlink.
- Bar plugin files written at `~/.config/omarchy/plugins/you.review/{manifest.json,BarWidget.qml}` (pattern copied from the stock weather BarWidget + BarIconButton and the working user plugin `~/.config/omarchy/plugins/you.agents/`).
- Hyprland rule appended to `~/.config/hypr/hyprland.lua`: `o.window({ class = "^review-queue$" }, { float = true, center = true, size = { "60%", "80%" } })` — `hyprctl reload` OK, `hyprctl configerrors` empty.
- Icon PNG `~/.local/share/icons/hicolor/256x256/apps/review-queue.png` and `~/.local/share/applications/review-queue.desktop`.

## Blocker to solve first
`omarchy bar put you.review --after omarchy.tailscale` fails: `omarchy-bar: you.review is not a known widget; run 'omarchy plugin list'`. Find out how Omarchy 4 discovers user bar widgets (read `cat $(which omarchy-bar)`, `omarchy plugin --help`, `omarchy plugin list`, the loader under `/usr/share/omarchy/shell/` — READ ONLY there, never edit `/usr/share/omarchy`), compare with the working `you.agents` manifest, and fix whatever `you.review` is missing (manifest fields, registration in `shell.json` `plugins`, an `omarchy plugin` command, directory naming — whatever the loader actually requires). Then place it right after `omarchy.tailscale` in the right section (prefer `omarchy bar put`; editing `~/.config/omarchy/shell.json` is acceptable if the CLI cannot).

## Steps
1. Solve the blocker above. Confirm the shell loaded the widget with no QML errors: find the omarchy shell / quickshell log (journalctl --user, `omarchy debug --no-sudo --print`, or the shell's own log path) and grep it for `you.review` errors. If `BarWidget.qml` has an error, fix it minimally (keep: paper glyph `󰈙`, badge when unread > 0, tooltip with the count, click → `~/.local/bin/rq open`). If the glyph does not render in the bar font, use another Nerd Font paper/document glyph that does.
2. Seed the queue:
   `~/.local/bin/rq push $HOME/AI_Projects/_tools/example-app/PLAN-REVIEW-LOG.md --note "3 Codex review rounds"` then
   `~/.local/bin/rq push $HOME/AI_Projects/_tools/example-app/PLAN.md --note "Example App plan, hardened"` (PLAN.md must end up newest / index 1). `rq list` must show both, both unread; the bar badge must show.
3. Live test the viewer: run `~/.local/bin/rq open`, wait ≤ 5 s, then prove with `hyprctl clients -j` that a client with class `review-queue` exists, is floating, and is roughly 60%×80% of the monitor and centered. Then close it (`hyprctl dispatch closewindow class:^review-queue$`). After this the PLAN.md entry will be read (the viewer marks on open) — that is expected; re-push PLAN.md once more at the end so the user's first click lands on it unread.
4. Write `README.md` (≤ 40 lines): what it is, `rq push <file> [--note]`, `rq list`, `rq open`, keys (←/→ files, ↑/↓ PgUp/PgDn wheel scroll, q/Esc quit), where the manifest lives, how the bar widget and window rule are wired, how to reinstall (`uv sync`, symlink, `omarchy bar put`).
5. Proof: run `.venv/bin/python -m pytest -q` (must stay 10 passed), `omarchy bar` layout / `python3 -c` dump of shell.json right section showing `you.review` right after `omarchy.tailscale`, `~/.local/bin/rq list`, and the `hyprctl clients -j` excerpt from step 3.

## Constraints
- Never edit anything under `/usr/share/omarchy/` (reading is fine). User config only: `~/.config/omarchy/`, `~/.config/hypr/`.
- Do not change the queue/viewer/CLI behaviour or the test expectations except for a bug that blocks the live run; if you change code, keep the tests green and say why.
- Do not commit. Do not run `omarchy refresh` or `omarchy reinstall`.

## Non-goals
Notifications, multi-machine sync, editing files from the viewer, any GUI other than the foot+Textual window.

## Output
End with a report: files changed (path + what/why, one line each), the proof output verbatim, the root cause of the "not a known widget" blocker, and any deviations.
