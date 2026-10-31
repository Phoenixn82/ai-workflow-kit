"""Deterministic transcript pre-filter. No LLM, no network.

Does ~99.6% of the reduction: prose is 0.4% of a transcript (measured 23.6 KB of
5.92 MB). Every extracted item gets an immutable prefilter-assigned candidate ID,
which is what makes Pass B's linkage verifiable rather than asserted (PLAN.md D/E).

Two measured facts drive the parsing rules:
  * `type:"user"` is NOT the user speaking - 174 tool_result blocks to 4 text
    blocks in one transcript. Correction candidates must be text blocks only.
  * `is_error` is unreliable: `error: unknown option '-S'` was observed with
    is_error false. Failures need a grounded retry or terminal relation instead.
"""
import json
import re
from dataclasses import dataclass, field

from .redact import redact

SKIP_TOOLS = {"TodoWrite", "Read", "Glob", "Grep", "ToolSearch", "NotebookRead"}
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Inspection is not work. Read/Glob/Grep are skipped as tools, but a session run
# in bypass mode does all of it through Bash instead, and those `cat`/`sed -n`
# commands were being distilled as accomplishments - the model reported files it
# had merely READ as things it had DONE.
READONLY_CMD = re.compile(
    r"^\s*(?:cat|head|tail|less|more|ls|ll|stat|file|wc|du|df|pwd|echo|printf|"
    r"grep|rg|find|fd|which|type|env|date|whoami|uname|tree|diff|cmp|md5sum|"
    r"sha256sum|jq|column|sort|uniq|command\s+\w+|sed\s+-n|awk|python3?\s+-c\s*['\"]"
    r"(?:[^'\"]*\b(?:print|json\.load|open\()[^'\"]*)['\"])\b"
)


def is_readonly_command(cmd: str) -> bool:
    """True only when EVERY segment of the command merely inspects state.

    Checking just the first segment would classify `head -5 f && git push` as
    read-only and silently drop a real push from the evidence corpus.
    """
    segments = [s for s in re.split(r"&&|\|\||;|\|", cmd) if s.strip()]
    if not segments:
        return False
    # `2>/dev/null` and `2>&1` discard or merge output; they write nothing.
    probe = re.sub(r"\d?>>?\s*/dev/null|\d?>&\d", " ", cmd)
    if re.search(r"[>]{1,2}\s*\S|--apply\b|\brm\b|\bmv\b|\bcp\b|\bmkdir\b|\btouch\b", probe):
        return False
    return all(READONLY_CMD.match(s) for s in segments)

# Wrappers the harness injects into user turns. None of these are the user.
META_PREFIXES = (
    "<system-reminder", "<local-command-", "<command-name>", "<command-message>",
    "<command-args>", "<task-notification", "<codex-activity", "<codex-handoff",
    "<user-prompt-submit-hook", "Caveat: The messages below were generated",
)

CORRECTION_MARKERS = re.compile(
    r"(?i)\b(no|not|wrong|actually|instead|don'?t|stop|never|isn'?t|shouldn'?t|"
    r"i said|that'?s not|incorrect|revert|undo|rather than|you missed|wait|"
    r"change .* back|why did|supposed to)\b"
)

# A dropped screenshot or a pasted path is not speech. These turns were being
# ranked as corrections and the model invented traps around filenames.
ATTACHMENT_ONLY = re.compile(
    r"^\s*(?:\[Image[^\]]*\]|\[Attachment[^\]]*\]|[~/][^\s]*\.[A-Za-z0-9]{1,5}|"
    r"[A-Za-z]:\\[^\s]*)\s*$"
)


def is_speech(text: str) -> bool:
    """A correction candidate must be something the user actually said."""
    t = text.strip()
    if not t or ATTACHMENT_ONLY.match(t):
        return False
    words = re.findall(r"[A-Za-z']{2,}", t)
    return len(words) >= 3

# Deliberately strict. An earlier, looser version matched the word "timeout" in
# "Monitor started (timeout 900000ms)" and flagged every healthy Monitor call as
# a failure. Bare "cannot"/"could not"/"timeout" are prose, not evidence.
ERROR_MARKERS = re.compile(
    r"(?im)^\s*(?:error|fatal|fail(?:ed|ure)?)\s*[:\]]"
    r"|\bTraceback \(most recent call last\)"
    r"|\b(?:[A-Za-z_]+Error|[A-Za-z_]+Exception)\b\s*:"
    r"|\bcommand not found\b"
    r"|\bpermission denied\b"
    r"|\bno such file or directory\b"
    r"|\bsegmentation fault\b"
    r"|^panic:"
    r"|\bconnection refused\b"
    r"|\bexit(?:ed with)? (?:code|status) [1-9]"
    r"|\bBLOCKED by\b"
)


@dataclass
class Candidate:
    cid: str
    kind: str                      # "human" | "action" | "failure"
    text: str
    anchor: str | None = None      # candidate id this one reacts to
    meta: dict = field(default_factory=dict)


@dataclass
class Result:
    conversation: list[str]
    actions: list[Candidate]
    corrections: list[Candidate]
    failures: list[Candidate]
    nominations: list[Candidate]
    candidates: dict[str, Candidate]
    edited_paths: list[str]
    redactions: int
    lifecycle_only: bool
    signals: dict = field(default_factory=dict)   # routing evidence, see resolve.rank
    timeline: list = field(default_factory=list)   # [cid, line] in transcript order

    def chunks(self, max_bytes: int) -> list[str]:
        """The whole segment in order, cut on line boundaries into pieces of at
        most max_bytes. v1 fitted ONE window and trimmed the oldest conversation
        first, so a long session's first hours never reached the model."""
        out, cur, size = [], [], 0
        for cid, line in self.timeline:
            if cid.startswith("f") and cid not in self.candidates:
                continue                      # an ungrounded failure was discarded
            b = len(line.encode()) + 1
            if cur and size + b > max_bytes:
                out.append("\n".join(cur))
                cur, size = [], 0
            if b > max_bytes:                 # one giant line: clip, never drop
                line = line.encode()[:max_bytes - 4].decode("utf-8", "ignore") + " …"
                b = len(line.encode()) + 1
            cur.append(line)
            size += b
        if cur:
            out.append("\n".join(cur))
        return out

    def render(self, max_actions: int | None = None,
               max_conv_bytes: int | None = None) -> str:
        # Conversation is the hog in a long session (many times the bytes of
        # all actions), and actions are what `done` items must cite for grounding. So
        # conversation is trimmed OLDEST-first and actions are kept last.
        conv = self.conversation
        if max_conv_bytes is not None:
            kept, total = [], 0
            for line in reversed(conv):
                total += len(line.encode()) + 1
                if total > max_conv_bytes and kept:
                    break
                kept.append(line)
            conv = list(reversed(kept))
        parts = []
        if self.nominations:
            parts += ["=== VAULT NOMINATIONS (main agent judged - keep verbatim) ==="]
            parts += [f"[{c.cid}] {c.text}" for c in self.nominations]
            parts += [""]
        parts += ["=== CONVERSATION ===", "\n".join(conv)]
        acts = self.actions if max_actions is None else self.actions[-max_actions:]
        if acts:
            parts += ["", "=== ACTIONS ==="] + [f"[{c.cid}] {c.text}" for c in acts]
        if self.corrections:
            parts += ["", "=== CORRECTIONS (human turns) ==="] + [
                f"[{c.cid}] (reacts to {c.anchor or 'nothing'}) {c.text}" for c in self.corrections]
        if self.failures:
            parts += ["", "=== FAILURES ==="] + [
                f"[{c.cid}] ({c.meta.get('why')}) {c.text}" for c in self.failures]
        return "\n".join(parts)


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


# Long "user" text blocks are usually the harness talking, not the user: skill
# bodies, the injected vault-boot block, pasted handoffs. Distilling these made
# the model summarize its own injected memory instead of the session.
INJECTED_SHAPES = re.compile(
    r"^(?:Base directory for this skill:"
    r"|---\s*\ntype:\s*vault-"
    r"|#\s*Memory index"
    r"|You are starting a fresh AI_Projects session"
    r"|Session saved\s+[-—]\s+wrote\s+\d+\s+nodes"
    r"|<command-name>)", re.I)

# A correction is a reaction, and reactions are short. Longer human turns still
# appear in CONVERSATION for context; they just are not lesson candidates.
MAX_CORRECTION_CHARS = 600

# Main-agent vault nominations. A subagent's report and a Codex manifest arrive
# as tool_result blocks, which are untrusted and dropped - so the only way a
# delegated finding becomes durable is for the main agent to judge it and
# restate it. This marker makes that restatement machine-capturable instead of
# hoping the distiller notices it buried in prose.
#
#   > vault: the auth check lives in middleware/session.ts, not the router
#   > vault(example-agency): supabase free tier caps at 2 projects
#
# Written by the main agent in its own visible output, so the user sees every
# nomination as it is made. Captured verbatim; no model grades it.
NOMINATION_RE = re.compile(
    r"^\s*(?:>\s*)?(?:⟢\s*)?vault(?:\(([A-Za-z0-9_\-]{1,60})\))?\s*:\s*(.+?)\s*$",
    re.I | re.M)


def _is_meta(text: str) -> bool:
    t = text.lstrip()
    if any(t.startswith(p) for p in META_PREFIXES):
        return True
    return bool(INJECTED_SHAPES.match(t))


def codex_records(rollout: list[dict]) -> list[dict]:
    """Adapt a Codex rollout to the transcript shape consumed by ``build``."""
    out, cwd = [], None

    def add(rtype, timestamp, content):
        if content:
            out.append({"type": rtype, "cwd": cwd, "timestamp": timestamp,
                        "message": {"content": content}})

    def text_content(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        return ""

    for rec in rollout:
        payload = rec.get("payload") or {}
        timestamp = rec.get("timestamp") or payload.get("timestamp")
        if rec.get("type") in ("session_meta", "turn_context"):
            cwd = payload.get("cwd") or cwd
            continue
        if rec.get("type") == "event_msg":
            item = payload.get("item") or {}
            if payload.get("type") == "item_completed" and item.get("type") == "UserMessage":
                text = text_content(item.get("content"))
                if "<skills_instructions>" not in text and not text.startswith("<environment_context>"):
                    add("user", timestamp, [{"type": "text", "text": text}])
            elif payload.get("type") == "task_complete":
                add("assistant", timestamp, [{"type": "text", "text": payload.get("last_agent_message", "")}])
            continue
        if rec.get("type") != "response_item":
            continue
        kind = payload.get("type")
        if kind in ("custom_tool_call", "function_call"):
            raw_input = payload.get("input", payload.get("arguments", {}))
            if isinstance(raw_input, str):
                try:
                    raw_input = json.loads(raw_input)
                except json.JSONDecodeError:
                    raw_input = {"input": raw_input}
            add("assistant", timestamp, [{"type": "tool_use", "id": payload.get("call_id", ""),
                                           "name": payload.get("name", kind), "input": raw_input or {}}])
        elif kind in ("custom_tool_call_output", "function_call_output"):
            add("user", timestamp, [{"type": "tool_result", "tool_use_id": payload.get("call_id", ""),
                                      "is_error": bool(payload.get("is_error")),
                                      "content": payload.get("output", payload.get("content", ""))}])
    return out


def read_records(path, start: int, end: int, expect_inode: int | None = None,
                 source: str = "claude") -> list[dict]:
    """Read whole newline-terminated records in [start, end). Verifies inode."""
    import os
    fd = os.open(str(path), os.O_RDONLY)
    try:
        if expect_inode is not None and os.fstat(fd).st_ino != expect_inode:
            raise RuntimeError(f"inode changed for {path}; transcript rotated")
        os.lseek(fd, start, os.SEEK_SET)
        raw = os.read(fd, max(0, end - start))
    finally:
        os.close(fd)
    out = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # a truncated tail record is skipped, never guessed at
    if source == "claude":
        return out
    if source == "codex":
        return codex_records(out)
    raise ValueError(f"unknown transcript source: {source}")


def last_record_boundary(path, start: int, limit: int) -> int:
    """Offset just past the last complete newline-terminated record at or before
    `limit`. A concurrent append is therefore never half-read."""
    import os
    size = os.path.getsize(str(path))
    end = min(limit, size)
    if end <= start:
        return start
    with open(str(path), "rb") as fh:
        fh.seek(start)
        chunk = fh.read(end - start)
    idx = chunk.rfind(b"\n")
    return start + idx + 1 if idx >= 0 else start


# Routing evidence. Paths in commands and the ssh target are how a session run
# from ~/AI_Projects reveals which project it actually worked on.
PATH_IN_CMD = re.compile(r"(?:/home/[\w.-]+|~|/srv|/mnt)/[^\s'\"`;|&<>(){}$*?]+")
SSH_ARG_FLAGS = set("oOpiIlFJLRDWbcEemQSw")


def ssh_hosts(cmd: str) -> list[str]:
    """Hosts named by `ssh`/`scp`-style invocations, e.g. `ssh -o X=y thor 'cmd'`."""
    out = []
    toks = cmd.replace(";", " ; ").replace("&&", " && ").replace("|", " | ").split()
    i = 0
    while i < len(toks):
        if toks[i] in ("ssh", "mosh"):
            j = i + 1
            while j < len(toks) and toks[j].startswith("-"):
                flag = toks[j]
                j += 2 if (len(flag) == 2 and flag[1] in SSH_ARG_FLAGS) else 1
            if j < len(toks):
                host = toks[j].split("@")[-1].strip("'\"")
                if re.fullmatch(r"[A-Za-z][\w.-]*", host):
                    out.append(host)
            i = j
        i += 1
    return out


# Real transcripts close a paste with the id repeated (`</pasted_content id="d3ec">`),
# and a long paste may have been clipped before its closing tag: strip to the end.
PASTED = re.compile(r"<pasted_content[^>]*>(.*?)(?:</pasted_content[^>]*>|\Z)", re.S)


def spoken(text: str) -> str:
    """What the user typed themselves: pasted blocks removed. A pasted web page or
    log is data, and must never be quotable as an instruction of their."""
    return PASTED.sub(" ", text)


def _collapse_pastes(text: str) -> str:
    return PASTED.sub(lambda m: f"[pasted {len(m.group(1))} chars: {_clip(m.group(1), 240)}]", text)


# The useful residue of a successful command: commit hashes, PR/artifact URLs,
# test tallies. Everything else in a tool result stays out (size, and tool
# output is untrusted text).
RESULT_SIGNAL = re.compile(
    r"\[[\w./-]+ [0-9a-f]{7,40}\]|https?://\S+|\b\d+ (?:passed|failed|tests?)\b|\bRan \d+ tests?\b"
    r"|\bPR #?\d+|\bpull/\d+|\bmerged?\b.*\b[0-9a-f]{7,40}\b|\bHTTP/?\S* [1-5]\d\d\b")


def _result_snippet(body: str) -> str:
    """The first signal line, redacted in full BEFORE it is clipped."""
    for line in body.splitlines()[:60]:
        if RESULT_SIGNAL.search(line):
            return _clip(redact(line)[0], 160)
    return ""


def build(records: list[dict]) -> Result:
    conversation: list[str] = []
    nominations: list[Candidate] = []
    actions: list[Candidate] = []
    corrections: list[Candidate] = []
    failures: list[Candidate] = []
    candidates: dict[str, Candidate] = {}
    edited_paths: list[str] = []
    redactions = 0
    n = 0
    last_action_cid: str | None = None
    # tool_use_id -> (cid, tool, target, seq) so a result can be tied to its call
    call_index: dict[str, tuple[str, str, str, int]] = {}
    pending_failures: list[tuple[Candidate, str, str, int]] = []
    calls: list[tuple[int, str, str]] = []            # (seq, tool, target)
    seq = 0
    from collections import Counter
    sig = {"edits": Counter(), "reads": Counter(), "bash_paths": Counter(),
           "hosts": Counter(), "cwds": Counter()}
    human_text: list[str] = []
    timeline: list[list[str]] = []
    tl_index: dict[str, int] = {}

    def tl(cid: str, line: str) -> None:
        tl_index[cid] = len(timeline)
        timeline.append([cid, line])

    def nid(prefix: str) -> str:
        nonlocal n
        n += 1
        return f"{prefix}{n:03d}"

    for rec in records:
        rtype = rec.get("type")
        if rec.get("cwd"):
            sig["cwds"][rec["cwd"]] += 1
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue

        for block in content:
            btype = block.get("type")

            if btype == "text":
                text = (block.get("text") or "").strip()
                if not text or _is_meta(text):
                    continue
                text, r = redact(text)
                redactions += r
                if rtype == "user" and rec.get("isMeta"):
                    continue    # an expanded slash command / skill body: the harness, not the user
                if rtype == "user":
                    cid = nid("h")
                    clipped = _clip(text, 2000)
                    eligible = is_speech(clipped) and len(clipped) <= MAX_CORRECTION_CHARS
                    said = is_speech(spoken(clipped))
                    cand = Candidate(cid, "human", clipped, anchor=last_action_cid,
                                     meta={"speech": eligible, "said": said})
                    candidates[cid] = cand
                    if said:
                        human_text.append(spoken(clipped))
                    if eligible:
                        corrections.append(cand)
                    conversation.append(f"\n[{cid} HUMAN] {cand.text}")
                    tl(cid, f"[{cid} USER] {_collapse_pastes(clipped)}")
                else:
                    for m in NOMINATION_RE.finditer(text):
                        slug_override, body = m.group(1), m.group(2).strip()
                        if len(body) < 8:
                            continue
                        ncid = nid("v")
                        ncand = Candidate(ncid, "nomination", _clip(body, 400),
                                          anchor=last_action_cid,
                                          meta={"slug": slug_override})
                        candidates[ncid] = ncand
                        nominations.append(ncand)
                    conversation.append(f"[CLAUDE] {_clip(text, 1200)}")
                    # Claude's own words carry most findings (tool output is
                    # dropped), so they get ids the distiller can cite. Clipped
                    # at 4000: the 1200 cut lost the end of long summaries.
                    ccid = nid("c")
                    candidates[ccid] = Candidate(ccid, "claude", _clip(text, 4000))
                    tl(ccid, f"[{ccid} CLAUDE] {_clip(text, 4000)}")

            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input") or {}
                if name in ("Read", "Glob", "Grep", "NotebookRead"):
                    target = inp.get("file_path") or inp.get("path") or ""
                    if target:
                        sig["reads"][str(target)] += 1
                elif name in EDIT_TOOLS and inp.get("file_path"):
                    sig["edits"][str(inp["file_path"])] += 1
                elif name == "Bash":
                    cmd = str(inp.get("command", ""))
                    # `cat`/`ls` of a path is looking, not working: count it as a read.
                    bucket = "reads" if is_readonly_command(cmd) else "bash_paths"
                    for pth in PATH_IN_CMD.findall(cmd):
                        sig[bucket][pth.rstrip("/.,:")] += 1
                    for host in ssh_hosts(cmd):
                        sig["hosts"][host] += 1
                if name in SKIP_TOOLS:
                    continue
                if name in EDIT_TOOLS:
                    target = str(inp.get("file_path", ""))
                    line = f"{name} {target}"
                    if target:
                        edited_paths.append(target)
                elif name == "Bash":
                    raw_cmd = str(inp.get("command", ""))
                    if is_readonly_command(raw_cmd):
                        continue                    # inspection, not work
                    # Redact the WHOLE command before clipping: a secret cut in
                    # half at the clip no longer matches any pattern.
                    raw_cmd, r = redact(raw_cmd)
                    redactions += r
                    target = _clip(raw_cmd, 160)
                    line = f"$ {target}"
                else:
                    dumped, r = redact(json.dumps(inp, default=str))
                    redactions += r
                    target = _clip(dumped, 120)
                    line = f"{name} {target}"
                line, r = redact(line)
                redactions += r
                cid = nid("a")
                cand = Candidate(cid, "action", line, meta={"tool": name, "target": target})
                candidates[cid] = cand
                actions.append(cand)
                tl(cid, f"[{cid}] {line}")
                last_action_cid = cid
                seq += 1
                calls.append((seq, name, target))
                if block.get("id"):
                    call_index[block["id"]] = (cid, name, target, seq)

            elif btype == "tool_result":
                cont = block.get("content")
                body = cont if isinstance(cont, str) else json.dumps(cont, default=str) if cont else ""
                explicit = bool(block.get("is_error"))
                if not explicit and not ERROR_MARKERS.search(body[:800]):
                    origin = call_index.get(block.get("tool_use_id", ""))
                    snip = _result_snippet(body) if origin and origin[1] == "Bash" else ""
                    if snip and origin[0] in tl_index:
                        snip, r = redact(snip)
                        redactions += r
                        timeline[tl_index[origin[0]]][1] += f"  => {snip}"
                    continue
                origin = call_index.get(block.get("tool_use_id", ""))
                if origin is None:
                    continue          # cannot ground it to a call; not a candidate
                acid, tool, target, cseq = origin
                body, r = redact(body)
                redactions += r
                cid = nid("f")
                cand = Candidate(cid, "failure", _clip(body, 300), anchor=acid,
                                 meta={"tool": tool, "target": target, "explicit": explicit})
                candidates[cid] = cand
                pending_failures.append((cand, tool, target, cseq))
                tl(cid, f"[{cid} FAILED {acid}] {_clip(body, 300)}")

    # Grounding is proportional to how trustworthy the signal is. An explicit
    # `is_error` is unambiguous and stands on its own. A regex-only match is an
    # inference and must be grounded by a reaction (the same tool used again
    # shortly after - a fix usually CHANGES the command, so demanding an
    # identical target would discard most real failures) or by being terminal.
    REACTION_WINDOW = 3
    for cand, tool, target, cseq in pending_failures:
        later = [(s, t, tg) for (s, t, tg) in calls if s > cseq]
        nearby = [(s, t, tg) for (s, t, tg) in later if s <= cseq + REACTION_WINDOW]
        terminal = not any(t == tool for (s, t, tg) in later)
        if cand.meta.get("explicit"):
            cand.meta["why"] = "explicit"
        elif any(t == tool and tg == target for (s, t, tg) in later):
            cand.meta["why"] = "retried-same"
        elif any(t == tool for (s, t, tg) in nearby):
            cand.meta["why"] = "reacted"
        elif terminal:
            cand.meta["why"] = "terminal"
        else:
            candidates.pop(cand.cid, None)
            continue
        failures.append(cand)

    said_any = any(c.kind == "human" and c.meta.get("said") for c in candidates.values())
    lifecycle_only = not said_any and not actions and not failures and not nominations

    sig["human_text"] = "\n".join(human_text)
    return Result(conversation, actions, corrections, failures, nominations,
                  candidates, edited_paths, redactions, lifecycle_only, sig, timeline)


def rank_corrections(result: Result) -> list[Candidate]:
    """Marker words RANK candidates, they never gate them - real corrections
    often carry no marker at all, e.g. "wdo what she asked for unless she asked
    you to be creative" (PLAN.md D).

    Score: a marker is the strongest signal, following an assistant action is the
    next (a correction reacts to something), brevity breaks ties - long turns are
    usually new instructions rather than corrections.
    """
    def score(c: Candidate) -> tuple:
        marker = 1 if CORRECTION_MARKERS.search(c.text) else 0
        reacts = 1 if c.anchor else 0
        return (-(marker * 2 + reacts), len(c.text))

    return sorted(result.corrections, key=score)
