"""rq — review queue CLI: push | list | open | archive | badge | mark-read."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import queue as q



def is_image_file(path: str | Path) -> bool:
    """Whether an entry should open in the native image viewer."""
    return Path(path).suffix.lower() in q.IMAGE_SUFFIXES


def cmd_push(args: argparse.Namespace) -> int:
    entries = q.push(args.file, title=args.title, note=args.note or "")
    print(f"pushed: {entries[0].title} ({entries[0].path}) — {q.unread_count()} unread")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    entries = q.load()
    if args.json:
        print(json.dumps([e.__dict__ for e in entries], indent=1))
        return 0
    if not entries:
        print("(empty)")
        return 0
    for i, e in enumerate(entries, 1):
        flag = " " if e.read else "*"
        print(f"{flag} {i:2d}. {e.title}  {e.pushed_at}  {e.path}")
    return 0


def cmd_badge(args: argparse.Namespace) -> int:
    print(q.unread_count())
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    target = args.target if q.is_url(args.target) else str(Path(args.target).expanduser().resolve())
    if not q.archive(target):
        print(f"rq: not in the queue: {target}", file=sys.stderr)
        return 2
    print(f"archived: {target}")
    return 0


def cmd_mark_read(args: argparse.Namespace) -> int:
    for e in q.load():
        if e.path == str(Path(args.file).expanduser().resolve()):
            q.mark_read(e.path)
    return 0


def _repo_root() -> Path:
    """Directory holding the review_queue package, for PYTHONPATH."""
    return Path(__file__).resolve().parent.parent


def cmd_open(args: argparse.Namespace) -> int:
    """Open the whole queue in the GTK viewer: list and preview, side by side.

    This used to branch - imv if the newest unread entry happened to be an
    image, otherwise a Textual TUI in a separate foot window - so you got either
    pictures or files depending on what landed last, and queued images rendered
    as binary garbage in the text reader. One window now handles both.

    Runs under the SYSTEM python because PyGObject is a system package and this
    project's venv does not have it.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_repo_root()) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        subprocess.Popen([sys.executable if "gi" in sys.modules else "/usr/bin/python3",
                          "-m", "review_queue.gviewer"],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        return 0
    except OSError as exc:
        print(f"rq: could not start the viewer ({exc})", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rq", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("push", help="add an image, .html file or URL to the front of the queue (unread)")
    s.add_argument("file"); s.add_argument("--title"); s.add_argument("--note")
    s.set_defaults(fn=cmd_push)
    s = sub.add_parser("list", help="show the queue, newest first (* = unread)")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_list)
    sub.add_parser("badge", help="print the unread count").set_defaults(fn=cmd_badge)
    s = sub.add_parser("archive", help="hide an entry from the widget (the file is kept)")
    s.add_argument("target"); s.set_defaults(fn=cmd_archive)
    s = sub.add_parser("mark-read"); s.add_argument("file"); s.set_defaults(fn=cmd_mark_read)
    sub.add_parser("open", help="open the queue in the GTK viewer (list + preview)").set_defaults(fn=cmd_open)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"rq: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
