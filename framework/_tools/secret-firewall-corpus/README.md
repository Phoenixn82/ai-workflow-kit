# secret-firewall-corpus (audit 2026-10-12, item A5)

A labelled replay corpus for the odin `block-secrets.py` PreToolUse hook, the scorer that runs it, and a
fixed hook staged beside the live one. The hook is a **guard against accidental disclosure, not a security
boundary**: Claude has passwordless sudo, so OS ownership is the real wall.

| File | What it is |
|---|---|
| `corpus.jsonl` | 182 cases: 96 must-block, 86 must-allow. Sources: the audit's 18 bypass and 10 benign probes, its hand-classified denials, task shapes, 6 real-traffic false positives, 8 adversarial-review cases (`X01`-`X08`), and 16 string-splitting / glob / shell-variable evasion cases (`E01`-`E08`) each paired with a benign look-alike (`EA01`-`EA08`), 12 normalisation-gap cases (`F01`-`F12`) paired with `FA01`-`FA12`, and 24 Telegram account-credential cases (`T01`-`T24`, `T12`-`T24` SQLite `file:` URIs) with 18 look-alikes (`TA01`-`TA18`) |
| `run.py` | Sends each case to a hook as PreToolUse JSON and prints PASS/FAIL per case plus the score. Exits 0 only on FN=0 and FP<=2. A hook that runs past the 10 s timeout counts as allow, because Claude Code lets the call through. It never executes a corpus command, and it points the hook event log at /dev/null |
| `~/.claude/hooks/secret-firewall/block-secrets.py.proposed` | The fixed hook |

Fake secrets appear only as `{{PLACEHOLDERS}}`. `run.py` assembles them at runtime from fragments, so
gitleaks, detect-secrets and push protection never fire on these files.

```
python3 run.py --self-test                     # corpus shape + scorer check (allow-all stub)
python3 run.py                                 # score the LIVE hook
python3 run.py --hook ~/.claude/hooks/secret-firewall/block-secrets.py.proposed
python3 ~/.claude/hooks/secret-firewall/block-secrets.py.proposed --self-test
```

## Scores

| Hook | must-block blocked | must-allow allowed | FN | FP | precision | recall |
|---|---|---|---|---|---|---|
| legacy `block-secrets.py` | 13/52 | 21/48 | 39 | 27 | 0.33 | 0.25 |
| proposed, before the adversarial review | 46/52 | 47/48 | 6 | 1 | 0.98 | 0.88 |
| `block-secrets.py.proposed` (after review fixes) | 52/52 | 48/48 | 0 | 0 | 1.00 | 1.00 |

The legacy suite `test-block-secrets.py`, run against the proposed hook, passes 29/29.

For false positives, also replay real Bash commands from your own transcripts in-process:

- Most new denials are inline Python that opens `.credentials.json`, `auth.json`, `tokens.json` or a `.key`. A known false-positive shape: an edit script whose replacement string contains `open('<token path>')`.
- Hook cost: a few tens of ms per call, from regex compile plus the `shlex`, `base64` and `glob` imports.
- Without the "local and non-executable" limit on globs, a glob can match a credential-named script (`grep ... ~/.local/bin/*` against a `*-password` script), or expand against the local `/bin` inside an ssh remote command.

## Adversarial review fixes (2026-10-12)

The builder's hook failed X01-X07. Each fix has a corpus case:

- **X06, fail-open hang:** if the command contained the literal text `__SUB0__`, `unsub()` looped forever. The 10 s timeout then let the call through. Literal placeholders are now neutralised before substitution.
- **X01, unnormalised operands:** `~/.claude//mcp.json` slipped past the `endswith` rules. Operands are now checked after `normpath`; bare words are still never matched by name.
- **X02, globs:** `cat ~/.codex/auth*` was not caught. Globs now expand locally (up to 1000 matches) and skip executables. Remote ssh commands are not expanded.
- **X03, missing read verbs:** added `paste rev fold column pr comm join fmt expand unexpand iconv bzcat xzcat zstdcat lzcat zless zmore`.
- **X04, env var printed from code:** `print(os.environ['X_KEY'])`, `os.getenv`, `process.env.X`, `ENV['X']` inside print/console.log/puts, when the name is secret-shaped.
- **X05, token-printing CLIs:** `gh auth token`, `gh auth status -t/--show-token`, `rclone config show`, `gcloud ... print-access/identity-token`, `aws configure export-credentials`, `secret-tool lookup`, `git credential fill`. Allowed when the output goes to a file or a non-display pipe, or into `$(...)`.
- **X07, false block:** `printenv NAME` is denied only for a secret-shaped NAME. Bare `printenv` is still denied.
- **New protected paths:** `~/.pgpass`, `~/.docker/config.json`, `~/.kube/config`.

## What changed in the hook

- **Bash checks operands, not raw text.** A file counts as read only in these positions:
  - an operand of a read verb, now including `less`, `xxd`, `od`, `base64`, `jq`, `tac`, `nl`, `vim` and `sqlite3`
  - the source operand of `cp` or `mv`
  - `< file`, or `@file` / `x=@file`
  - a path passed straight to `open()`, `Path()` or `expanduser()` in `python -c`, `node -e` or an interpreter heredoc
  - anything inside `ssh host '...'`, `bash -c` or a shell heredoc

  Bare words in patterns, flags and prose (`grep tokens`, `--tokens`, `rg input_tokens`) never count. Neither do URLs or `grep -l/-c/-q` (which prints no content).
- **The generic filename-word rule is narrowed.** `password`, `token` and the other generic words now count only as the LAST hyphen or underscore word of a non-code, non-doc filename. `loki-jellyfin-password` is still blocked; `token-usage.csv` and `password_reset.py` no longer are.
- **New protected paths:** `gh/hosts.yml`, `rclone.conf`, `/proc/*/environ`, `/etc/shadow`, browser `Cookies` DBs, and symlinks resolved with realpath. File tools: Grep `pattern` is no longer treated as a path, and Grep `glob` is checked.
- **New literal-secret check, anywhere in the command (heredoc bodies included):** provider key prefixes, private-key headers, JWTs, `Authorization:` / `api-key:` headers with a literal value that contains a digit, `KEY=<high-entropy literal>`, URL passwords, and base64 that decodes to any of these.
- **Environment dumps:** bare `env` (also as `env | sort`), `printenv`, `export -p`, `declare -x`, bare `set`, `print(os.environ)`, and `echo $SECRET_VAR` / `echo "$(cat secret)"`. They are allowed when the output only feeds `wc`, `grep -c` or a non-display command. `${X:+set}` and `${#X}` are allowed.
- **Safe plumbing is allowed:** `X=$(cat f.token)`, a header built from `$(cat f.token)`, `echo "$T" | ssh/gh/curl`, `printf "$T" > file`, writing into a protected file, and `scp`/`rsync`/`install` deploys.
- **Fail-closed:** bad JSON or any exception denies the call (the old hook failed open). Windows-only verbs are dropped.

## Evasion closure (2026-10-12, item `secret-firewall-evasions`)

Two audit subagents got around the firewall while testing. Both cheap classes are now closed, each with a
block case and a benign look-alike allow case (`E01`-`E08` / `EA01`-`EA08`), and a self-test assert in the hook:

- **String-splitting** of a protected name so the pattern missed it: empty/adjacent quotes (`au""th.json`,
  `au''th.json`), ANSI-C `$'au\x74h.json'`, and brace forms (`auth.js{o,x}n`). The command is normalised with
  `shlex` (quotes dropped, adjacent fragments joined; `tokenize` now pre-decodes `$'..'`), and brace
  alternatives are each tested against the protected-path list.
- **Shell-variable indirection**: `f=~/.codex/auth; cat $f.json`, `d=~/.codex; f=auth.json; cat $d/$f`, and the
  former 18th audit probe `f=~/.e; cat ${f}nv`. `resolve_vars()` resolves simple same-command `VAR=literal`
  assignments to a fixpoint before screening. A value holding a command substitution (`X=$(...)`) or an
  unresolved `$ref` is left alone, so safe plumbing (`X=$(cat f.token); curl -H "...: Bearer $X"`) still passes.
- **Globs** already expanded against the filesystem (X02); still covered (`cat ~/.codex/a*h.json`).
- **`eval` of a literal** read command is now screened (`eval cat ~/.codex/auth.json`).

The second subagent "ran an installer the firewall had denied by another route": a wrapper script that performs
the read internally. That is **arbitrary indirection**, the ceiling below, not a cheap class -- the firewall
cannot see inside an arbitrary executable. Marked with a `ponytail:` comment at the `eval` handler.

Cost: within noise (+`codecs` import and the brace/var passes).

## Normalisation gaps (2026-10-12, fw2)

A reviewer's probe found 13 misses inside the documented scope. Each fix has a block case and a benign
look-alike (`F01`-`F12` / `FA01`-`FA12`) plus a self-test assert. The pre-fw2 hook scores FN=12 FP=1 on them.

- **Order-aware variables (F01/FA01):** `resolve_vars()` is now one in-order pass; each `$f` resolves against
  the value assigned so far (`f=SECRET; cat $f; f=x` blocks, `f=x; cat $f; f=SECRET` passes). Replaces the fixpoint.
- **`+=` append (F02/FA02)**, also stripped as a command prefix.
- **`$".."` locale quoting (F03/FA03)** is reduced to plain double quotes, like `$'..'`.
- **Backslash-newline (F04/FA04)** is deleted (word joined), not replaced by a space; the literal-secret scan also
  re-runs on the joined text.
- **`{x..y}` brace sequences (F05/F06/FA05/FA06):** numeric (zero-pad, step) and single-letter.
- **Brace cap (F07/F08/FA07/FA08):** raised from 64 to 1024 variants; past the cap the operand is DENIED instead
  of testing unexpanded leftovers as literal text. Trade-off: a read of a >1024-variant brace is a false block.
- **`cd`/`pushd` (F09/F10/FA09/FA10):** later relative operands resolve against the new directory (also inside
  ssh remote commands, where it turns on local glob expansion for that directory).
- **`for v in a b` (F11/FA11):** binds `v` to every listed word, substituted as the brace list `{a,b}`.
- **Compound-command keywords (F12/FA12):** `do cat f`, `then cat f`, `{ cat f; }`, `if cat f` were never screened
  (the verb was the keyword). `if then else elif do while until { !` are now skipped like wrappers.

Cost: negligible.

## Telegram account credentials (2026-10-13, audit S14)

The kit's telegram add-on keeps a full account login in `~/.config/telegram/user.session` (tg-admin's Telethon
session, SQLite) and imports it from Telegram Desktop's `tdata` store; both are stronger than a bot token, and the
hook saw neither. Now protected:

- a `*.session` file, and its SQLite sidecars `-journal`, `-wal`, `-shm`, anywhere under a path containing `telegram`
  (the Read tool, read verbs incl. `sqlite3`, the `cp` source, a relative name after `cd ~/.config/telegram`);
- everything in a `tdata` directory whose parent's name contains `telegram`: Linux `~/.local/share/TelegramDesktop`,
  flatpak and snap copies, macOS `~/Library/Application Support/Telegram Desktop`, Windows `%APPDATA%\Telegram Desktop`.
  Its files have no dot (`key_datas`, `D877F783D5D3EF8Cs`), so a bare name read after `cd` into `tdata` counts too
  (`T09`), and so does a backslash path with no dot (`T10`: any operand with a `\` is now treated as a path);
- a path passed straight to Python's `sqlite3.connect(...)` (`T11`; `connect(` joined `open(`/`Path(` in inline code).

Other apps' `session` files stay readable: `*.session` outside a telegram path (Playwright's daemon writes
`~/.cache/ms-playwright/daemon/*/*.session`), `session.py` / `session.db`, the docs and source that mention
`user.session`, Telegram Desktop's `log.txt` beside `tdata`, and `ls` of either directory (names only). The pre-S14
hook scores FN=11 on `T01`-`T11`. Not caught: a `user.session.bak` copy (like `id_ed25519.bak` below); the session
named inside SQL text (`sqlite3 :memory: "attach '...user.session' as s"`, `readfile('...')`, `.open file:...`);
a Telethon `*.session` outside a telegram path; the `tdata` of Telegram Desktop forks (Kotatogram, AyuGram, 64Gram);
and PowerShell's `Get-Content`, which is not a read verb for any protected file.

## SQLite `file:` URIs (2026-10-13, audit S14 round 2)

The S14 rules matched a filename ending in `.session`, so a SQLite URI got past them: `?mode=ro` broke the suffix,
and `file:///...` was skipped like every `://` URL. A `file:` URI is now screened as the local path SQLite opens,
parsed the way `sqlite3ParseUri` does it (sqlite.org/uri.html):

- `file:PATH`, `file:///PATH` and `file://localhost/PATH`; the `?query` and `#fragment` are dropped, `%XX` is
  decoded (`tele%67ram`, `user%2Esession`) and `%00` ends the path, as in SQLite. `$HOME` expands before the
  authority is checked (`file://$HOME/...`), a relative `file:user.session` resolves after `cd`, and braces expand
  before the URI is parsed, as bash does before sqlite3 sees it.
- Where: the operands of `sqlite3` (CLI, also behind `ssh`/`bash -c`/`sudo`) and of an interpreter (`python3 -m
  sqlite3 'file:...'`: Debian/Ubuntu build libsqlite3 with `SQLITE_USE_URI`), and the string passed straight to
  `connect(...)` in inline code (`sqlite3.connect('file:...', uri=True)`, and without `uri=True`, which builds with
  `SQLITE_USE_URI` parse anyway). Only SQLite reads these URIs, so `cat file:///...` (a relative path to cat) and
  other verbs are unchanged.
- Never a local file: a URI naming a literal other host (`file://nas/...`, which SQLite refuses) and any other scheme
  (`https://...`). Benign database URIs stay allowed: `file:test.db?mode=memory`, `file::memory:`, a project
  database by `file:///.../app.db?mode=ro`, an unrelated `session.db`, and grep for the URI text.
- An authority that is not a literal host name (`file://$X/...`, `${X}`, `$(cmd)`, `` `cmd` ``) counts as local:
  unset or empty, bash turns it into `file:///...` and SQLite opens the path (review of the round-2 fix).

Cases `T12`-`T24` (blocked; the pre-fix hook missed all 13, the first round-2 fix missed `T21`-`T24`) and `TA11`-`TA18`
(allowed). Still not caught: a URI built
at runtime (`connect(f"file:{p}")`), a `database=` keyword argument, URIs inside SQL text (above), a brace that
builds the scheme (`sqlite3 {file,x}:///...`, the parked brace-URL gap: `://` is exempt before braces expand), and SQLite
openers other than `connect(` in inline code (Node's `DatabaseSync(...)`, better-sqlite3's `Database(...)`), which
miss plain paths too.

## Not caught (it is a guard, not a boundary)

- Arbitrary indirection: `eval "$(printf ...)"`, `echo <b64> | base64 -d | sh`, a path built by `$(...)`, or a
  wrapper script/installer that reads the secret internally. Marked `ponytail: CEILING` in the hook.
- `git show REV:.env`, `git diff --no-index /dev/null .env`, `curl file:///.../.env`, `xargs -a .env echo`.
- A copy to a neutral name in one call followed by a read in the next. Also laundering inside a single call: `X=$(cat .env); echo "$X"`, `source .env && echo "$DATABASE_URL"`.
- Output that contains environment values: `docker compose config`, `docker inspect`, `ps eww`, `/etc/environment`. Also private-key backups (`id_ed25519.bak`) and keyring files.
- Minor false blocks, kept on purpose: `echo $PASS_COUNT` / `$GPG_KEY_ID` (any `_PASS_`/`_KEY_` word), `cat data/tokens.json`, and `print(os.environ['X_KEY'][:6])`, which prints a prefix.
- A pathological very long one-line command takes seconds, and a large enough one could reach the 10 s timeout, which fails open.
- Literal secrets in Write/Edit *content*, since only Bash text is scanned.
- Still blocked by design: `cp .env.local` for a build, because the legacy suite requires `cp` of secret files to deny. Also `env | grep -i X`, because it prints values; use `${X:+set}` or `grep -c` instead.
- A hook SyntaxError still exits 1 (non-blocking) and leaves the firewall open. The gate below must pass before every deploy.
- Codex has no firewall. Registering it there is a separate step.
- Shell-parsing limits, accepted as known limits (the user, 2026-10-13): the tokenizer drops quoting, so a quoted operator character can be taken for a real command boundary; any assignment-looking word (an argument or a comment, not only a real assignment) updates the tracked variables; a `cd` behind a compound keyword or a wrapper is not followed when later relative paths are judged; and one URL alternative inside a brace list exempts the whole list before it expands. Each can let a deliberately shaped read through; ordinary commands are screened as before.

## Apply (orchestrator)

Run from a shell, or as a single Bash call. The live hook was checked and allows this exact command.

```
cd ~/.claude/hooks/secret-firewall && \
python3 block-secrets.py.proposed --self-test && \
python3 ~/AI_Projects/_tools/secret-firewall-corpus/run.py --hook block-secrets.py.proposed && \
sed 's#block-secrets.py"#block-secrets.py.proposed"#' test-block-secrets.py | python3 - && \
install -m 755 block-secrets.py.proposed block-secrets.py.new && mv -f block-secrets.py.new block-secrets.py && \
mv -f readme.txt.proposed readme.txt && rm block-secrets.py.proposed && \
python3 test-block-secrets.py && python3 ~/AI_Projects/_tools/secret-firewall-corpus/run.py && \
git -C ~/.claude commit -q -m "secret-firewall: operand-level screening, literal-secret check, fail-closed (audit A5)" \
  -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" -- hooks/secret-firewall
```

`mv -f` is atomic, so the firewall never runs a half-written file. The commit uses a pathspec, so it includes
the deletion of `block-secrets.py.proposed` (an earlier auto-commit started tracking it) and never picks up
files that other sessions have staged in `~/.claude`. Rollback:
`git -C ~/.claude checkout dd9d61d -- hooks/secret-firewall/block-secrets.py hooks/secret-firewall/readme.txt`.
Optional, for Batch G: register the hook as `python3 -I -S ...` in settings.json. That saves interpreter start-up time on every call.
