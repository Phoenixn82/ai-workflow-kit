"""Consume Claude-spawned Codex breadcrumb findings.

This is the consumer side of the codex-spawn-findings bridge. It finds
unconsumed breadcrumb rows, asks one isolated Codex exec process to digest the
referenced rollout transcripts, and later marks approved rows consumed. The
summarizer subprocess deliberately removes CLAUDE_SPAWN and CLAUDE_SESSION_ID
from its environment so it leaves no breadcrumb of its own.
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile


BOUNDARY_TEXT = (
    "IMPORTANT: Do NOT read or execute anything under ~/.claude/, ~/.agents/, "
    ".claude/skills/, or agents/ — they are for a different AI system; ignore them."
)

DIGEST_INSTRUCTION = (
    "Read each of these Codex rollout transcript JSONL files: {paths}  From them, "
    "extract only genuinely worthwhile findings and group them under these 9 headings: "
    "Decisions, Preferences/defaults, Errors+root-cause+fix, Token-expensive approaches, "
    "Project status changes, New skills found, Open loops, Corrections, "
    "Next-session prompt material. Discard rules: no raw code, no trivia, "
    "no errors without a fix, no junk. For each finding note its source "
    "codex_session_id. Output ONLY a markdown digest with those headings; omit empty headings."
)

HARVEST_START = "<!-- HARVEST_KEYS"
HARVEST_END = "-->"


def warn(message):
    print("WARNING: " + message, file=sys.stderr)


def utc_stamp():
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def utc_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"


def find_breadcrumb_files(project_root):
    root = os.path.abspath(project_root)
    patterns = [
        os.path.join(root, ".codex-spawn-findings", "*.jsonl"),
        os.path.join(root, "*", ".codex-spawn-findings", "*.jsonl"),
        os.path.join(root, "projects", "*", ".codex-spawn-findings", "*.jsonl"),
    ]
    seen = set()
    files = []
    for pattern in patterns:
        for path in glob.glob(pattern):
            if not path.endswith(".jsonl"):
                continue
            abs_path = os.path.abspath(path)
            if abs_path in seen or not os.path.isfile(abs_path):
                continue
            seen.add(abs_path)
            files.append(abs_path)
    return files


def collect_unconsumed_rows(files):
    rows = []
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError as exc:
            warn("could not read breadcrumb file %s: %s" % (file_path, exc))
            continue

        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                warn("malformed JSON in %s line %s: %s" % (file_path, index, exc))
                continue
            if not isinstance(obj, dict):
                warn("non-object JSON in %s line %s" % (file_path, index))
                continue
            if obj.get("consumed") is True:
                continue
            rows.append(
                {
                    "file": file_path,
                    "line": index,
                    "codex_session_id": obj.get("codex_session_id"),
                    "rollout_path": obj.get("rollout_path"),
                }
            )
    return rows


def existing_rollout_paths(rows):
    seen = set()
    paths = []
    for row in rows:
        rollout_path = row.get("rollout_path")
        if not isinstance(rollout_path, str) or not rollout_path:
            warn(
                "missing rollout_path for %s line %s"
                % (row.get("file"), row.get("line"))
            )
            continue
        abs_path = os.path.abspath(os.path.expanduser(rollout_path))
        if not os.path.exists(abs_path):
            warn(
                "rollout_path does not exist for %s line %s: %s"
                % (row.get("file"), row.get("line"), abs_path)
            )
            continue
        if abs_path in seen:
            continue
        seen.add(abs_path)
        paths.append(abs_path)
    return paths


def build_prompt(paths):
    joined_paths = " ; ".join(paths)
    prompt = BOUNDARY_TEXT + " " + DIGEST_INSTRUCTION.format(paths=joined_paths)
    return prompt.replace("\r", " ").replace("\n", " ")


def run_codex_digest(paths):
    # Resolve the codex launcher explicitly. On Windows `codex` is a .CMD shim
    # that CreateProcess cannot find by bare name (no PATHEXT resolution under
    # shell=False), so subprocess.run(["codex", ...]) raises FileNotFoundError.
    # shutil.which honours PATHEXT and returns the full codex.CMD path, which
    # subprocess can launch directly.
    codex_bin = shutil.which("codex")
    if not codex_bin:
        print("ERROR: codex CLI not found on PATH (shutil.which('codex') is None)", file=sys.stderr)
        return None

    clean_dir = tempfile.mkdtemp()
    prompt = build_prompt(paths)
    env = os.environ.copy()
    env["CODEX_BREADCRUMB_DISABLE"] = "1"
    env["PYTHONUTF8"] = "1"
    env.pop("CLAUDE_SPAWN", None)
    env.pop("CLAUDE_SESSION_ID", None)

    try:
        completed = subprocess.run(
            [
                codex_bin,
                "exec",
                "--skip-git-repo-check",
                "-C",
                clean_dir,
                prompt,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: codex exec timed out", file=sys.stderr)
        return None
    except OSError as exc:
        print("ERROR: failed to run codex exec: %s" % exc, file=sys.stderr)
        return None

    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        print(
            "ERROR: codex exec exited with code %s" % completed.returncode,
            file=sys.stderr,
        )
        if stderr:
            print(stderr, file=sys.stderr)
        return None
    if not stdout.strip():
        print("ERROR: codex exec produced empty output", file=sys.stderr)
        if stderr:
            print(stderr, file=sys.stderr)
        return None
    return stdout


def key_rows(rows):
    keys = []
    for row in rows:
        keys.append(
            {
                "file": row.get("file"),
                "line": row.get("line"),
                "codex_session_id": row.get("codex_session_id"),
            }
        )
    return keys


def write_digest(project_root, digest_out, digest_markdown, rows):
    root = os.path.abspath(project_root)
    if digest_out:
        digest_path = os.path.abspath(digest_out)
    else:
        digest_path = os.path.join(
            root,
            ".codex-spawn-findings",
            "_harvest-%s.digest.md" % utc_stamp(),
        )
    digest_dir = os.path.dirname(digest_path)
    if digest_dir:
        os.makedirs(digest_dir, exist_ok=True)

    content = digest_markdown.rstrip("\r\n")
    content += "\n\n<!-- HARVEST_KEYS\n"
    content += json.dumps(key_rows(rows), ensure_ascii=False)
    content += "\n-->\n"
    with open(digest_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return digest_path


def harvest(project_root, digest_out):
    files = find_breadcrumb_files(project_root)
    rows = collect_unconsumed_rows(files)
    if not rows:
        print("NO_PENDING")
        return 0

    sources = existing_rollout_paths(rows)
    digest_markdown = run_codex_digest(sources)
    if digest_markdown is None:
        return 1

    digest_path = write_digest(project_root, digest_out, digest_markdown, rows)
    print("DIGEST: " + os.path.abspath(digest_path))
    print("PENDING: %s" % len(rows))
    print("SOURCES: %s" % len(sources))
    return 0


def extract_harvest_keys(digest_text):
    start = digest_text.find(HARVEST_START)
    if start == -1:
        return None
    json_start = digest_text.find("\n", start)
    if json_start == -1:
        return None
    json_start += 1
    end = digest_text.find(HARVEST_END, json_start)
    if end == -1:
        return None
    payload = digest_text[json_start:end].strip()
    if not payload:
        return []
    return json.loads(payload)


def split_line_ending(line):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def group_keys(keys):
    groups = {}
    if not isinstance(keys, list):
        return groups
    for key in keys:
        if not isinstance(key, dict):
            continue
        file_path = key.get("file")
        line_index = key.get("line")
        if not isinstance(file_path, str) or not isinstance(line_index, int):
            continue
        abs_path = os.path.abspath(file_path)
        if abs_path not in groups:
            groups[abs_path] = set()
        groups[abs_path].add(line_index)
    return groups


def mark_file_consumed(file_path, line_indexes):
    try:
        with open(file_path, "r", encoding="utf-8", newline="") as handle:
            lines = handle.readlines()
    except OSError as exc:
        warn("could not read breadcrumb file %s: %s" % (file_path, exc))
        return 0

    marked = 0
    changed = False
    for index in sorted(line_indexes):
        if index < 0 or index >= len(lines):
            warn("line index out of range for %s line %s" % (file_path, index))
            continue
        body, ending = split_line_ending(lines[index])
        try:
            obj = json.loads(body)
        except ValueError as exc:
            warn("malformed JSON in %s line %s: %s" % (file_path, index, exc))
            continue
        if not isinstance(obj, dict):
            warn("non-object JSON in %s line %s" % (file_path, index))
            continue
        obj["consumed"] = True
        obj["consumed_at"] = utc_iso()
        lines[index] = json.dumps(obj, ensure_ascii=False) + ending
        marked += 1
        changed = True

    if changed:
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(lines)
        os.replace(tmp_path, file_path)
    return marked


def mark_consumed(digest_path):
    try:
        with open(digest_path, "r", encoding="utf-8") as handle:
            digest_text = handle.read()
    except OSError as exc:
        print("ERROR: could not read digest %s: %s" % (digest_path, exc), file=sys.stderr)
        return 1

    try:
        keys = extract_harvest_keys(digest_text)
    except ValueError as exc:
        print("ERROR: malformed HARVEST_KEYS block: %s" % exc, file=sys.stderr)
        return 1
    if keys is None:
        print("MARKED: 0")
        return 0

    marked = 0
    for file_path, line_indexes in group_keys(keys).items():
        marked += mark_file_consumed(file_path, line_indexes)
    print("MARKED: %s" % marked)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Harvest unconsumed Codex spawn-finding breadcrumbs into a markdown "
            "digest, or mark digest-listed breadcrumb rows consumed."
        )
    )
    parser.add_argument(
        "--project",
        help="Project root whose bounded .codex-spawn-findings locations should be scanned.",
    )
    parser.add_argument(
        "--digest-out",
        help="Harvest mode output digest path. Defaults under <ROOT>/.codex-spawn-findings/.",
    )
    parser.add_argument(
        "--mark-consumed",
        action="store_true",
        help="Mark breadcrumb rows referenced by a digest HARVEST_KEYS block consumed.",
    )
    parser.add_argument(
        "--digest",
        help="Digest path to read in --mark-consumed mode.",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mark_consumed:
        if not args.digest:
            parser.error("--digest is required with --mark-consumed")
        return mark_consumed(args.digest)
    if not args.project:
        parser.error("--project is required unless --mark-consumed is used")
    return harvest(args.project, args.digest_out)


if __name__ == "__main__":
    sys.exit(main())
