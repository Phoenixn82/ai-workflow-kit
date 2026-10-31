#!/usr/bin/env python3
"""Fail-open PreToolUse guardrail for unbounded Bash file inspection."""

import json
import os
import re
import shlex
import stat
import sys


INSPECTORS = {"cat", "head", "tail", "less", "more", "sed", "awk"}
UNBOUNDED_INSPECTORS = {"cat", "less", "more", "awk"}
CONTROL_OPERATORS = {"&&", "||", ";"}
REDIRECTIONS = {"<", ">", ">>", ">|"}
BYTE_CAP = 60_000
LINE_LIMIT = 400
LINE_CAP = 5_000

SED_RANGE_RE = re.compile(r"^(\d+)\s*,\s*(\d+)p$")
PYTHON_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")


def tokenize(command):
    """Tokenize only the shell subset needed by the policy, else return None."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    if not tokens:
        return []

    # Heredocs and here-strings can contain arbitrary shell text which is not a
    # command pipeline. Unsupported punctuation is also a parse doubt.
    if any(token.startswith("<<") for token in tokens):
        return None
    if any(
        token in {"<", ">"} and index + 1 < len(tokens) and tokens[index + 1].startswith("(")
        for index, token in enumerate(tokens)
    ):
        return None
    punctuation = set("|&;<>")
    allowed = CONTROL_OPERATORS | {"|"} | REDIRECTIONS
    for token in tokens:
        if token and all(character in punctuation for character in token) and token not in allowed:
            return None
    return tokens


def pipelines_from(tokens):
    """Return pipelines made of argv-like segments, or None on bad syntax."""
    pipelines = []
    pipeline = []
    segment = []

    for token in tokens:
        if token == "|":
            if not segment:
                return None
            pipeline.append(segment)
            segment = []
        elif token in CONTROL_OPERATORS:
            if not segment:
                return None
            pipeline.append(segment)
            pipelines.append(pipeline)
            pipeline = []
            segment = []
        else:
            segment.append(token)

    if segment:
        pipeline.append(segment)
    elif pipeline:
        return None
    if pipeline:
        pipelines.append(pipeline)
    return pipelines


def command_position(segment):
    """Return (command name, argument start), limited to the frozen wrappers."""
    if not segment:
        return None, None
    position = 0
    if segment[position] == "command":
        position += 1
        if position >= len(segment):
            return None, None
    return segment[position], position + 1


def has_stdout_redirection(segment):
    """Recognize stdout redirection; stderr-only redirection is not a bound."""
    for index, token in enumerate(segment):
        if token not in {">", ">>", ">|"}:
            continue
        if index and segment[index - 1] in {"0", "2"}:
            continue
        return True
    return False


def sed_is_quiet(segment):
    _name, start = command_position(segment)
    if start is None:
        return False
    for token in segment[start:]:
        if token == "-n" or (token.startswith("-") and not token.startswith("--") and "n" in token[1:]):
            return True
        if token == "--silent" or token == "--quiet":
            return True
    return False


def is_bounded_consumer(segment):
    name, _start = command_position(segment)
    if name in {"head", "tail", "wc", "grep", "awk", "jq", "cut"}:
        return True
    if isinstance(name, str) and PYTHON_RE.fullmatch(name):
        return True
    return name == "sed" and sed_is_quiet(segment)


def has_later_bound(pipeline, index):
    """Apply the spec's explicit list of context-bounding consumers."""
    remainder = pipeline[index:]
    if any(has_stdout_redirection(segment) for segment in remainder):
        return True
    later = pipeline[index + 1:]
    if any(is_bounded_consumer(segment) for segment in later):
        return True
    names = [command_position(segment)[0] for segment in later]
    return any(names[position:position + 2] == ["sort", "uniq"] for position in range(len(names) - 1))


def safe_file_operand(token):
    """Resolve a literal file operand; dynamic shell operands are allowed."""
    if not token or token == "-" or token.startswith(("<(", ">(")):
        return None
    if any(character in token for character in "*?["):
        return None
    if "$" in token or "`" in token or token.startswith("("):
        return None
    return os.path.abspath(os.path.expanduser(token))


def file_operands(segment, start):
    """Return literal operands for cat/less/more while skipping shell syntax."""
    operands = []
    options = True
    index = start
    while index < len(segment):
        token = segment[index]
        if token in REDIRECTIONS:
            index += 2
            continue
        if index + 1 < len(segment) and segment[index + 1] in REDIRECTIONS and token.isdigit():
            index += 1
            continue
        if options and token == "--":
            options = False
        elif options and token.startswith(("-", "+")):
            pass
        else:
            path = safe_file_operand(token)
            if path is not None:
                operands.append(path)
        index += 1
    return operands


def awk_operands_if_unfiltered(segment, start):
    """Return awk input files only when its program clearly has no pattern."""
    index = start
    program = None
    while index < len(segment):
        token = segment[index]
        if token in REDIRECTIONS:
            index += 2
            continue
        if token == "--":
            index += 1
            break
        if token in {"-f", "--file"} or token.startswith("--file="):
            return []
        if token in {"-F", "-v", "--field-separator", "--assign"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        program = token
        index += 1
        break

    if program is None or not (program.strip().startswith("{") and program.strip().endswith("}")):
        return []

    operands = []
    while index < len(segment):
        token = segment[index]
        if token in REDIRECTIONS:
            index += 2
            continue
        if "=" not in token:
            path = safe_file_operand(token)
            if path is not None:
                operands.append(path)
        index += 1
    return operands


def inspect_file(path):
    """Return a readable regular file's size and bounded line count."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
            return None
        return info.st_size, line_count_capped(path)
    except OSError:
        return None


def line_count_capped(path):
    """Count lines exactly up to LINE_CAP without reading the whole file."""
    count = 0
    saw_bytes = False
    last_byte = b""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        while True:
            chunk = os.read(descriptor, 8192)
            if not chunk:
                break
            saw_bytes = True
            count += chunk.count(b"\n")
            if count >= LINE_CAP:
                return LINE_CAP
            last_byte = chunk[-1:]
    finally:
        os.close(descriptor)
    if saw_bytes and last_byte != b"\n":
        count += 1
    return count


def requested_line_span(segment, name, start):
    """Return a confidently parsed head/tail/sed line span, else None."""
    arguments = segment[start:]
    if name in {"head", "tail"}:
        span = None
        for index, token in enumerate(arguments):
            value = None
            if token == "-n" and index + 1 < len(arguments):
                value = arguments[index + 1]
            elif token.startswith("-n") and len(token) > 2:
                value = token[2:]
            elif token.startswith("--lines="):
                value = token.split("=", 1)[1]
            if isinstance(value, str) and value.isdigit():
                span = int(value)
        return span

    if name == "sed" and sed_is_quiet(segment):
        script = None
        index = 0
        while index < len(arguments):
            token = arguments[index]
            if token in {"-n", "--silent", "--quiet"}:
                index += 1
                continue
            if token == "-e" and index + 1 < len(arguments):
                script = arguments[index + 1]
                break
            if token.startswith("-e") and len(token) > 2:
                script = token[2:]
                break
            if token.startswith("-"):
                index += 1
                continue
            script = token
            break
        if script is not None:
            match = SED_RANGE_RE.fullmatch(script.strip())
            if match:
                first, last = map(int, match.groups())
                if last >= first:
                    return last - first + 1
    return None


def shown_lines(lines):
    return "%d+" % LINE_CAP if lines >= LINE_CAP else str(lines)


def emit_deny(name, path, size, lines):
    reason = (
        "unbounded %s inspection of %s is %d bytes (%s lines); "
        "use `sed -n 'A,Bp'`, `head -n`, or Grep"
        % (name, path, size, shown_lines(lines))
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, separators=(",", ":")))


def emit_warning(name, span):
    context = "%s: that is %d lines; narrow the range or Grep first" % (name, span)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": context,
    }}, separators=(",", ":")))


def main():
    raw = sys.stdin.buffer.read()
    if not raw:
        return
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return

    tokens = tokenize(command)
    if tokens is None:
        return
    pipelines = pipelines_from(tokens)
    if pipelines is None:
        return

    warnings = []
    for pipeline in pipelines:
        for index, segment in enumerate(pipeline):
            name, start = command_position(segment)
            if name not in INSPECTORS:
                continue

            span = requested_line_span(segment, name, start)
            if span is not None and span > LINE_LIMIT:
                warnings.append((name, span))

            if name not in UNBOUNDED_INSPECTORS or has_later_bound(pipeline, index):
                continue
            if name == "awk":
                operands = awk_operands_if_unfiltered(segment, start)
            else:
                operands = file_operands(segment, start)
            for path in operands:
                inspected = inspect_file(path)
                if inspected is None:
                    continue
                size, lines = inspected
                if size > BYTE_CAP or lines > LINE_LIMIT:
                    emit_deny(name, path, size, lines)
                    return

    if warnings:
        name, span = max(warnings, key=lambda warning: warning[1])
        emit_warning(name, span)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook implementation or parse failure must never fail a tool call.
        pass
