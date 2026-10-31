#!/usr/bin/env python3
"""harness_lint.py -- governance lint that breaks the rebuild loop (audit 2026-10-12, G1-G5). Read-only.
Prints PASS / FAIL / SKIP per rule: instruction-file size budgets, the Skill listing vs a budget of 75, lanes.json vs
live configs, the lanes.json route table is sound, every non-stock timer and .path unit on odin/heimdall/thor has a REGISTRY.md row, no .path unit
re-fires on a level condition its service leaves true, every REGISTRY row names a kill switch, hook commands point at real files, Codex hook hashes match, vault-lint budgets.
  --json        machine output for weekly-review (collect.py)
  --dry-run     list the rules and the commands they would run; touches nothing (no ssh, no thor-up)
  --self-test   synthetic checks, no network
  --pre-commit  git hook: refuse a commit that grows a staged boot.md or CLAUDE.md past its budget
  --units       print this host's timer/.path units as JSON (shipped to heimdall/thor as `python3 - --units`)
Exit 1 when any rule FAILs (SKIP = could not check: host asleep or unreachable, file absent)."""
import argparse, datetime as dt, glob, json, os, re, shlex, shutil, subprocess, sys, tempfile

HOME = os.path.expanduser("~")
AI = f"{HOME}/AI_Projects"
VAULT = f"{AI}/_system/second-brain"
REGISTRY = f"{AI}/_system/automations/REGISTRY.md"
APPLY_LANES = f"{AI}/_tools/lane-intel/apply_lanes.py"
LANES_JSON = f"{AI}/_system/lanes.json"
CONFIG_LANES = {"claude.main", "codex.interactive"}  # config defaults apply_lanes.py writes; nothing dispatches them by route
HOOK_HASH = f"{AI}/_system/skills/guard/scripts/codex_hook_hash.py"
VAULT_LINT = f"{AI}/_tools/vault-lint/vault_lint.py"
THOR_UP = f"{HOME}/.local/bin/thor-up"
KB = 1024
BUDGETS = {  # path -> {metric: max}; metrics: lines, bytes, chars
    f"{HOME}/.claude/CLAUDE.md": {"lines": 60, "bytes": 10 * KB},
    f"{AI}/CLAUDE.md": {"lines": 40},
    f"{AI}/AGENTS.md": {"lines": 20, "bytes": 3 * KB},  # thin loader; Codex rules live in ~/.codex/AGENTS.md
    f"{HOME}/.codex/AGENTS.md": {"lines": 60},
    f"{VAULT}/AGENTS.md": {"lines": 120},
    f"{VAULT}/boot.md": {"chars": 4000},
    f"{VAULT}/harness-memory/MEMORY.md": {"lines": 200, "bytes": 25 * KB},
}
DEFAULT_BUDGET = {"boot.md": {"chars": 4000}, "CLAUDE.md": {"lines": 60, "bytes": 10 * KB}}  # pre-commit, unnamed paths
SKILL_BUDGET = 75
STOCK = ("/usr/lib/", "/lib/", "/dev/null")  # distro-shipped (or masked) units are not ours to register; same rule as collect.py
KILL = re.compile(r"disable|stop|HALT|touch |\brm\b|revoke|delete|close|pause|unpublish|\boff\b|kill|compose down", re.I)
INTERP = {"bash", "sh", "zsh", "python", "python3", "node", "uv", "deno", "bun", "env"}
HOOK_FILES = (f"{HOME}/.claude/settings.json", f"{HOME}/.codex/hooks.json")
HOSTS = ("odin", "heimdall", "thor")
LEVEL = ("DirectoryNotEmpty", "PathExists", "PathExistsGlob")  # level-triggered .path conditions
PATH_KEYS = LEVEL + ("PathChanged", "PathModified")  # an empty assignment of any of these resets them all (systemd.path)


def sh(cmd, timeout=60, inp=None):
    """(rc, stdout). Never raises; on timeout the process group is killed (collect.py's pattern)."""
    try:
        p = subprocess.Popen(cmd, text=True, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             stdin=subprocess.PIPE if inp is not None else subprocess.DEVNULL)
    except OSError:
        return 127, ""
    try:
        out = p.communicate(inp, timeout=timeout)[0] or ""
        return p.returncode, out
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        p.communicate()
        return 124, ""


def short(p):
    return p.replace(HOME, "~", 1)


def res(rule, status, detail=""):
    return {"rule": rule, "status": status, "detail": detail}


# ---------- 1. size budgets ----------
def measure(text):
    return {"lines": len(text.splitlines()), "bytes": len(text.encode()), "chars": len(text)}


def over(text, budget):
    m = measure(text)
    return [f"{k} {m[k]} > {v}" for k, v in budget.items() if m[k] > v]


def check_sizes(budgets=BUDGETS):
    out = []
    for p, b in budgets.items():
        if not os.path.exists(p):
            out.append(res(f"size {short(p)}", "SKIP", "file absent"))
            continue
        text = open(p, encoding="utf-8", errors="replace").read()
        bad, m = over(text, b), measure(text)
        out.append(res(f"size {short(p)}", "FAIL" if bad else "PASS",
                       "; ".join(bad) if bad else ", ".join(f"{k} {m[k]}/{v}" for k, v in b.items())))
    return out


# ---------- 2. skill listing ----------
def check_skills(root=f"{HOME}/.claude/projects", days=14, budget=SKILL_BUDGET):
    """Newest initial skill_listing attachment per entrypoint (cli = interactive, sdk-cli = claude -p) in recent
    top-level transcripts. Reads that one record's count only, never message text."""
    newest, cutoff = {}, dt.datetime.now().timestamp() - days * 86400
    for f in glob.glob(f"{root}/*/*.jsonl"):
        if os.path.getmtime(f) < cutoff:
            continue
        with open(f, errors="replace") as fh:
            for line in fh:
                if '"skill_listing"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                a = d.get("attachment") or {}
                if a.get("type") == "skill_listing" and a.get("isInitial") and d.get("entrypoint"):  # pre-entrypoint versions: skip
                    ep = d["entrypoint"]
                    if d.get("timestamp", "") > newest.get(ep, {}).get("ts", ""):
                        newest[ep] = {"ts": d.get("timestamp", ""), "n": a.get("skillCount") or len(a.get("names") or []),
                                      "bytes": len(a.get("content") or "")}
                    break
    if not newest:
        return [res("skill listing", "SKIP", f"no skill_listing in transcripts of the last {days} days")]
    return [res(f"skill listing ({ep})", "FAIL" if v["n"] > budget else "PASS",
                f"{v['n']} skills (budget {budget}), {v['bytes']} chars, session started {v['ts'][:16]}Z")
            for ep, v in sorted(newest.items())]


# ---------- 3. lanes ----------
def check_lanes():
    rc, out = sh([sys.executable, APPLY_LANES, "--check"], 60)
    if rc not in (0, 1):
        return [res("lanes.json vs live configs", "SKIP", f"apply_lanes.py --check rc {rc}")]
    return [res("lanes.json vs live configs", "PASS" if rc == 0 else "FAIL",
                "0 drift" if rc == 0 else "; ".join(l for l in out.splitlines() if l.startswith("DRIFT")))]


def check_routes(path=LANES_JSON):
    """Every route names an existing lane and carries use/why/source; every lane model is in allowed_models for its
    provider (only local ollama-* providers are exempt: `lane` admission governs them, so a misspelt cloud provider
    fails); every lane except the config defaults is used by at least one route."""
    try:
        L = json.load(open(path))
    except (OSError, ValueError) as e:
        return [res("lanes.json routes", "FAIL", f"unreadable: {e}")]
    lanes, allowed = L.get("lanes", {}), L.get("allowed_models", {})
    routes = {k: v for k, v in L.get("routes", {}).items() if not k.startswith("_")}
    bad = [] if routes else ["no routes"]
    for c, r in routes.items():
        if r.get("lane") not in lanes:
            bad.append(f"route {c} -> missing lane {r.get('lane')!r}")
        bad += [f"route {c} has no {k}" for k in ("use", "why", "source") if not str(r.get(k, "")).strip()]
    for n, l in lanes.items():
        p = str(l.get("provider", ""))
        if not p.startswith("ollama") and l.get("model") not in allowed.get(p, []):
            bad.append(f"lane {n} model {l.get('model')!r} not in allowed_models.{p}")
    used = {r.get("lane") for r in routes.values()}
    bad += [f"lane {n} is used by no route" for n in lanes if n not in used and n not in CONFIG_LANES]
    return [res("lanes.json routes", "FAIL" if bad else "PASS",
                "; ".join(bad) if bad else f"{len(routes)} routes over {len(used)} lanes; config-only: {', '.join(sorted(CONFIG_LANES))}")]


# ---------- 4. timers / .path units vs REGISTRY ----------
def parse_show(out):
    units, cur = {}, {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur.get("Id"):
                units[cur["Id"]] = cur
            cur = {}
        elif "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
    return units


def level_triggers(fragment, dropins=()):
    """["Directive=path"] for level-triggered conditions in a .path unit whose service (Unit=, default <stem>.service,
    same directory) lacks a `# harness-lint: drains <path>` line. systemd re-fires those each time the service exits
    while the condition still holds, until start-limit-hit kills the .path (review-queue-intake, astra round 1 #14).
    The unit's drop-ins (DropInPaths, in order) count too: `systemctl edit` writes the directive there (astra2 review)."""
    text = ""
    for f in (fragment, *dropins):
        try:
            text += open(f, encoding="utf-8", errors="replace").read() + "\n"
        except OSError as e:
            return [f"unreadable {short(f)} ({type(e).__name__})"]
    kv = [[x.strip() for x in l.split("=", 1)] for l in text.splitlines() if "=" in l and not l.lstrip().startswith(("#", ";"))]
    hits = []
    for k, v in kv:
        if k in PATH_KEYS and not v:
            hits = []
        elif k in LEVEL:
            hits.append((k, v))
    if not hits:
        return []
    svc = next((v for k, v in reversed(kv) if k == "Unit"), os.path.basename(fragment).rsplit(".", 1)[0] + ".service")
    try:
        svc_text = open(os.path.join(os.path.dirname(fragment), svc), encoding="utf-8", errors="replace").read()
    except OSError:
        svc_text = ""  # service not beside the .path: unproven, so flagged
    return [f"{k}={v}" for k, v in hits if f"harness-lint: drains {v}" not in svc_text]


def units():
    """This host's timers (systemctl list-timers --all) and .path units, system and user, with their unit file path."""
    out = []
    for scope, flag in (("system", []), ("user", ["--user"])):
        names = []
        for cmd, key in ((["list-timers", "--all"], "unit"), (["list-units", "--type=path", "--all"], "unit")):
            rc, raw = sh(["systemctl", *flag, *cmd, "-o", "json", "--no-pager"])
            try:  # a failed or empty query is an error, never "0 units" (systemctl prints [] when there are none)
                if rc != 0 or not raw.strip():
                    raise ValueError
                names += [u[key] for u in json.loads(raw)]
            except (ValueError, KeyError, TypeError):
                out.append({"scope": scope, "unit": "?", "fragment": "", "error": f"{' '.join(cmd)} rc {rc}"})
        rc, raw = sh(["systemctl", *flag, "show", "-p", "Id,FragmentPath,DropInPaths", *names]) if names else (0, "")
        if rc != 0:
            out.append({"scope": scope, "unit": "?", "fragment": "", "error": f"show rc {rc}"})
        show = parse_show(raw)
        for n in names:
            frag, dropins = show.get(n, {}).get("FragmentPath", ""), show.get(n, {}).get("DropInPaths", "").split()
            out.append({"scope": scope, "unit": n, "fragment": frag,
                        **({"level": level_triggers(frag, dropins)} if n.endswith(".path") and frag else {})})
    return out


def host_units(host):
    """(units, error)."""
    if host == "odin":
        return units(), None
    if host == "thor" and sh([THOR_UP], 150)[0] != 0:
        return None, "thor-up failed (asleep or down)"
    rc, out = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "nice", "-n", "10", "python3", "-", "--units"],
                 90, inp=open(__file__).read())
    try:
        return json.loads(out), None
    except ValueError:
        return None, f"ssh failed (rc {rc})"


def registry_rows(text):
    """[(name, kill)] for every table row (also rows pasted below the table)."""
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or set(s) <= set("|-: "):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", s.strip("|"))]
        if len(cells) < 5 or cells[0] == "Automation":
            continue
        rows.append((cells[0], cells[-1]))
    return rows


def check_registry(reg_text, host_data):
    """host_data: {host: (units | None, error | None)}."""
    out = []
    nokill = [n[:60] for n, k in registry_rows(reg_text) if not KILL.search(k)]
    out.append(res("registry rows name a kill switch", "FAIL" if nokill else "PASS",
                   "no kill switch: " + " | ".join(nokill) if nokill else f"{len(registry_rows(reg_text))} rows"))
    for host, (us, err) in host_data.items():
        for kind in ("timer", "path"):
            rule = f"{kind} units registered ({host})"
            if err:
                out.append(res(rule, "SKIP", err))
                continue
            # transient units (systemd-run, e.g. web's per-launch web-identity-cap timer) live under /run and are not installed jobs
            ours = [u for u in us if u["unit"].endswith("." + kind) and not u["fragment"].startswith(STOCK)
                    and "/systemd/transient/" not in u["fragment"]]
            # ponytail: a row "covers" a unit when the unit's stem appears anywhere in REGISTRY.md (collect.py's rule)
            miss = sorted(f"{u['scope']}:{u['unit']}" for u in ours if u["unit"].rsplit(".", 1)[0] not in reg_text)
            errs = [u["error"] for u in us if u.get("error")]
            out.append(res(rule, "FAIL" if miss else ("SKIP" if errs else "PASS"),
                           ("no row: " + ", ".join(miss)) if miss else
                           (f"systemctl json failed: {errs}" if errs else f"{len(ours)} non-stock, all registered")))
    return out


def check_path_triggers(host_data):
    """A non-stock .path unit with a level condition its service does not drain (see level_triggers) FAILs."""
    out = []
    for host, (us, err) in host_data.items():
        rule = f"path units not re-firing on a level condition ({host})"
        if err:
            out.append(res(rule, "SKIP", err))
            continue
        paths = [u for u in us if u["unit"].endswith(".path") and not u["fragment"].startswith(STOCK)]
        # ponytail: a host still running an older harness_lint omits "level"; that unit reads as unchecked, not clean
        unchecked = [f"{u['scope']}:{u['unit']}" for u in paths if "level" not in u]
        bad = [f"{u['scope']}:{u['unit']} {'; '.join(u['level'])}" for u in paths if u.get("level")]
        errs = [u["error"] for u in us if u.get("error")]  # astra3_7: a failed systemctl query is never "0 .path ok"
        out.append(res(rule, "FAIL" if bad else ("SKIP" if unchecked or errs else "PASS"),
                       ("level-triggered, service does not drain (use PathChanged, or add `# harness-lint: drains "
                        "<path>` to the service): " + " | ".join(bad)) if bad else
                       (f"systemctl json failed: {errs}" if errs else
                        f"no level data: {', '.join(unchecked)}" if unchecked else f"{len(paths)} non-stock .path ok")))
    return out


# ---------- 5. hook targets ----------
def hook_commands(path):
    try:
        d = json.load(open(path))
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}"
    return [(ev, h.get("command", "")) for ev, gs in (d.get("hooks") or {}).items() for g in gs
            for h in g.get("hooks", []) if h.get("type", "command") == "command"], None


def hook_target_problem(cmd):
    """None when the command's script exists (and is executable when run directly), else a reason."""
    try:
        argv = [os.path.expanduser(os.path.expandvars(a)) for a in shlex.split(cmd)]
    except ValueError:
        return "unparseable"
    while argv and re.fullmatch(r"[A-Za-z_]\w*=.*", argv[0], re.S):
        argv = argv[1:]  # leading VAR=value env assignments (PYTHONPATH=... python3 -m x) are not the target
    if not argv:
        return "empty"
    if os.path.basename(argv[0]) in INTERP:
        if not shutil.which(argv[0]):
            return f"interpreter {argv[0]} not found"
        target = next((a for a in argv[1:] if not a.startswith("-") and "/" in a), None)
        if target is None:
            return None  # ponytail: inline code (bash -c '...') has no file to check
        return None if os.path.isfile(target) else f"missing {short(target)}"
    if "/" not in argv[0]:
        return None if shutil.which(argv[0]) else f"{argv[0]} not on PATH"
    if not os.path.isfile(argv[0]):
        return f"missing {short(argv[0])}"
    return None if os.access(argv[0], os.X_OK) else f"not executable {short(argv[0])}"


def check_hooks(files=HOOK_FILES):
    out = []
    for f in files:
        cmds, err = hook_commands(f)
        if err:
            out.append(res(f"hook targets {short(f)}", "SKIP" if not os.path.exists(f) else "FAIL", err))
            continue
        bad = [f"{ev}: {p}" for ev, c in cmds if (p := hook_target_problem(c))]
        out.append(res(f"hook targets {short(f)}", "FAIL" if bad else "PASS", "; ".join(bad) if bad else f"{len(cmds)} hooks ok"))
    return out


# ---------- 6. Codex hook trust ----------
def check_codex_hashes():
    rc, out = sh([sys.executable, HOOK_HASH, f"{HOME}/.codex/hooks.json", f"{HOME}/.codex/config.toml"], 30)
    lines = [l.split()[1] for l in out.splitlines() if l.startswith(("MATCH", "DIFF"))]
    diff = [":".join(l.split()[1].split(":")[-3:]) for l in out.splitlines() if l.startswith("DIFF")]  # event:group:hook
    if rc != 0 or not lines:
        return [res("codex hook hashes", "SKIP", f"codex_hook_hash.py rc {rc}, {len(lines)} hooks")]
    return [res("codex hook hashes", "FAIL" if diff else "PASS",
                ("untrusted (re-approve in Codex /hooks): " + ", ".join(diff)) if diff else f"{len(lines)}/{len(lines)} match")]


# ---------- 7. vault-lint budgets ----------
def check_vault_lint():
    rc, out = sh([sys.executable, VAULT_LINT, "report", "--json"], 120)
    try:
        r = json.loads(out)
    except ValueError:
        return [res("vault-lint budgets", "SKIP", f"vault_lint.py report --json rc {rc}")]
    keys = [k for k in r["counts"] if k.endswith("_over_budget")]
    bad = [f"{k}: {', '.join(r['offenders'][k])}" for k in keys if r["counts"][k]]
    return [res("vault-lint budgets", "FAIL" if bad else "PASS", "; ".join(bad) if bad else ", ".join(f"{k} 0" for k in keys))]


# ---------- pre-commit ----------
def pre_commit():
    git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True)
    top = git("rev-parse", "--show-toplevel").stdout.strip()
    staged = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").stdout.split("\0") if p]
    known = {os.path.realpath(k): v for k, v in BUDGETS.items()}
    refused = 0
    for rel in staged:
        name = os.path.basename(rel)
        if name not in DEFAULT_BUDGET:
            continue
        budget = known.get(os.path.realpath(os.path.join(top, rel)), DEFAULT_BUDGET[name])
        new = git("show", f":{rel}").stdout
        old_p = git("show", f"HEAD:{rel}")
        old = old_p.stdout if old_p.returncode == 0 else None
        bad = over(new, budget)
        if not bad:
            continue
        # ponytail: a commit that shrinks an already over-budget file passes (warned) so the trim can land
        grew = old is None or not over(old, budget) or any(measure(new)[k] > measure(old)[k] for k in budget)
        print(f"harness-lint pre-commit: {rel} over budget ({'; '.join(bad)})" + ("" if grew else ", shrinking: allowed"), file=sys.stderr)
        refused += grew
    if refused:
        print("harness-lint pre-commit: refused. Trim the file, or override once with `git commit --no-verify`.", file=sys.stderr)
    return 1 if refused else 0


# ---------- main ----------
def run_all():
    reg = open(REGISTRY, encoding="utf-8").read()
    hd = {h: host_units(h) for h in HOSTS}
    return (check_sizes() + check_skills() + check_lanes() + check_routes() + check_registry(reg, hd) + check_path_triggers(hd)
            + check_hooks() + check_codex_hashes() + check_vault_lint())


def dry_run():
    print("harness_lint --dry-run: rules and commands (nothing executed)")
    for p, b in BUDGETS.items():
        print(f"  size {short(p)} <= {b}")
    print(f"  skill listing <= {SKILL_BUDGET} (newest skill_listing record per entrypoint in ~/.claude/projects/*/*.jsonl, 14 days)")
    print(f"  python3 {short(APPLY_LANES)} --check")
    print(f"  {short(LANES_JSON)} routes: existing lane, use/why/source, models in allowed_models, every lane routed except {sorted(CONFIG_LANES)}")
    print(f"  REGISTRY rows need a kill switch matching /{KILL.pattern}/")
    print(f"  timers + .path units: local systemctl; ssh heimdall python3 - --units; {short(THOR_UP)} then ssh thor python3 - --units")
    print(f"  .path units: no {'/'.join(LEVEL)} unless the service says `# harness-lint: drains <path>`")
    print(f"  hook targets in {', '.join(short(f) for f in HOOK_FILES)}")
    print(f"  python3 {short(HOOK_HASH)} ~/.codex/hooks.json ~/.codex/config.toml")
    print(f"  python3 {short(VAULT_LINT)} report --json (*_over_budget counts)")
    return 0


def self_test():
    global sh  # swapped for stubs below; restored in finally
    assert measure("a\nb\n") == {"lines": 2, "bytes": 4, "chars": 4} and measure("é") == {"lines": 1, "bytes": 2, "chars": 1}
    assert over("x\n" * 61, {"lines": 60}) == ["lines 61 > 60"] and over("x\n" * 60, {"lines": 60}) == []
    reg = ("| Automation | Schedule | Lane | Scope | How to kill |\n|---|---|---|---|---|\n"
           "| foo-job | daily | z | s | `systemctl --user disable --now foo-job.timer` |\n| tg | x | z | s | n/a (no schedule) |\n"
           "\n## Kill everything now\n| bar | x | z | s | touch HALT |\n")
    assert registry_rows(reg) == [("foo-job", "`systemctl --user disable --now foo-job.timer`"), ("tg", "n/a (no schedule)"), ("bar", "touch HALT")]
    us = [{"scope": "user", "unit": "foo-job.timer", "fragment": f"{HOME}/.config/systemd/user/foo-job.timer"},
          {"scope": "user", "unit": "new-thing.timer", "fragment": "/etc/systemd/user/new-thing.timer"},
          {"scope": "system", "unit": "fstrim.timer", "fragment": "/usr/lib/systemd/system/fstrim.timer"},
          {"scope": "user", "unit": "web-identity-cap.timer", "fragment": "/run/user/1000/systemd/transient/web-identity-cap.timer"},
          {"scope": "user", "unit": "q.path", "fragment": f"{HOME}/.config/systemd/user/q.path"}]
    r = {x["rule"]: x for x in check_registry(reg, {"odin": (us, None), "thor": (None, "thor-up failed")})}
    assert r["registry rows name a kill switch"]["status"] == "FAIL" and "tg" in r["registry rows name a kill switch"]["detail"]
    assert KILL.search("`cd ~/services/immich && docker compose down` on thor")  # weekly review 2026-10-25 #12
    assert r["timer units registered (odin)"]["status"] == "FAIL" and "new-thing" in r["timer units registered (odin)"]["detail"]
    assert "web-identity-cap" not in r["timer units registered (odin)"]["detail"]
    assert "fstrim" not in r["timer units registered (odin)"]["detail"] and "foo-job" not in r["timer units registered (odin)"]["detail"]
    assert r["path units registered (odin)"]["status"] == "FAIL" and r["timer units registered (thor)"]["status"] == "SKIP"
    assert sh(["sleep", "5"], timeout=1)[0] == 124 and sh(["sh", "-c", "exit 3"])[0] == 3 and sh(["true"]) == (0, "")
    # a failed, timed-out or empty systemctl query must never read as "0 non-stock, all registered" (Astra round 1, #21)
    real_sh = sh

    def good(cmd, *a, **k):
        if "list-timers" in cmd:
            return 0, json.dumps([{"unit": "foo-job.timer"}])
        if "list-units" in cmd:
            return 0, "[]"
        return 0, f"Id=foo-job.timer\nFragmentPath={HOME}/.config/systemd/user/foo-job.timer\n"
    for stub, want in ((good, {"PASS"}), (lambda *a, **k: (1, ""), {"SKIP", "FAIL"}), (lambda *a, **k: (0, ""), {"SKIP", "FAIL"}),
                       (lambda *a, **k: (124, ""), {"SKIP", "FAIL"}), (lambda *a, **k: (1, "[]"), {"SKIP", "FAIL"})):
        sh = stub
        try:
            got = {x["rule"]: x["status"] for x in check_registry(reg, {"odin": (units(), None)}) if "(odin)" in x["rule"]}
        finally:
            sh = real_sh
        assert set(got.values()) <= want and len(got) == 2, (stub, got)
    assert parse_show("Id=a.timer\nFragmentPath=/x\n\nId=b.path\nFragmentPath=\n")["a.timer"]["FragmentPath"] == "/x"
    # astra2_14: DirectoryNotEmpty on an inbox the service only registers (never empties) re-fired until
    # unit-start-limit-hit can kill a .path unit
    with tempfile.TemporaryDirectory() as td:
        inbox = f"{td}/Review Queue Inbox"
        open(f"{td}/rq.path", "w").write(f"[Path]\nPathChanged={inbox}\nDirectoryNotEmpty={inbox}\nUnit=rq.service\n")
        open(f"{td}/rq.service", "w").write("[Service]\nExecStart=/bin/true\n")
        assert level_triggers(f"{td}/rq.path") == [f"DirectoryNotEmpty={inbox}"]
        open(f"{td}/rq.service", "w").write(f"# harness-lint: drains {inbox}\n[Service]\nExecStart=/bin/true\n")
        assert level_triggers(f"{td}/rq.path") == []                         # service declares it empties the dir
        open(f"{td}/x.path", "w").write(f"[Path]\nPathExists={td}/flag\n# DirectoryNotEmpty={inbox}\n")
        assert level_triggers(f"{td}/x.path") == [f"PathExists={td}/flag"]   # default x.service is absent: flagged
        open(f"{td}/ok.path", "w").write(f"[Path]\nPathChanged={inbox}\n")
        assert level_triggers(f"{td}/ok.path") == [] and level_triggers(f"{td}/gone.path")[0].startswith("unreadable")
        live = f"{HOME}/.config/systemd/user/review-queue-intake.path"
        if os.path.exists(live):
            assert level_triggers(live) == [], level_triggers(live)
        mk = lambda lv: [{"scope": "user", "unit": "rq.path", "fragment": f"{td}/rq.path", **lv},
                         {"scope": "system", "unit": "s.path", "fragment": "/usr/lib/systemd/system/s.path", "level": ["X=y"]}]
        st = lambda us, err=None: check_path_triggers({"odin": (us, err)})[0]
        assert st(mk({"level": [f"DirectoryNotEmpty={inbox}"]}))["status"] == "FAIL"
        assert st(mk({"level": []}))["status"] == "PASS"                       # stock unit's level is not ours
        assert st(mk({}))["status"] == "SKIP" and st(None, "thor-up failed")["status"] == "SKIP"
        # astra3_7: units() turns failed systemctl queries into error rows (unit "?"); with every query exiting 1 the
        # check filtered them out and printed "PASS: 0 non-stock .path ok"
        real_sh = sh
        for stub in (lambda *a, **k: (1, ""), lambda *a, **k: (0, ""), lambda *a, **k: (124, "")):
            sh = stub
            try:
                got = st(units())
            finally:
                sh = real_sh
            assert got["status"] in ("SKIP", "FAIL") and "failed" in got["detail"], got
        assert st(mk({"level": [f"DirectoryNotEmpty={inbox}"]}) + [{"scope": "user", "unit": "?", "fragment": "", "error": "e"}])["status"] == "FAIL"
        # astra3r_7 (review): one blind scope beside a clean one. With only the user bus failing, the system scope's clean
        # unit read "PASS: 1 non-stock .path ok" before the fix; an errors-only-when-no-units shortcut would bring it back
        sh = lambda cmd, *a, **k: ((1, "") if "--user" in cmd and "list-units" in cmd else (0, "[]") if "list-timers" in cmd else
                                   (0, json.dumps([{"unit": "ok.path"}])) if "list-units" in cmd else
                                   (0, f"Id=ok.path\nFragmentPath={td}/ok.path\nDropInPaths=\n"))
        try:
            got = st(units())
        finally:
            sh = real_sh
        assert got["status"] == "SKIP" and "list-units --type=path --all rc 1" in got["detail"], got
        # astra2_14 review: `systemctl edit` puts DirectoryNotEmpty in a drop-in; only the fragment was read, so a
        # clean PathChanged fragment + override.conf passed while systemd loaded both (live repro, astra2rev-probe.path)
        os.makedirs(f"{td}/ok.path.d")
        open(f"{td}/ok.path.d/override.conf", "w").write(f"[Path]\nDirectoryNotEmpty={inbox}\n")
        assert level_triggers(f"{td}/ok.path", [f"{td}/ok.path.d/override.conf"]) == [f"DirectoryNotEmpty={inbox}"]
        open(f"{td}/ok.path.d/reset.conf", "w").write(f"[Path]\nPathChanged=\nPathChanged={inbox}\n")
        assert level_triggers(f"{td}/ok.path", [f"{td}/ok.path.d/override.conf", f"{td}/ok.path.d/reset.conf"]) == []
        assert level_triggers(f"{td}/ok.path", [f"{td}/ok.path.d/gone.conf"])[0].startswith("unreadable")
        open(f"{td}/ok.path.d/unit.conf", "w").write(f"[Path]\nDirectoryNotEmpty={inbox}\nUnit=rq.service\n")
        assert level_triggers(f"{td}/ok.path", [f"{td}/ok.path.d/unit.conf"]) == []  # last Unit= wins: rq.service drains
        real_sh = sh

        def dropin_sh(cmd, *a, **k):
            if "list-timers" in cmd:
                return 0, "[]"
            if "list-units" in cmd:
                return 0, json.dumps([{"unit": "ok.path"}])
            assert "DropInPaths" in ",".join(cmd), cmd
            return 0, f"Id=ok.path\nFragmentPath={td}/ok.path\nDropInPaths={td}/ok.path.d/override.conf\n"
        sh = dropin_sh
        try:
            got = [u for u in units() if u["unit"] == "ok.path"]
        finally:
            sh = real_sh
        assert got and all(u["level"] == [f"DirectoryNotEmpty={inbox}"] for u in got), got
    with tempfile.TemporaryDirectory() as td:
        exe, noexe = f"{td}/ok.sh", f"{td}/plain.sh"
        for p, mode in ((exe, 0o755), (noexe, 0o644)):
            open(p, "w").write("#!/bin/sh\n")
            os.chmod(p, mode)
        assert hook_target_problem(f"bash {noexe} arg") is None and hook_target_problem(f'bash "{td}/gone.sh"').startswith("missing")
        assert hook_target_problem(exe) is None and hook_target_problem(noexe).startswith("not executable")
        assert hook_target_problem("bash -c 'echo hi'") is None and hook_target_problem("nosuchcmd-xyz").endswith("not on PATH")
        assert hook_target_problem("PYTHONPATH=/x/review-queue /usr/bin/python3 -m review_queue.artifact_hook") is None
        assert hook_target_problem(f"A=1 B=/x {td}/gone.sh").startswith("missing") and hook_target_problem("A=1") == "empty"
        assert hook_target_problem(f"/usr/bin/python3 {td}/gone.py").startswith("missing") and hook_target_problem(f'bash "$HOME/../..{noexe}"') is None
        json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"{td}/gone"}]}]}}, open(f"{td}/h.json", "w"))
        assert check_hooks((f"{td}/h.json",))[0]["status"] == "FAIL"
        # skill listing: newest initial record per entrypoint wins; message text never read
        os.makedirs(f"{td}/p/proj")
        rec = lambda ts, ep, n: json.dumps({"type": "attachment", "timestamp": ts, "entrypoint": ep, "attachment": {
            "type": "skill_listing", "isInitial": True, "skillCount": n, "content": "x" * 10}})
        open(f"{td}/p/proj/a.jsonl", "w").write(rec("2026-10-12T01:00:00Z", "cli", 90) + "\n")
        open(f"{td}/p/proj/b.jsonl", "w").write('{"message": "SECRET"}\n' + rec("2026-10-12T02:00:00Z", "cli", 70) + "\n")
        open(f"{td}/p/proj/c.jsonl", "w").write(rec("2026-10-12T03:00:00Z", "sdk-cli", 80) + "\n")
        s = {x["rule"]: x for x in check_skills(f"{td}/p")}
        assert s["skill listing (cli)"]["status"] == "PASS" and s["skill listing (sdk-cli)"]["status"] == "FAIL", s
        assert "SECRET" not in json.dumps(s)
        assert check_sizes({f"{td}/none.md": {"lines": 1}})[0]["status"] == "SKIP"
        lj = lambda d: (json.dump(d, open(f"{td}/lanes.json", "w")), check_routes(f"{td}/lanes.json")[0])[1]
        okl = {"lanes": {"claude.main": {"provider": "anthropic", "model": "m1"}, "a.x": {"provider": "anthropic", "model": "m1"},
                         "local.t": {"provider": "ollama-thor", "model": "q"}},
               "allowed_models": {"anthropic": ["m1"]},
               "routes": {"_how": "x", "r1": {"lane": "a.x", "use": "u", "why": "w", "source": "s"},
                          "r2": {"lane": "local.t", "use": "u", "why": "w", "source": "s"}}}
        assert lj(okl)["status"] == "PASS", lj(okl)
        bad = json.loads(json.dumps(okl))
        bad["routes"]["r3"] = {"lane": "gone", "use": "u", "why": "", "source": "s"}
        bad["lanes"]["a.y"] = {"provider": "anthropic", "model": "m2"}
        d = lj(bad)["detail"]
        assert lj(bad)["status"] == "FAIL" and all(x in d for x in ("missing lane 'gone'", "r3 has no why", "a.y model 'm2' not in", "a.y is used by no route")), d
        typo = json.loads(json.dumps(okl)); typo["lanes"]["a.x"]["provider"] = "anthropc"
        assert "a.x model 'm1' not in allowed_models.anthropc" in lj(typo)["detail"], lj(typo)
        assert check_routes(f"{td}/none.json")[0]["status"] == "FAIL"
        open(f"{td}/big.md", "w").write("x\n" * 5)
        assert check_sizes({f"{td}/big.md": {"lines": 4}})[0]["status"] == "FAIL"
        # pre-commit in a scratch repo: new over-budget boot.md refused, shrink of an over-budget file allowed
        repo = f"{td}/repo"
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        g = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, env=env)
        os.makedirs(repo)
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        hook = lambda: subprocess.run([sys.executable, os.path.abspath(__file__), "--pre-commit"], cwd=repo, env=env,
                                      capture_output=True, text=True).returncode
        open(f"{repo}/boot.md", "w").write("x" * 5000)
        g("add", "boot.md")
        assert hook() == 1
        g("commit", "-q", "--no-verify", "-m", "a")
        open(f"{repo}/boot.md", "w").write("x" * 4500)
        g("add", "boot.md")
        assert hook() == 0  # still over, but shrinking
        open(f"{repo}/boot.md", "w").write("x" * 4600)
        g("add", "boot.md")
        assert hook() == 0  # 4600 < committed 5000: still a shrink vs HEAD
        g("commit", "-q", "--no-verify", "-m", "b")
        open(f"{repo}/boot.md", "w").write("x" * 4700)
        g("add", "boot.md")
        assert hook() == 1  # grows an over-budget file
        os.makedirs(f"{repo}/sub")
        open(f"{repo}/sub/CLAUDE.md", "w").write("x\n" * 61)
        open(f"{repo}/boot.md", "w").write("ok\n")
        g("add", "-A")
        assert hook() == 1  # unnamed CLAUDE.md gets the 60-line default
        open(f"{repo}/sub/CLAUDE.md", "w").write("x\n" * 10)
        g("add", "-A")
        assert hook() == 0
    print("harness_lint self-test: ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for f in ("--json", "--dry-run", "--self-test", "--pre-commit", "--units"):
        ap.add_argument(f, action="store_true")
    a = ap.parse_args()
    if a.units:
        print(json.dumps(units()))
        return 0
    if a.self_test:
        return self_test()
    if a.dry_run:
        return dry_run()
    if a.pre_commit:
        return pre_commit()
    results = run_all()
    fails = sum(r["status"] == "FAIL" for r in results)
    if a.json:
        print(json.dumps({"date": dt.date.today().isoformat(), "fails": fails, "results": results}, indent=1, ensure_ascii=False))
    else:
        for r in results:
            print(f"{r['status']:4}  {r['rule']}: {r['detail']}")
        print(f"harness-lint: {fails} FAIL, {sum(r['status'] == 'SKIP' for r in results)} SKIP, {len(results)} rules")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
