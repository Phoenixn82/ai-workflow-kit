"""GTK4 review-queue viewer: the queue list and the preview, in one window.

The user, 2026-10-13: the widget shows only things to look at. Images render as
pictures; .html files, claude.ai artifacts and other web links render in an
embedded WebKit browser at full fidelity. The user, 2026-10-16: videos play in a
native GTK player and PDFs open in WebKit's built-in PDF viewer. Text, Markdown,
diffs and logs never reach the queue (queue.reviewable). No model is involved:
zero tokens per view.

The browser keeps its cookies in this host's data dir, so signing in to
claude.ai once inside the widget keeps private artifacts viewable.

Runs on the SYSTEM python, not the project venv - PyGObject is a system package.
"""
from __future__ import annotations

import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, GLib, Gtk, WebKit  # noqa: E402

from . import queue as q  # noqa: E402

ARTIFACT_PAGE = ("<body style='font:16px system-ui;color:#aaa;display:grid;place-items:center;"
                 "height:90vh;background:#111;text-align:center'><div>No local copy of this artifact.<br><br>"
                 "claude.ai doesn't run in this viewer.<br>Press Enter to open it in your browser.</div></body>")
SEEK_US = 5_000_000   # Left / Right on a video
VOLUME_STEP = 0.1     # Up / Down on a video
EMPTY_PAGE = ("<body style='font:16px system-ui;color:#888;display:grid;place-items:center;"
              "height:90vh;background:#111'>Nothing queued.</body>")


def _esc(s: str) -> str:
    return GLib.markup_escape_text(s)


def _raven_preview_url(uri: str) -> str | None:
    """Tunnel an Odin loopback preview into Raven; None when no tunnel is needed."""
    if socket.gethostname().lower() != "raven":
        return None
    parts = urlsplit(uri)
    if parts.scheme not in ("http", "https") or parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        return None
    remote_port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, remote_port), timeout=0.2):
            return None  # already reachable on Raven (or an existing tunnel)
    except OSError:
        pass
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
    # ponytail: one tunnel per entry view; reuse tunnels if preview traffic grows.
    subprocess.run(["ssh", "-f", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                    "-o", "ConnectTimeout=5", "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
                    "odin"], check=True, timeout=8, capture_output=True)
    return urlunsplit(parts._replace(netloc=f"127.0.0.1:{local_port}"))


def _entry_uri(target: str) -> str:
    """What the browser loads for an entry: the URL itself (tunnelled on Raven) or a file:// URI."""
    if q.is_url(target):
        try:
            return _raven_preview_url(target) or target
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return target  # the page shows the connection error itself
    return Path(target).as_uri()


def _kind_icon(target: str) -> str:
    suffix = "" if q.is_url(target) else Path(target).suffix.lower()
    if suffix in q.IMAGE_SUFFIXES:
        return "🖼 "
    if suffix in q.VIDEO_SUFFIXES:
        return "🎬 "
    if suffix in q.PDF_SUFFIXES:
        return "📄 "
    return "🌐 "


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%a %d %b %H:%M")
    except ValueError:
        return iso or "?"


def _web_session() -> WebKit.NetworkSession:
    base = Path(GLib.get_user_data_dir()) / "review-queue" / "webkit"
    cache = Path(GLib.get_user_cache_dir()) / "review-queue" / "webkit"
    session = WebKit.NetworkSession.new(str(base), str(cache))
    session.get_cookie_manager().set_persistent_storage(
        str(base / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)
    return session


# --------------------------------------------------------------------- app

class ReviewWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title="Review Queue")
        self.set_default_size(1400, 880)
        self.archived_view = False
        self.entries = q.load()
        self.index = q.newest_unread_index(self.entries) if self.entries else 0

        header = Gtk.HeaderBar()
        self.subtitle = Gtk.Label(label="")
        self.subtitle.add_css_class("dim-label")
        self.subtitle.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        header.set_title_widget(self.subtitle)
        open_btn = Gtk.Button(label="Open externally")
        open_btn.connect("clicked", lambda *_: self.open_external())
        header.pack_end(open_btn)
        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload")
        reload_btn.connect("clicked", lambda *_: self.web.reload())
        header.pack_start(reload_btn)
        archive_btn = Gtk.Button(tooltip_text="Hide this from the queue (Delete key). The file is kept.")
        archive_btn.set_child(Gtk.Box(spacing=6))
        archive_btn.get_child().append(Gtk.Image(icon_name="folder-download-symbolic"))
        archive_btn.get_child().append(Gtk.Label(label="Archive"))
        archive_btn.connect("clicked", lambda *_: self.archive_current())
        header.pack_start(archive_btn)
        self.archive_btn_label = archive_btn.get_child().get_last_child()
        self.archive_btn = archive_btn
        archived_toggle = Gtk.ToggleButton(label="Archived", tooltip_text="Show what you archived (A)")
        archived_toggle.connect("toggled", lambda b: self.set_archived_view(b.get_active()))
        header.pack_start(archived_toggle)
        self.archived_toggle = archived_toggle
        self.set_titlebar(header)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(360)
        paned.set_shrink_start_child(False)
        self.set_child(paned)

        # left: the whole queue, always visible
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self.on_row_selected)
        left = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        left.set_child(self.listbox)
        paned.set_start_child(left)

        # right: a picture, a video or a browser (pages, artifacts, PDFs)
        self.stack = Gtk.Stack()
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.SCALE_DOWN)  # never upscaled or cropped
        self.picture.set_can_shrink(True)
        self.web = WebKit.WebView(network_session=_web_session())
        self.web.connect("create", self.on_popup)  # sign-in popups (Google, claude.ai)
        self.web.get_settings().set_default_charset("utf-8")  # local pages without <meta charset>
        self.video = Gtk.Video(autoplay=True)
        self.stack.add_named(self.picture, "image")
        self.stack.add_named(self.video, "video")
        self.stack.add_named(self.web, "web")
        paned.set_end_child(self.stack)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)  # before the list eats Space
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.populate()

    # ------------------------------------------------------------- list
    def populate(self) -> None:
        self.listbox.remove_all()
        for e in self.entries:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6); box.set_margin_bottom(6)
            box.set_margin_start(10); box.set_margin_end(10)
            title = Gtk.Label(xalign=0)
            mark = "" if e.read else "● "
            kind = _kind_icon(e.path)
            mark = "" if self.archived_view else mark
            title.set_markup(f"{mark}{kind}<b>{_esc(e.title)}</b>")
            title.set_wrap(True)
            title.set_lines(2)
            sub = Gtk.Label(xalign=0, label=_local_time(e.pushed_at))
            sub.add_css_class("dim-label")
            box.append(title); box.append(sub)
            if e.note:
                note = Gtk.Label(xalign=0, label=e.note, wrap=True, lines=2)
                note.set_ellipsize(3)
                note.add_css_class("dim-label")
                box.append(note)
            row.set_child(box)
            self.listbox.append(row)
        if self.entries:
            self.listbox.select_row(self.listbox.get_row_at_index(self.index))

    def set_archived_view(self, on: bool) -> None:
        self.archived_view = on
        self.archive_btn_label.set_text("Restore" if on else "Archive")
        self.archive_btn.set_tooltip_text("Put this back in the queue (Delete key)" if on
                                          else "Hide this from the queue (Delete key). The file is kept.")
        self.entries = q.load_archive() if on else q.load()
        self.index = 0 if on or not self.entries else q.newest_unread_index(self.entries)
        self.populate()
        if not self.entries:
            self.show_current()

    def on_row_selected(self, _box, row) -> None:
        if row is None:
            return
        self.index = row.get_index()
        self.show_current()

    # ---------------------------------------------------------- preview
    def show_current(self) -> None:
        self.video.set_file(None)  # stop a playing video when the entry changes
        self.web.load_uri("about:blank")  # and any page audio or video (reloads cleanly on return)
        if not self.entries:
            self.subtitle.set_text("Archive · empty" if self.archived_view else "Review Queue · empty")
            self.web.load_html(EMPTY_PAGE.replace("Nothing queued.", "Nothing archived.")
                               if self.archived_view else EMPTY_PAGE, None)
            self.stack.set_visible_child_name("web")
            return
        e = self.entries[self.index]
        self.subtitle.set_text(("Archived · " if self.archived_view else "")
                               + f"{self.index + 1}/{len(self.entries)} · {e.title}"
                               + (f" · {e.note}" if e.note else ""))
        if not q.is_url(e.path) and not Path(e.path).exists():
            self.web.load_html(f"<body style='font:16px system-ui;padding:2em'>Missing file<br><br>"
                               f"{GLib.markup_escape_text(e.path)}</body>", None)
            self.stack.set_visible_child_name("web")
        elif Path(e.path).suffix.lower() in q.IMAGE_SUFFIXES and not q.is_url(e.path):
            self.picture.set_filename(e.path)
            self.stack.set_visible_child_name("image")
        elif Path(e.path).suffix.lower() in q.VIDEO_SUFFIXES and not q.is_url(e.path):
            self.video.set_filename(e.path)
            self.stack.set_visible_child_name("video")
        elif q.is_claude_artifact(e.path):
            # the user, 2026-10-29: artifacts stay in the widget. The hook's local copy renders here;
            # Enter still opens the live claude.ai page.
            snap = q.artifact_snapshot(e.path)
            if snap.is_file():
                self.web.load_uri(snap.as_uri())
            else:
                self.web.load_html(ARTIFACT_PAGE, None)
            self.stack.set_visible_child_name("web")
        else:
            self.web.load_uri(_entry_uri(e.path))
            self.stack.set_visible_child_name("web")
        if not self.archived_view:
            q.mark_read(e.path)
            e.read = True

    def on_popup(self, view, _action):
        popup = WebKit.WebView(related_view=view)
        win = Gtk.Window(transient_for=self, default_width=560, default_height=720)
        win.set_child(popup)
        popup.connect("ready-to-show", lambda *_: win.present())
        popup.connect("close", lambda *_: win.destroy())
        return popup

    # ------------------------------------------------------------- keys
    def open_external(self) -> None:
        if self.entries:
            target = self.entries[self.index].path
            subprocess.Popen(["xdg-open", _entry_uri(target) if q.is_url(target) else target],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def archive_current(self) -> None:
        if not self.entries:
            return
        target = self.entries.pop(self.index).path
        q.unarchive(target) if self.archived_view else q.archive(target)
        self.listbox.remove(self.listbox.get_row_at_index(self.index))
        if not self.entries:
            self.show_current()
            return
        self.index = min(self.index, len(self.entries) - 1)
        self.listbox.select_row(self.listbox.get_row_at_index(self.index))

    def _focus_in_web(self) -> bool:
        w = self.get_focus()
        while w is not None:
            if w is self.web:
                return True
            w = w.get_parent()
        return False

    def on_key(self, _c, keyval, _code, _state) -> bool:
        on_web = self.stack.get_visible_child_name() == "web"
        if on_web and self._focus_in_web():
            return False                  # typing, scrolling and sign-in belong to the page
        name = Gdk.keyval_name(keyval)
        if self.stack.get_visible_child_name() == "video" and self._video_key(name):
            return True
        if name in ("q", "Escape"):
            self.close(); return True
        if name in ("Down", "j", "Right"):
            self._move(1); return True
        if name in ("Up", "k", "Left"):
            self._move(-1); return True
        if name in ("Return", "KP_Enter"):
            self.open_external(); return True
        if name in ("Delete", "BackSpace"):
            self.archive_current(); return True
        if name == "a":
            self.archived_toggle.set_active(not self.archived_toggle.get_active()); return True
        return False

    def _video_key(self, name: str) -> bool:
        """Space plays/pauses, Left/Right seek, Up/Down set the volume. j/k still move entries."""
        stream = self.video.get_media_stream()
        if name not in ("space", "Left", "Right", "Up", "Down"):
            return False
        if stream is None:
            return True
        if name == "space":
            stream.set_playing(not stream.get_playing())
        elif name in ("Left", "Right") and stream.is_seekable():
            step = SEEK_US if name == "Right" else -SEEK_US
            end = stream.get_duration() or stream.get_timestamp() + step
            stream.seek(max(0, min(end, stream.get_timestamp() + step)))
        elif name in ("Up", "Down"):
            step = VOLUME_STEP if name == "Up" else -VOLUME_STEP
            stream.set_muted(False)
            stream.set_volume(max(0.0, min(1.0, stream.get_volume() + step)))
        return True

    def _move(self, delta: int) -> None:
        if not self.entries:
            return
        i = max(0, min(len(self.entries) - 1, self.index + delta))
        self.listbox.select_row(self.listbox.get_row_at_index(i))


class ReviewApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__()          # no application id, so app_id comes from prgname

    def do_activate(self) -> None:
        win = ReviewWindow(self)
        win.show_current()
        win.present()


def main() -> int:
    GLib.set_prgname("review-queue")
    return ReviewApp().run(sys.argv[:1])


if __name__ == "__main__":
    raise SystemExit(main())
