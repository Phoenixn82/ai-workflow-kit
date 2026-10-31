# Review Queue

Review Queue keeps the last 25 things flagged for the user to look at and opens
them from the Omarchy bar. One window: the list on the left, the preview on the
right. It holds only things to look at (images and pages since 2026-10-13,
videos and PDFs back since 2026-10-16):

- images (PNG, JPG, WebP, SVG, GIF), shown at native resolution, never upscaled;
- videos (MP4, M4V, MOV, MKV, WebM), played in a native GTK player
  (`Gtk.Video`, GStreamer) with controls, autoplaying and stopped when you move on;
- PDFs, shown in WebKit's built-in PDF viewer (pages, zoom, search);
- `.html` files, Claude artifacts and other web URLs, rendered by an embedded
  WebKit browser (`webkitgtk-6.0`).

Markdown, text, reports, diffs and logs are refused (`rq push` exits 2), the
inbox ignores them, and older text entries drop out on load. No model is
involved anywhere: viewing costs zero tokens.

## Commands

```sh
rq push <image|video|.pdf|.html|url> [--title name] [--note "context"]
rq list
rq open
rq archive <path|url>
```

`Archive` (button, or the Delete key) hides an entry from the widget. The file
stays where it is; `~/.local/state/review-queue/archive.json` records it, and
`rq push <path>` brings it back.

## How things get queued

- **Shared inbox.** Drop a finished image, video, PDF or `.html` into
  `$HOME/Review Queue Inbox/` on Odin. `review-queue-intake.timer`
  registers it within 15 seconds once writes settle.
- **Claude artifacts.** A Claude Code PostToolUse hook (matcher `Artifact`,
  `python3 -m review_queue.artifact_hook`, in `~/.claude/settings.json`) queues
  every published artifact URL, titled from the page's `<title>`. Reads, lists
  and asset uploads are ignored. claude.ai refuses WebKitGTK, so the hook also
  copies the published HTML to `~/.local/state/review-queue/artifacts/<id>.html`
  and the widget renders that copy in place (page only: no `files` siblings or
  runtime capabilities). `Enter` opens the live artifact in the browser.
- **Live pages.** `rq push http://localhost:3056/ --title "Site preview"`.

## Signing in

The embedded browser keeps cookies per host under
`~/.local/share/review-queue/webkit/`. Private artifacts show claude.ai's
"Sign in" page until the user signs in once inside the widget on that host;
sign-in popups open in a small window.

## Raven

Raven mounts Odin's home at `$HOME` and runs this same source through
`~/.local/bin/raven-review`. The unread badge and read status come from Odin's
queue. Odin `localhost` / `127.0.0.1` entries are tunnelled to Raven over SSH
when opened. Keep preview services running on Odin while they are queued.

## Viewer keys

- `↑` / `↓` (or `j` / `k`, `←` / `→`): move between entries
- On a video: `Space` plays/pauses, `←` / `→` seek 5 s, `↑` / `↓` change the volume;
  `j` / `k` still move between entries
- `Enter`: open the entry in its normal application
- Keys go to the page while it has focus; click the list to navigate again
- `Delete` / `Backspace`: archive the entry
- `q` / `Esc`: quit

## Omarchy wiring

The plugin manifest and widget live in
`~/.config/omarchy/plugins/you.review/`. The widget runs
`~/.local/bin/rq open`; `~/.config/hypr/hyprland.lua` floats, centers, and
sizes (90% of the screen) and keeps fully opaque windows whose app id is `review-queue`, on Odin and Raven. The GTK viewer sets that app id
through `GLib.set_prgname` — `review-queue` is not a legal D-Bus name, so it
cannot be a GApplication id, and without the prgname the existing rule misses
and the window opens tiled.

## Reinstall

From this repository:

```sh
sudo pacman -S --needed webkitgtk-6.0
uv sync
ln -sfn "$PWD/.venv/bin/rq" ~/.local/bin/rq
omarchy-shell shell rescanPlugins
omarchy bar put you.review --after omarchy.tailscale
```
