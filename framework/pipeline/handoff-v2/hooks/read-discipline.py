#!/usr/bin/env python3
"""Fail-open PreToolUse guardrail for wasteful Claude Code Read calls."""

import json
import os
import re
import stat
import sys
import tempfile
import time


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ico", ".bmp")
SEVEN_DAYS = 7 * 24 * 60 * 60


def state_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "state", "read-discipline")


def prune_state_files(directory):
    """Remove at most 50 old state files; failure is intentionally harmless."""
    try:
        cutoff = time.time() - SEVEN_DAYS
        removed = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if removed >= 50:
                    break
                if not entry.name.endswith(".json"):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    pass
    except OSError:
        pass


def load_state(session_id):
    """Return (state, path), or (None, None) when persistence is unavailable."""
    try:
        directory = state_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if not os.access(directory, os.W_OK):
            return None, None
        prune_state_files(directory)
        path = os.path.join(directory, session_id + ".json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                data = {}
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError, TypeError):
            # A corrupt state file is equivalent to no usable session state.
            data = {}
        return data, path
    except OSError:
        return None, None


def save_state(path, data):
    """Atomically save state.  A failed save never changes the hook decision."""
    temporary = None
    try:
        directory = os.path.dirname(path)
        fd, temporary = tempfile.mkstemp(prefix=".read-discipline-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def binary_magic(head):
    """Recognize binary data without ever inspecting more than 32 bytes."""
    return (
        b"\x00" in head
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or head.startswith(b"RIFF") and head[8:12] == b"WEBP"
        or head.startswith(b"%PDF-")
        or head.startswith(b"\x00\x00\x01\x00")
        or head.startswith(b"BM")
    )


def inspect_file(path):
    """Return (stat_result, head) for a readable regular file, else None."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            head = os.read(fd, 32)
        finally:
            os.close(fd)
        return info, head
    except OSError:
        return None


LINE_CAP = 5000
# A large single-line JSON counts as "1 line" and would sail through, costing
# tens of thousands of tokens of context. Lines alone are not the unit; bytes cap too.
BYTE_CAP = 60_000


def line_count_capped(path):
    """Bounded count: exact up to LINE_CAP, then LINE_CAP as a floor sentinel.

    401 was the original cap, but "401+" throws away the one number the model
    needs to pick a sensible offset/limit. 5,000 lines is ~1 ms of counting
    and covers nearly every real source file exactly."""
    count = 0
    saw_bytes = False
    last_byte = b""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        while True:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            saw_bytes = True
            count += chunk.count(b"\n")
            if count >= LINE_CAP:
                return LINE_CAP
            last_byte = chunk[-1:]
    finally:
        os.close(fd)
    if saw_bytes and last_byte != b"\n":
        count += 1
    return count


def emit_deny(lines, size=None):
    # 401 is deliberately a lower-bound sentinel; bounded counting avoids
    # loading or traversing arbitrarily large files in the permission path.
    shown = ("%d+" % LINE_CAP) if lines >= LINE_CAP else str(lines)
    if size is not None and size > BYTE_CAP:
        reason = (
            "whole-file Read is %d bytes (%s lines) - over the %d-byte cap; "
            "use offset/limit, or Grep for the symbol first" % (size, shown, BYTE_CAP)
        )
    else:
        reason = (
            "whole-file Read is over 400 lines (counted " + shown + "); "
            "use offset/limit, or Grep for the symbol first"
        )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, separators=(",", ":")))


def emit_warning(minutes):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "already read %d min ago, unchanged — it is in your context" % minutes,
    }}, ensure_ascii=False, separators=(",", ":")))


def record(state, state_path, file_path, info, lines, span=None):
    state[file_path] = {
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
        "lines": lines,
        "span": span,            # [offset, limit] of a ranged read, else None
        "at": time.time(),
    }
    save_state(state_path, state)


def main():
    raw = sys.stdin.buffer.read()
    if not raw:
        return
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        return
    if data.get("tool_name") != "Read":
        return
    tool_input = data.get("tool_input")
    session_id = data.get("session_id")
    if not isinstance(tool_input, dict) or not isinstance(session_id, str):
        return
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return

    # An invalid ID must never be used to form a state-file pathname.  With no
    # safe session persistence, fail open rather than applying session policy.
    if not SESSION_ID_RE.fullmatch(session_id):
        return
    state, state_path = load_state(session_id)
    if state is None:
        return

    inspected = inspect_file(file_path)
    if inspected is None:
        return
    info, head = inspected
    if file_path.lower().endswith(IMAGE_SUFFIXES) or binary_magic(head):
        return

    previous = state.get(file_path)
    unchanged = isinstance(previous, dict) and (
        previous.get("mtime_ns") == info.st_mtime_ns and previous.get("size") == info.st_size
    )
    ranged = "offset" in tool_input or "limit" in tool_input
    span = [tool_input.get("offset"), tool_input.get("limit")] if ranged else None
    prior_was_whole = isinstance(previous, dict) and previous.get("lines") is not None
    # Redundant only if the SAME thing is being re-read: a whole read after a
    # whole read, or a ranged read with the identical span. Two different
    # ranges of one file are new content each time (found while reviewing a
    # 482-line file in two slices).
    same_span = isinstance(previous, dict) and previous.get("span") == span
    redundant = unchanged and ((prior_was_whole and not ranged) or (ranged and same_span))
    if redundant:
        record(state, state_path, file_path, info, previous.get("lines"), span)
        then = previous.get("at", time.time())
        try:
            minutes = max(0, int((time.time() - float(then)) // 60))
        except (TypeError, ValueError):
            minutes = 0
        emit_warning(minutes)
        return

    if ranged:
        # A whole read that follows a ranged read must stay allowed (edit
        # safety valve), so keep lines=None until a real whole read happens.
        record(state, state_path, file_path, info, None, span)
        return

    lines = line_count_capped(file_path)
    # Evaluated on EVERY non-redundant whole read against the file's current
    # size. An earlier version gated this on `previous is None`, which meant a
    # 1-line ranged peek permanently unlocked unbounded reads of that path, and
    # a file read once while small stayed unlocked after growing. The "edit
    # safety valve" that motivated the gate is unnecessary: Codex verified from
    # the official Edit reference that a ranged Read satisfies Edit's
    # read-before-edit precondition.
    if lines > 400 or info.st_size > BYTE_CAP:
        emit_deny(lines, info.st_size)
        return
    record(state, state_path, file_path, info, lines)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hooks must never turn an internal failure into a failed tool call.
        pass
