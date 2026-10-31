"""common.py: the few helpers every lane-intel script shares (paths, HALT, log, alerts, lanes, headless Claude)."""
import json, os, re, subprocess, sys, tempfile, time
from datetime import datetime, timezone

TOOL = os.path.dirname(os.path.abspath(__file__))
HALT = __import__('os').path.expanduser("~/AI_Projects/_system/automations/HALT")
from route import lanes_path  # noqa: E402 (route.py sits beside this file)
LANES = lanes_path()  # $ROUTE_LANES, then $KIT_HOME/framework/_system/lanes.json, then ~/AI_Projects/_system/lanes.json
VAULT_OUT = __import__('os').path.expanduser("~/AI_Projects/_system/second-brain/Reference/lane-intel")
STATE = os.environ.get("LANE_INTEL_STATE", os.path.expanduser("~/.local/state/lane-intel"))
CLAUDE = __import__('os').path.expanduser("~/.local/bin/claude")
ROUTE = "headless-summary"  # task class in lanes.json "routes" for the summarize calls


def halted():
    if os.path.exists(HALT):
        print("HALT present"); return True
    return False


def log(msg):
    print(msg, flush=True)
    subprocess.run(["logger", "-t", "lane-intel", "--", msg[:900]], check=False)


def load_json(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(data, open(tmp, "w"), indent=1, sort_keys=True)
    os.replace(tmp, path)


def notify(msg, dry_run=False):
    """One Telegram line. Under run.sh (LANE_INTEL_NOTIFY_FILE set) lines are collected and sent once per run."""
    log(f"notify: {msg}")
    if dry_run:
        return
    f = os.environ.get("LANE_INTEL_NOTIFY_FILE")
    if f:
        open(f, "a").write(msg.replace("\n", " ") + "\n"); return
    try:  # tg: odin's forwards to heimdall over ssh; with no Telegram configured it logs that and exits 0
        subprocess.run(["tg", "lane-intel", "lane-intel: " + msg], check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"telegram send failed: {e}")


def alert_once(key, msg, dry_run=False):
    """Notify only on the first occurrence of a condition; clear(key) re-arms it."""
    path = os.path.join(STATE, "alerts.json")
    flags = load_json(path, {})
    if key in flags:
        log(f"(already alerted {key}) {msg}"); return
    notify(msg, dry_run)
    if not dry_run:
        flags[key] = datetime.now(timezone.utc).isoformat(timespec="seconds"); save_json(path, flags)


def clear(key):
    path = os.path.join(STATE, "alerts.json")
    flags = load_json(path, {})
    if flags.pop(key, None):
        save_json(path, flags)


def lanes(path=None):
    path = path or LANES
    if not os.path.isfile(path):
        sys.exit(f"lane-intel: no lanes.json at {path} (set ROUTE_LANES to one, or KIT_HOME to the kit clone)")
    return json.load(open(path))


def canon(model, allowed):
    """A spoken model name -> its allowed_models id when that is unambiguous: 'GPT-6.1' -> gpt-6.1-sol (the only GPT-6.1
    variant), 'Opus 5.5' -> claude-opus-5-5. 'Opus 5' or 'GPT-6' (several variants) stay as said."""
    key = lambda m: re.sub(r"[^a-z0-9]", "", m.lower()).removeprefix("claude")  # noqa: E731
    ids = [m for ms in allowed.values() for m in ms]
    k = key(model or "")
    if not k:
        return model
    hits = [m for m in ids if key(m) == k] or [m for m in ids if any(key(m) == k + v for v in ("sol", "astra", "luna"))]
    return hits[0] if len(hits) == 1 else model


def claude_json(prompt, schema, max_turns=3, timeout=900):
    """One headless Claude call with structured output; model/effort from the headless-summary route. Returns (dict, cost)."""
    L = lanes()
    lane = L["lanes"][L["routes"][ROUTE]["lane"]]
    cmd = [CLAUDE, "-p", "--model", lane["model"], "--effort", lane["effort"], "--permission-mode", "dontAsk",
           "--permission-prompts", "none", "--max-turns", str(max_turns), "--output-format", "json",
           "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--json-schema", json.dumps(schema)]
    t = time.time()
    # the prompt goes over stdin: a long transcript can pass the 128 KiB single-argv limit
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "CLAUDE_SPAWN": "1"}, cwd=tempfile.gettempdir())
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"claude exit {r.returncode}: {(r.stderr or r.stdout)[-400:]}")
    cost = float(d.get("total_cost_usd") or 0)
    os.makedirs(STATE, exist_ok=True)
    open(os.path.join(STATE, "costs.jsonl"), "a").write(json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "usd": cost, "model": lane["model"],
         "effort": lane["effort"], "caller": os.path.basename(sys.argv[0])}) + "\n")
    u = d.get("usage") or {}
    tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    subprocess.run([sys.executable, __import__('os').path.expanduser("~/AI_Projects/_system/automations/record_spend.py"),
                    __import__('os').path.expanduser("~/AI_Projects/_system/automations"), "lane-intel", str(tokens)], check=False, capture_output=True)
    log(f"claude {lane['model']}/{lane['effort']}: ${cost:.3f} in {time.time() - t:.0f}s, {d.get('num_turns')} turns, {tokens} tokens "
        f"(cache write {u.get('cache_creation_input_tokens')}, cache read {u.get('cache_read_input_tokens')})")
    out = d.get("structured_output")
    if d.get("is_error") or not isinstance(out, dict):
        raise RuntimeError(f"claude returned no structured output: {str(d.get('result'))[:300]}")
    return out, cost
