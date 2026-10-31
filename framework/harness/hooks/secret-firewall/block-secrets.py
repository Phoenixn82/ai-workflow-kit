#!/usr/bin/env python3
"""Secret Firewall - PreToolUse hook for Claude Code (proposed 2026-10-12, audit item A5).

An accidental-disclosure GUARD, not a boundary (Claude has passwordless sudo;
OS ownership is the real wall). It denies a tool call that would put a secret
value into the model context, a transcript or a file:

- Bash, operand-level: a file counts as read only when it is an operand of a
  read verb (cat/less/jq/xxd/base64/sqlite3/...), the source of cp/mv/scp, an
  input redirect `< f`, an `@f` upload, a path inside inline interpreter code
  (`python3 -c`, `node -e`, interpreter heredocs) or inside an ssh / `bash -c`
  remote command. A SQLite `file:` URI (a sqlite3 or interpreter operand,
  connect('...')) counts as the local path it opens. Operands are normalised (a//b) and globs expand locally
  (executables skipped). Bare words in patterns, prose and flags (`grep tokens`,
  `--max_tokens`, `rg input_tokens`) never count.
- Literal secret shapes anywhere in the command, heredoc bodies included:
  provider key prefixes, private-key headers, JWTs, Authorization / api-key
  headers and KEY=<high-entropy literal>, URL passwords, and base64 that
  decodes to any of those.
- Environment dumps: bare env/printenv/export -p/declare -x/set,
  /proc/*/environ, print(os.environ), print(os.environ['X_TOKEN']) /
  console.log(process.env.X_KEY), and echo/printf of $SECRET_VAR or $(cmd)
  to the screen; printenv NAME only when NAME is secret-shaped.
- CLIs that print a stored token to the screen: gh auth token, gh auth status
  -t, rclone config show, gcloud print-access-token, secret-tool lookup, ...
- Safe plumbing stays allowed: X=$(cat f.token), a header built from
  $(cat f.token), echo "$T" | ssh/gh/wl-copy ..., printf "$T" > file,
  writing INTO a protected file.

Fail-CLOSED: bad JSON or any hook bug denies the call. Deploy gate:
AI_Projects/_tools/secret-firewall-corpus/run.py (corpus + legacy suite).

Registered for tools: Read, Edit, MultiEdit, Write, Grep, Glob, Bash, NotebookEdit.
`--self-test` runs a few inline asserts.
"""
import sys, os, json, re, fnmatch, posixpath, shlex, base64, time, glob, itertools, codecs

# Structured evidence (linux-relaunch T7 item 11): log AFTER this hook's own
# stdout/exit-code decision is already made. Import failure must never affect
# the security decision, hence the no-op fallback.
_HOOK_START_MS = int(time.time() * 1000)
_HOOK_SESSION_ID = "unknown"
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hook_event import log_hook_event
except Exception:
    def log_hook_event(*a, **k):
        pass

def norm(p):
    return str(p or "").replace("\\", "/").lower()

def basename(p):
    return posixpath.basename(norm(p).rstrip("/"))

ENV_OK_SUFFIX = (".example", ".sample", ".template", ".dist", ".md")
# Generic plaintext-credential words (2026-10-11): they count only as the LAST
# hyphen/underscore word of a filename that is not code or docs, so
# media-password, credentials, admin-token, app-tokens.json match and
# token-usage.csv, password_reset.py, docs/token-budget.md do not.
GENERIC_SENSITIVE_WORDS = {"password", "passwd", "pwd", "credential", "credentials", "token", "tokens", "apikey"}
CODE_DOC_EXT = {"md", "markdown", "rst", "adoc", "html", "css", "csv", "tsv", "log", "jsonl", "py", "sh", "bash",
                "zsh", "fish", "ts", "tsx", "js", "jsx", "mjs", "cjs", "rs", "go", "rb", "java", "kt", "c", "h",
                "cpp", "lua", "sql", "ipynb", "lock", "pyc"}
BARE_STRONG = {"credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
TG_TDATA = re.compile(r"/[^/]*telegram[^/]*/tdata(?:/|$)")  # Telegram Desktop's account store, on a normalised path

def is_protected_path(p):
    n = norm(p)
    if not n:
        return None
    b = basename(n)
    if "/.secrets/" in n or n.endswith("/.secrets"):
        return "the encrypted secret store (.secrets)"
    if b.endswith(".pub") or n == "/etc/passwd":
        return None  # public keys and the world-readable user list are safe
    if b == ".env" or (b.startswith(".env.") and not b.endswith(ENV_OK_SUFFIX)):
        return "an environment/secrets file (.env)"
    if b.endswith(".env") and not b.endswith(ENV_OK_SUFFIX):
        return "an environment/secrets file (*.env)"
    if fnmatch.fnmatch(b, "provider-keys*.json") or fnmatch.fnmatch(b, "*secrets*.local.json"):
        return "a provider-key vault file"
    if b == ".credentials.json" or b in ("token.json", "tokens.json") or fnmatch.fnmatch(b, "*apikey*.json") or fnmatch.fnmatch(b, "service-account*.json"):
        return "a sign-in / API-key / service-account file"
    if n.endswith("/.codex/auth.json") or (b == "auth.json" and "/.codex/" in n):
        return "the Codex auth/session file"
    if n.endswith("/.claude/mcp.json"):
        return "the user-level MCP config (embeds API keys)"
    if n.endswith(("/.aws/credentials", "/.npmrc", "/.docker/config.json", "/.kube/config")) or b in (".netrc", ".pgpass"):
        return "a cloud/registry credential file"
    if n.endswith("/gh/hosts.yml") or b == "rclone.conf":
        return "a CLI OAuth token file (gh hosts.yml / rclone.conf)"
    if re.fullmatch(r"/proc/[^/]+/environ", n):
        return "a process environment (/proc/*/environ)"
    if n in ("/etc/shadow", "/etc/gshadow") or re.fullmatch(r"cookies(\.(db|sqlite3?))?|login data", b):
        return "a password-hash / browser cookie store"
    if b == ".cliproxy-client.key":
        return "the CLIProxy client key"
    if b in ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"):
        return "an SSH private key"
    if b.endswith((".pem", ".pfx", ".p12")):
        return "a private key / certificate file"
    if b.endswith((".token", ".key")):
        return "a bare token / key file"
    if b.endswith((".secret", ".password", ".pwd", ".cred", ".creds")):
        return "a bare password / secret file"
    if fnmatch.fnmatch(b, "client_secret*.json"):
        return "a Google OAuth client-secret file"
    # audit S14: Telegram account logins, stronger than a bot token. Only a *.session (plus its SQLite sidecars) under a
    # telegram path (tg-admin's ~/.config/telegram/user.session, Telethon), never other apps' "session" files; and
    # Telegram Desktop's whole tdata store (Linux, flatpak/snap, macOS "Telegram Desktop", Windows).
    if "telegram" in n and re.search(r"\.session(?:-journal|-wal|-shm)?$", b):
        return "a Telegram account session (user.session)"
    if TG_TDATA.search(n):
        return "the Telegram Desktop account store (tdata)"
    core = b[1:] if b.startswith(".") and not b.startswith("..") else b
    stem, _, ext = core.rpartition(".") if "." in core else (core, "", "")
    if ext not in CODE_DOC_EXT and re.split(r"[-_]", stem)[-1] in GENERIC_SENSITIVE_WORDS:
        return "a plaintext password / credential / token file"
    return None

BRACE = re.compile(r"\{([^{},]*,[^{}]*)\}")
BRACE_SEQ = re.compile(r"\{(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])(?:\.\.(-?\d+))?\}")
BRACE_CAP = 1024

def brace_seq(m):
    """{1..3} {01..10} {a..e} {1..9..2} -> list of strings, or None when not a valid bash sequence."""
    a, b, step = m.group(1), m.group(2), abs(int(m.group(3) or 1)) or 1
    if a.isalpha() != b.isalpha():
        return None
    if a.isalpha():
        lo, hi = ord(a), ord(b)
        return [chr(c) for c in range(lo, hi + (1 if hi >= lo else -1), step if hi >= lo else -step)]
    lo, hi = int(a), int(b)
    n = abs(hi - lo) // step + 1
    if n > BRACE_CAP:
        return [""] * (BRACE_CAP + 1)  # only the count matters: the caller denies
    w = max(len(a), len(b)) if re.match(r"-?0\d", a) or re.match(r"-?0\d", b) else 0
    return [str(x).zfill(w) for x in range(lo, hi + (1 if hi >= lo else -1), step if hi >= lo else -step)]

def braces(tok, cap=BRACE_CAP):
    """a{b,c}d -> [abd, acd], a{1..3} -> [a1, a2, a3]; innermost first. None when the
    expansion passes `cap` variants (the caller denies rather than test leftovers as literals)."""
    out, todo = [], [tok]
    while todo:
        if len(out) + len(todo) > cap:
            return None
        t = todo.pop()
        m = BRACE.search(t)
        alts = m and m.group(1).split(",")
        if not m:
            m = BRACE_SEQ.search(t)
            alts = m and brace_seq(m)
        if not alts:
            out.append(t)
            continue
        todo.extend(t[:m.start()] + alt + t[m.end():] for alt in alts)
    return out

PCT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
HOST_NAME = re.compile(r"[A-Za-z0-9.\-]+")

def sqlite_uri_path(tok):
    """A SQLite filename -> the local path SQLite opens (audit S14 r2; sqlite.org/uri.html, sqlite3ParseUri):
    file:PATH, file:///PATH and file://localhost/PATH lose the scheme, the ?query and the #fragment; %XX is decoded
    (UTF-8) and %00 ends the path. A plain filename comes back unchanged; a literal other host (SQLite refuses the
    URI) gives None, never a local path. An authority that is not a literal host name ($X, ${X}, $(cmd), `cmd`,
    __SUBn__) counts as local: unset or empty, the shell turns file://$X/PATH into file:///PATH (S14 r2 review).
    urllib.parse is not used: its import costs ~20 ms per hook call."""
    if tok[:5].lower() != "file:":
        return tok
    rest = tok[5:]
    if rest.startswith("//"):
        auth = rest[2:].split("/", 1)[0]
        if auth.lower() != "localhost" and HOST_NAME.fullmatch(auth):
            return None
        rest = rest[2 + len(auth):]
    rest = re.split(r"[?#]", rest, maxsplit=1)[0].split("%00", 1)[0]
    return PCT_RUN.sub(lambda m: bytes.fromhex(m.group(0).replace("%", "")).decode("utf-8", "replace"), rest)

def check_path(tok, cwd="", uri=False):
    """One command operand -> reason or None. Bare words (no '/' and no '.')
    only match unmistakable names; URLs never match. Brace forms are tried
    alternative by alternative (cat ~/.codex/auth.js{o,x}n). uri: the operand
    is a SQLite filename (sqlite3's), so a local file: URI is screened as the
    path it names, after brace expansion, as bash expands before sqlite3 parses."""
    if not tok or ("://" in tok and not (uri and tok[:5].lower() == "file:")):
        return None
    if "{" in tok and ("," in tok or ".." in tok):
        variants = braces(tok)
        if variants is None:
            return "a path whose brace expansion is too large to screen (over %d variants)" % BRACE_CAP
        for t in variants:
            hit = check_path1(t, cwd, uri)
            if hit:
                return hit
        return None
    return check_path1(tok, cwd, uri)

def check_path1(tok, cwd="", uri=False):
    tok = os.path.expandvars(tok)  # $HOME/.env -> /home/u/.env
    if uri:  # file:'s authority is checked after $HOME expands (file://$HOME/...)
        tok = sqlite_uri_path(tok)
        if not tok:  # another host, or no path at all (file:?mode=memory)
            return None
    pathish = "/" in tok or "\\" in tok or "." in tok  # a backslash too: 'C:\Users\u\...\tdata\key_datas' has no dot
    if pathish:
        hit = is_protected_path(tok)
    else:
        hit = "an SSH private key / credentials file" if tok.lower() in BARE_STRONG else None
    if hit:
        return hit
    # review 2026-10-12: ~/.claude//mcp.json (unnormalised) and globs (cat ~/.codex/auth*) bypassed.
    # A bare word is still never matched by name, only through a glob match or a symlink. Globs expand
    # only locally (cwd set; ssh remote commands pass cwd="") and skip executables (bin/*-password scripts).
    # A bare word inside Telegram Desktop's tdata does match (S14 review: `cd .../tdata && xxd key_datas`): its files
    # have no dot, and everything in it is the account store.
    p = os.path.normpath(os.path.join(cwd or "/", os.path.expanduser(tok)))
    globbed = bool(cwd and re.search(r"[*?[]", tok))
    for g in itertools.islice(glob.iglob(p), 1000) if globbed else (p,):
        if globbed and os.path.isfile(g) and os.access(g, os.X_OK):
            continue
        real = os.path.realpath(g)
        hit = ((pathish or globbed or TG_TDATA.search(norm(g))) and is_protected_path(g)) or (real != g and is_protected_path(real))
        if hit:
            return hit
    return None

# ---- literal secret values ---------------------------------------------------
SECRET_SHAPES = [re.compile(x) for x in (
    r"sk-ant-[A-Za-z0-9_\-]{20,}", r"\bsk-[A-Za-z0-9_\-]{32,}", r"\bsk_live_[A-Za-z0-9]{24,}",
    r"\bAIza[0-9A-Za-z_\-]{35}\b", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", r"\bgithub_pat_[A-Za-z0-9_]{60,}\b", r"\bglpat-[A-Za-z0-9_\-]{20,}",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}", r"\bnpm_[A-Za-z0-9]{36}\b", r"\bsbp_[a-f0-9]{40,}\b",
    r"\bfc-[A-Za-z0-9]{20,}\b", r"\bgsk_[A-Za-z0-9]{40,}\b", r"\bhf_[A-Za-z0-9]{30,}\b",
    r"\bxai-[A-Za-z0-9]{40,}\b", r"\bpplx-[A-Za-z0-9]{32,}\b", r"\bcsk-[A-Za-z0-9]{30,}\b",
    r"\bnvapi-[A-Za-z0-9_\-]{30,}\b", r"(?<!\d)\d{8,10}:[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])",
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"(?i)\bauthorization['\"]?\s*:\s*['\"]?(?:bearer|basic|token|bot)\s+(?![$<{])(?=[A-Za-z._~+/=\-]*\d)[A-Za-z0-9._~+/=\-]{16,}",  # needs a digit: "Bearer wrong-token-here" is a test
    r"[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:(?![$<{])[^/\s@'\"]{6,}@",
)]
ASSIGN = re.compile(r"(?i)\b[A-Z0-9_\-]*(?:API[_\-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASS|AUTH[_\-]?KEY)[A-Z0-9_\-]*"
                    r"['\"]?\s*[=:]\s*['\"]?([A-Za-z0-9+/_.~\-]{16,})")
B64_RUN = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")

def looks_random(v):
    return bool(re.search(r"[a-z]", v) and re.search(r"[A-Z]", v) and re.search(r"\d", v)) or bool(re.fullmatch(r"[0-9a-f]{32,}", v))

def literal_secret(text, depth=0):
    if any(rx.search(text) for rx in SECRET_SHAPES) or any(looks_random(m.group(1)) for m in ASSIGN.finditer(text)):
        return "contains a literal secret value (key / token / password shape)"
    for m in B64_RUN.finditer(text) if depth == 0 else ():
        s = m.group(0)
        try:
            dec = base64.b64decode(s + "=" * (-len(s) % 4), validate=True).decode("ascii")
        except Exception:
            continue
        if literal_secret(dec, 1):
            return "contains a base64-encoded secret value"
    return None

# ---- environment dumps / inline code ---------------------------------------
ENV_CODE = re.compile(r"\b(?:print|pprint|log|dumps?|stringify|entries|repr)\s*\(\s*(?:dict\(\s*)?(?:os\.environ|process\.env)\s*[),]")
# Only a path handed straight to a file-opening call counts: edit scripts that
# merely mention a secret path inside a replacement string are not reads.
READ_CALL = re.compile(r"""(?:\bopen|\bPath|readFileSync|readFile|expanduser|fopen|\bconnect)\s*\(\s*[rbfu]?['"]([^'"\n]+)['"]"""
                       r"""|Path\.home\(\)\s*/\s*['"]([^'"\n]+)['"]""")
VAR_REF = re.compile(r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)(?!:?\+)[^}]*\})")  # ${X:+set} and ${#X} print no value
SECRET_VAR_NAME = re.compile(r"(?i)(?:^|_)(?:api_?key|key|token|secret|password|passwd|pass|pat|credentials?)(?:_|$)")
ENV_VAR_PRINT = re.compile(r"""\b(?:print|pprint|puts|console\.(?:log|error|info))\s*\(?\s*(?:os\.environ(?:\.get)?\s*[\[(]\s*['"]"""
                           r"""|os\.getenv\s*\(\s*['"]|ENV\s*\[\s*['"]|process\.env(?:\.|\s*\[\s*['"]))(\w+)""")
TOKEN_CLI = re.compile(r"(?:gh auth token|gh auth status .*(?:--show-token|(?<!\S)-t)|rclone config show|secret-tool lookup"
                       r"|gcloud auth (?:application-default )?print-(?:access|identity)-token|aws configure export-credentials"
                       r"|git credential fill)\b")
DECRYPT_HINT = re.compile(r"(?ix)unprotect-?data|protecteddata|unprotect-secret"
                          r"|\bsecret(?:\.ps1)?\s+(?:get|show|reveal|export|run|decrypt|dump)\b")

def screen_code(code):
    if ENV_CODE.search(code):
        return "dumps the environment from inline code"
    for m in ENV_VAR_PRINT.finditer(code):
        if SECRET_VAR_NAME.search(m.group(1)):
            return "prints a secret environment variable (" + m.group(1) + ") from inline code"
    for m in READ_CALL.finditer(code):
        p = m.group(1) or m.group(2)
        if m.group(1) and m.group(0).startswith("connect"):  # sqlite3.connect('file:...?mode=ro', uri=True), audit S14 r2
            p = sqlite_uri_path(p)
            if not p:
                continue
        hit = is_protected_path(p) or (m.group(2) and is_protected_path("~/" + p))
        if hit:
            return "reads " + hit + " from inline code"
    return None

# ---- Bash ----------------------------------------------------------------------
READ_VERBS = {"cat", "tac", "nl", "less", "more", "most", "bat", "batcat", "head", "tail", "sed", "awk", "gawk",
              "strings", "xxd", "hexdump", "od", "base64", "base32", "basenc", "jq", "yq", "grep", "egrep", "fgrep",
              "rg", "ag", "ack", "cut", "sort", "uniq", "diff", "vim", "vi", "nvim", "nano", "view", "ed", "ex",
              "sqlite3", "zcat", "dd", "openssl", "gpg", "python", "python3", "node", "deno", "bun", "ruby", "perl",
              "cp", "mv", "paste", "rev", "fold", "column", "pr", "comm", "join", "fmt", "expand", "unexpand", "iconv",
              "bzcat", "xzcat", "zstdcat", "lzcat", "zless", "zmore"}
COPY_VERBS = {"cp", "mv"}  # only sources count; writing INTO a secret file, scp/rsync/install deploys are plumbing
GREPS = {"grep", "egrep", "fgrep", "rg", "ag", "ack"}
PATTERN_VERBS = GREPS | {"sed", "awk", "gawk", "jq", "yq"}
# output of echo/printf (or env) only reaches the model when it ends the pipeline or feeds one of these
FILTERS = {"grep", "egrep", "rg", "head", "tail", "cat", "tee", "base64", "xxd", "od", "tr", "cut", "sed", "awk",
           "sort", "uniq", "jq", "less", "more", "rev", "fold", "column", "nl"}
INTERP = {"python", "python3", "node", "deno", "bun", "ruby", "perl"}
SHELLS = {"bash", "sh", "zsh", "dash"}
WRAPPERS = {"sudo", "doas", "nice", "nohup", "timeout", "command", "exec", "time", "stdbuf", "xargs", "env",
            "ionice", "watch"}
# reserved words that open a compound command: `do cat f`, `then cat f`, `{ cat f; }` still run cat
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "{", "!"}
WRAP_ARG = {"-u", "-g", "-I", "-s", "-n", "-k", "-C"}
SSH_ARG_FLAGS = set("bcDEeFIiJLlmOopQRSWw")
SECRET_WORD = re.compile(r"(?i)pass(?:word|wd)?|token|secret|api.?key|credential")
SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
HEREDOC_HEADER = re.compile(r"<<-?\s*(?:'([A-Za-z_]\w*)'|\"([A-Za-z_]\w*)\"|([A-Za-z_]\w*))")

def strip_heredoc_bodies(cmd):
    """Blank heredoc bodies (doc prose that merely mentions a secret path) and
    return the bodies fed to a shell / ssh / interpreter, which are code."""
    out, code = cmd, []
    for m in HEREDOC_HEADER.finditer(cmd):
        delim = m.group(1) or m.group(2) or m.group(3)
        body_start = cmd.find("\n", m.end())
        if body_start == -1:
            continue
        body_start += 1
        close = re.search(r"(?m)^\t*" + re.escape(delim) + r"\s*$", cmd[body_start:])
        if not close:
            continue
        body_end = body_start + close.start()
        head = re.split(r"[;&|(\n]", cmd[:m.start()])[-1].split()
        head = [w for w in head if not w.startswith("-") and w not in WRAPPERS and "=" not in w]
        verb = posixpath.basename(head[0]) if head else ""
        if verb in SHELLS or verb == "ssh" or verb in INTERP:
            code.append((verb not in INTERP, cmd[body_start:body_end]))
        out = out[:body_start] + re.sub(r"[^\n]", " ", out[body_start:body_end]) + out[body_end:]
    return out, code

ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'")

def ansi_c(m):
    try:
        return shlex.quote(codecs.decode(m.group(1), "unicode_escape"))
    except Exception:
        return shlex.quote(m.group(1))

def tokenize(text):
    text = ANSI_C.sub(ansi_c, text).replace('$"', '"')  # $'au\x74h.json' -> 'auth.json'; $"au"th -> "au"th
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
        return list(lex)
    except ValueError:  # ponytail: unbalanced quotes -> crude split, quotes stripped
        return [t.strip("'\"") for t in re.findall(r"[^\s;&|()<>]+|[;&|()<>]+|\n", text)]

def split_commands(tokens):
    """-> pipelines: [[(words, in_files, redirected_out), ...], ...]"""
    pipes, cur, words, ins, red, skip = [], [], [], [], False, None
    for t in tokens:
        if skip:
            if skip == "in":
                ins.append(t)
            skip = None
            continue
        if t and set(t) <= set("();<>|&\n"):
            if ("<" in t or ">" in t) and "(" not in t and ")" not in t:
                skip = "in" if t == "<" else "out"
                red = red or ">" in t
                continue
            cur.append((words, ins, red))
            words, ins, red = [], [], False
            if t != "|":
                pipes.append(cur)
                cur = []
            continue
        words.append(t)
    cur.append((words, ins, red))
    pipes.append(cur)
    return pipes

def strip_wrappers(words):
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"[A-Za-z_]\w*\+?=", w) or w in KEYWORDS:
            i += 1
            continue
        if posixpath.basename(w) not in WRAPPERS:
            break
        env = posixpath.basename(w) == "env"
        i += 1
        while i < len(words) and (words[i].startswith("-") or re.match(r"[A-Za-z_]\w*=|[\d.]+[smhd]?$|\{\}$", words[i])):
            i += 2 if words[i] in WRAP_ARG else 1
        if env and i >= len(words):
            return None  # bare `env [-0] [X=y]` prints the environment
    return words[i:]

def quiet_grep(verb, flags):
    """grep -l/-L/-c/-q print names, counts or nothing -- never file content."""
    return verb in GREPS and any(re.fullmatch(r"-[a-zA-Z]*[lLcq][a-zA-Z]*", f) or f in (
        "--files-with-matches", "--files-without-match", "--count", "--quiet") for f in flags)

def printed(nxt):
    """Can output piped into `nxt` (wrapper-stripped words; None = end of pipeline) reach the model?"""
    if not nxt:
        return True
    v = posixpath.basename(nxt[0])
    return v in FILTERS and not quiet_grep(v, [a for a in nxt[1:] if a.startswith("-")])

def screen_cmd(words, ins, red, nxt, subs, cwd):
    for f in ins:
        hit = check_path(f, cwd)
        if hit:
            return "reads " + hit + " via input redirect"
    for w in words:
        if "@" in w:
            hit = check_path(w.rsplit("@", 1)[1], cwd)
            if hit:
                return "uploads " + hit
    words = strip_wrappers(words)
    dump = "enumerates environment variables (may contain secret tokens)"
    if words is None:
        return dump if red or printed(nxt) else None
    if not words:
        return None
    verb, args = posixpath.basename(words[0]), words[1:]
    flags = [a for a in args if a.startswith("-")]
    names = [a for a in args if not a.startswith("-")]
    if not red and printed(nxt) and TOKEN_CLI.match(" ".join([verb] + args)):
        return "prints a CLI auth token / credential to the screen"
    if (red or printed(nxt)) and ((verb == "printenv" and (not names or any(SECRET_VAR_NAME.search(a) for a in names))) or (verb == "export" and set(args) <= {"-p"}) or (verb == "set" and not args)
            or (verb in ("declare", "typeset") and args and len(flags) == len(args) and re.search(r"[xp]", "".join(flags)))):
        return dump
    if verb in SHELLS:
        for j, a in enumerate(args[:-1]):
            if re.fullmatch(r"-[a-z]*c[a-z]*", a):
                return screen_bash(unsub(args[j + 1], subs), cwd)
        return None
    if verb == "eval":  # eval joins its args and reparses them as a command
        # ponytail: CEILING -- only a literal eval is caught. Arbitrary indirection
        # stays a guard gap by design: eval/base64 -d of a computed string
        # (eval "$(printf ...)", echo <b64>|base64 -d|sh), a path built by $(...),
        # or a wrapper script/installer that reads the secret internally (the
        # firewall cannot see inside an arbitrary executable). A guard, not a
        # boundary (Claude has sudo; OS ownership is the real wall).
        return screen_bash(unsub(" ".join(args), subs), cwd)
    if verb == "ssh":
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if len(args[j]) == 2 and args[j][1] in SSH_ARG_FLAGS else 1
        return screen_bash(unsub(" ".join(args[j + 1:]), subs), "") if len(args) > j + 1 else None
    if verb in ("echo", "printf") and not red and printed(nxt):
        for a in args:
            for k in re.findall(r"__SUB(\d+)__", a):
                r = screen_bash(unsub(subs[int(k)], subs), cwd)
                if r:
                    return "prints the output of a command that " + r
            for m in VAR_REF.finditer(a):
                name = m.group(1) or m.group(2)
                if SECRET_VAR_NAME.search(name):
                    return "prints a secret environment variable ($" + name + ")"
        return None
    if verb not in READ_VERBS or quiet_grep(verb, flags):
        return None
    ops, j = [], 0
    while j < len(args):
        a = args[j]
        if verb in INTERP and re.fullmatch(r"-[a-zA-Z]*[ceEp]|--eval|--print", a) and j + 1 < len(args):
            r = screen_code(unsub(args[j + 1], subs))
            if r:
                return r
            j += 2
            continue
        if a.startswith("-"):
            if "=" in a:
                ops.append(a.split("=", 1)[1])
        else:
            ops.append(a.split("=", 1)[1] if verb == "dd" and "=" in a else a)
        j += 1
    pattern = None
    if verb in PATTERN_VERBS and ops and not any(f in ("-e", "-f", "--regexp", "--file") or f.startswith(("--regexp=", "--file=")) for f in flags):
        pattern, ops = ops[0], ops[1:]
    if verb in COPY_VERBS:
        ops = ops[:-1]
    uri = verb == "sqlite3" or verb in INTERP  # python3 -m sqlite3 'file:...': Debian's libsqlite3 parses URIs anyway
    for o in ops:
        hit = check_path(o, cwd, uri)
        if hit:
            return "reads " + hit
    home = os.path.expanduser("~")
    roots = {home, "/", "/etc", "/root"} | {home + d for d in ("/.config", "/.ssh", "/.aws", "/.codex", "/.claude", "/.local/share")}
    if verb in ("grep", "egrep", "rg", "ag", "ack") and SECRET_WORD.search(pattern or " ".join(args)) and \
            any(os.path.normpath(os.path.expanduser(o)) in roots for o in ops):
        return "greps a secrets-bearing directory for a secret word"
    return None

def unsub(s, subs):
    while re.search(r"__SUB\d+__", s):
        s = re.sub(r"__SUB(\d+)__", lambda m: "$(" + subs[int(m.group(1))] + ")", s)
    return s

ASSIGN_TOK = re.compile(r"^([A-Za-z_]\w*)(\+?)=(.*)$", re.S)
PLAIN_REF = re.compile(r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\})")
OPS = set("();<>|&\n")
CMD_START = {"do", "then", "else", "elif", "{", "!"}

def resolve_vars(tokens):
    """Resolve simple same-command `VAR=literal; ... $VAR` substitutions so a
    secret path assembled through a shell variable (f=~/.codex/auth; cat $f.json)
    is screened. One in-order pass: each use resolves against the value assigned
    so far (f=SECRET; cat $f; f=x reads SECRET), `f+=x` appends, and
    `for f in a b` binds f to every listed word, substituted as the brace list
    {a,b} that check_path tries alternative by alternative. A value holding a
    command substitution (__SUB) becomes unknown and its uses stay unexpanded,
    so nothing executes; genuine runtime-only vars simply stay unexpanded."""
    varmap, out, prev, i = {}, [], None, 0
    def sub(t):
        def one(m):  # an empty value expands to ""; an unknown (None) or unseen name stays as written
            v = varmap.get(m.group(1) or m.group(2))
            return m.group(0) if v is None else v
        return PLAIN_REF.sub(one, t) if "$" in t else t
    while i < len(tokens):
        t = tokens[i]
        m = ASSIGN_TOK.match(t)
        if m:
            k, plus, v = m.group(1), m.group(2), sub(m.group(3))
            old = varmap.get(k, "") if plus else ""
            varmap[k] = None if old is None or "__SUB" in v else old + v
            out.append(k + plus + "=" + v)
        elif (t == "for" and (prev is None or set(prev) <= OPS or prev in CMD_START) and i + 2 < len(tokens)
              and tokens[i + 2] == "in" and re.fullmatch(r"[A-Za-z_]\w*", tokens[i + 1])):
            j, vals = i + 3, []
            while j < len(tokens) and tokens[j] != "do" and not set(tokens[j]) <= OPS:
                vals.append(sub(tokens[j]))
                j += 1
            varmap[tokens[i + 1]] = (None if not vals or any("__SUB" in v for v in vals)
                                     else vals[0] if len(vals) == 1 else "{" + ",".join(vals) + "}")
            out += tokens[i:i + 3] + vals
            prev, i = out[-1], j
            continue
        else:
            out.append(sub(t))
        prev, i = t, i + 1
    return out

def chdir(args, cwd):
    """cwd after `cd`/`pushd args` (later relative operands resolve against it); unknown targets keep cwd."""
    args = [a for a in args if not a.startswith("-") or a == "-"]
    target = os.path.expandvars(os.path.expanduser(args[0] if args else "~"))
    if target == "-" or "$" in target or "__SUB" in target:
        return cwd
    return os.path.normpath(os.path.join(cwd or "/", target))

def screen_bash(cmd, cwd=""):
    r = literal_secret(cmd) or ("\\\n" in cmd and literal_secret(cmd.replace("\\\n", "")))
    if r:
        return r
    scan, code = strip_heredoc_bodies(cmd)
    for shell, body in code:
        r = screen_bash(body, cwd) if shell else screen_code(body)
        if r:
            return r
    if re.search(r"(?i)[/\\]\.secrets([/\\]|\b)", scan):
        return "accesses the encrypted secret store (.secrets)"
    if DECRYPT_HINT.search(scan):
        return "attempts to decrypt secrets via the secret helper"
    subs = []
    def keep(m):
        subs.append(m.group(1) if m.group(1) is not None else m.group(2))
        return "__SUB%d__" % (len(subs) - 1)
    prev, scan = None, scan.replace("__SUB", "__SuB")  # a literal `__SUB0__` in the input made unsub() loop forever
    while prev != scan:  # innermost $(...) first; contents screened only when printed
        prev, scan = scan, SUBST.sub(keep, scan)
    tokens = resolve_vars(tokenize(scan.replace("\\\n", "")))  # bash deletes backslash-newline, joining the word
    for pipe in split_commands(tokens):
        for i, (words, ins, red) in enumerate(pipe):
            nxt = strip_wrappers(pipe[i + 1][0]) if i + 1 < len(pipe) else None
            r = screen_cmd(words, ins, red, nxt, subs, cwd)
            if r:
                return r
            if words and words[0] in ("cd", "pushd"):
                cwd = chdir(words[1:], cwd)
    return None

def deny(reason):
    msg = ("BLOCKED by secret-firewall: this call " + reason + ". API keys/"
           "secrets must never be read into the model context. If a secret "
           "value is genuinely needed, ask the user to provide/rotate it "
           "directly, or pipe it where it is used without printing it "
           "(X=$(cat f) && cmd \"$X\", or `| ssh host 'install -m600 /dev/stdin f'`).")
    out = {
        "decision": "block",          # legacy key (older CLI builds)
        "reason": msg,
        "hookSpecificOutput": {       # current documented key
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": msg,
        },
    }
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    sys.stderr.write(msg)
    log_hook_event("block-secrets", "PreToolUse", 0, _HOOK_START_MS, "deny:" + reason[:60], _HOOK_SESSION_ID)
    sys.exit(0)

def main():
    global _HOOK_SESSION_ID
    data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError("payload is not a JSON object")
    _HOOK_SESSION_ID = data.get("session_id") or "unknown"
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if tool in ("Bash", "PowerShell"):
        r = screen_bash(str(ti.get("command", "")), str(data.get("cwd") or ""))
        if r:
            deny(r)
    else:
        keys = ("file_path", "path", "notebook_path") + (("pattern",) if tool == "Glob" else ()) + (("glob",) if tool == "Grep" else ())
        for key in keys:
            val = ti.get(key)
            if val:
                hit = is_protected_path(val) or is_protected_path(os.path.realpath(os.path.expanduser(str(val))))
                if hit:
                    deny("targets " + hit)
    log_hook_event("block-secrets", "PreToolUse", 0, _HOOK_START_MS, "allow", _HOOK_SESSION_ID)
    sys.exit(0)

def self_test():
    assert screen_bash("cat ~/.codex/auth.json") and screen_bash("env | sort") and screen_bash("less ~/.env")
    assert screen_bash("ssh heimdall 'cat /etc/se/host.env'") and screen_bash('echo "$(cat ~/.env)"')
    assert not screen_bash("grep -c tokens report.md") and not screen_bash("head -5 token-usage.csv")
    assert not screen_bash('X=$(cat ~/.config/se/worker.token) && curl -H "Authorization: Bearer $X" https://x.test')
    assert is_protected_path("/home/u/.config/gh/hosts.yml") and not is_protected_path("docs/token-budget.md")
    assert screen_bash("cat ~/.claude//mcp.json") and screen_bash("cat /proc/self/env*ron", "/tmp") and screen_bash("paste ~/.env")
    assert screen_bash("gh auth token") and not screen_bash("printenv PATH") and not screen_bash("echo $(echo __SUB0__)")
    # evasion classes (audit item secret-firewall-evasions): quote-split, var-concat, brace, ANSI-C, eval
    assert screen_bash('cat ~/.codex/au""th.json') and screen_bash("f=~/.codex/auth; cat $f.json")
    assert screen_bash("d=~/.codex; f=auth.json; cat $d/$f") and screen_bash("cat ~/.codex/auth.js{o,x}n")
    assert screen_bash("cat ~/.codex/$'au\\x74h.json'") and screen_bash("eval cat ~/.codex/auth.json")
    # benign look-alikes must stay allowed
    assert not screen_bash('cat no""tes.md') and not screen_bash("f=~/notes; cat $f.md")
    assert not screen_bash("cat report.{md,txt}") and not screen_bash("eval ls ~/.codex")
    # normalisation gaps (review 2026-10-12 fw2): order, +=, $"..", backslash-newline, {x..y}, cap, cd, for
    assert screen_bash("f=~/.codex/auth.json; cat $f; f=x") and not screen_bash("f=~/.codex/auth.json; f=notes.md; cat $f")
    assert screen_bash("e=; cat ~/.e${e}nv") and screen_bash("f=~/.codex/; f+=auth.json; cat $f") and screen_bash('cat ~/.codex/$"au"th.json')
    assert screen_bash("cat ~/.codex/au\\\nth.json") and screen_bash("cat ~/.ssh/id_ed{25518..25520}") and screen_bash("cat ~/.codex/aut{g..i}.json")
    assert screen_bash("cat ~/x/{a,b,c,d,e,f}{a,b,c,d,e,f}{a,b,c,d,e,f}{a,b,c,d,e,f}") and not screen_bash("cat log{1..200}.txt")
    assert screen_bash("cd ~/.codex && cat auth.json") and not screen_bash("cd ~/.codex && cat config.toml")
    assert screen_bash("for f in notes.md ~/.codex/auth.json; do cat $f; done") and not screen_bash("for f in a.md b.md; do cat $f; done")
    # Telegram account credentials (audit S14): tg-admin's session + SQLite sidecars, Telegram Desktop's tdata
    assert screen_bash("sqlite3 ~/.config/telegram/user.session 'select hex(auth_key) from sessions'")
    assert screen_bash("cp ~/.config/telegram/user.session-wal /tmp/x") and screen_bash("cd ~/.config/telegram && cat user.session")
    assert screen_bash("cp -r ~/.local/share/TelegramDesktop/tdata /tmp/td")
    assert is_protected_path("/Users/u/Library/Application Support/Telegram Desktop/tdata/key_datas")
    assert not is_protected_path("/home/u/.cache/ms-playwright/daemon/x/bakeoff-1.session")
    assert not is_protected_path("/home/u/proj/telegram/session.py") and not screen_bash("cat ~/.local/share/TelegramDesktop/log.txt")
    # review: a bare tdata file after cd, a backslash path with no dot, Python's sqlite3.connect
    assert screen_bash("cd ~/.local/share/TelegramDesktop/tdata && xxd key_datas") and not screen_bash("cd ~/proj/tdata && cat notes")
    assert screen_bash(r"cat 'C:\Users\u\AppData\Roaming\Telegram Desktop\tdata\key_datas'")
    assert not screen_bash(r"cat 'C:\Users\u\Documents\Telegram Desktop\readme'")
    assert screen_code("import sqlite3; sqlite3.connect('/home/u/.config/telegram/user.session')")
    assert not screen_code("import sqlite3; sqlite3.connect('/home/u/proj/app.db')")
    # audit S14 r2: SQLite file: URIs (query, ///, localhost, %XX, %00, #, $HOME, cd, a brace variant) and connect()
    tg = "/home/u/.config/telegram/user.session"
    for u in ("file:" + tg + "?mode=ro", "file://" + tg, "file://localhost" + tg + "?immutable=1",
              "file:///home/u/.config/tele%67ram/user%2Esession", "file:" + tg + "%00.db", "file:" + tg + "#x",
              "FILE:" + tg, "file://$HOME/.config/telegram/user.session", "file:" + tg + "{?x,}"):
        assert screen_bash("sqlite3 '%s' 'select hex(auth_key) from sessions'" % u), u
    assert screen_bash("cd ~/.config/telegram && sqlite3 'file:user.session?mode=ro' .dump")
    assert screen_code("import sqlite3; sqlite3.connect('file:%s?mode=ro', uri=True)" % tg)
    assert screen_code('import sqlite3; sqlite3.connect("file:///home/u/.config/telegram/user%2Esession")')
    assert sqlite_uri_path("file:///a/b%20c.db?mode=ro#f") == "/a/b c.db" and sqlite_uri_path("x.db?y") == "x.db?y"
    assert sqlite_uri_path("file://nas" + tg) is None and sqlite_uri_path("file:%C3%A9.db") == "\u00e9.db"
    # S14 r2 review: an authority the shell expands to nothing (unset $X, $(true), `true`) opens the local path
    for a in ("${NOPE}", "$NOPE", "$(true)", "`true`", "loc${NOPE}alhost"):
        assert screen_bash('sqlite3 "file://%s%s" .dump' % (a, tg)), a
    assert screen_code("import sqlite3; sqlite3.connect('file://$NOPE%s', uri=True)" % tg)
    assert screen_bash("python3 -m sqlite3 'file:%s?mode=ro' 'select hex(auth_key) from sessions'" % tg)
    assert not screen_bash("python3 -m sqlite3 'file:/home/u/proj/app.db?mode=ro' .tables")
    for ok in ("sqlite3 'file:test.db?mode=memory' 'select 1'", "sqlite3 'file:///home/u/proj/app.db?mode=ro' .tables",
               "sqlite3 'file://nas%s' .dump" % tg, "sqlite3 https://example.com/telegram/user.session .tables",
               "sqlite3 'file::memory:?cache=shared' 'select 1'", "cat 'file://%s'" % tg):
        assert not screen_bash(ok), ok
    assert not screen_code("import sqlite3; sqlite3.connect('file:test.db?mode=memory&cache=shared', uri=True)")
    print("self-test PASS")

if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        sys.exit(0)
    try:
        main()
    except Exception as e:  # ponytail: fail-CLOSED -- a parse error or hook bug denies this one call
        deny("could not be screened (hook error: " + type(e).__name__ + "), so it is denied fail-closed")
