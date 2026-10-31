#!/usr/bin/env python3
"""open-loops prep — zero-token preparation for the open-loops workflow.

  prep.py condense [--days N] [--out DIR]   condense Claude transcripts (handoff-v2 prefilter) + Codex rollouts
  prep.py batches  [--out DIR] [--claude-batches N] [--codex-batches N]   write batches.json for the extract stage
  prep.py merge      <workflow-output.json> [--out DIR]   flatten+dedupe extractor output -> loops-raw.json
  prep.py reconciled <workflow-output.json> [--out DIR]   persist the reconcile phase's returned clusters -> reconciled/<cluster>.json
  prep.py results    <workflow-output.json> [--out DIR]   persist the build phase's returned records (incl. failed fallbacks) -> results/<id>.json

Default --out: $OPEN_LOOPS_DIR or ./.open-loops (git-ignored scratch). Uses only stdlib + handoff-v2's prefilter.
The `batches` vault window uses the same --days as `condense`.
"""
import sys, os, json, glob, time, re, argparse, collections
sys.path.insert(0, __import__('os').path.expanduser("~/AI_Projects/pipeline/handoff-v2"))
VAULT = __import__('os').path.expanduser("~/AI_Projects/_system/second-brain/Projects")

def clip(s, n): s = re.sub(r"\s+", " ", s or "").strip(); return s[:n] + ("…" if len(s) > n else "")

def condense_claude(days, out):
    from handoff import prefilter
    d = os.path.join(out, "condensed"); os.makedirs(d, exist_ok=True)
    state = json.load(open(__import__('os').path.expanduser("~/AI_Projects/pipeline/handoff-v2/state/transcripts.json")))
    cutoff = time.time() - days * 86400; index = []
    for path in sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=os.path.getmtime, reverse=True):
        if "/subagents/" in path or os.path.getmtime(path) < cutoff or os.path.getsize(path) < 20_000: continue
        size = os.path.getsize(path); key = "/".join(path.split("/")[-2:])[:-6]
        acked = state.get(key, {}).get("acked_offset", 0); sid = os.path.basename(path)[:8]
        try: r = prefilter.build(prefilter.read_records(path, 0, size, None))
        except Exception as e: index.append({"sid": sid, "path": path, "error": str(e)}); continue
        cwd = max(r.signals.get("cwds", {}).items(), key=lambda kv: kv[1])[0] if r.signals.get("cwds") else "?"
        o = os.path.join(d, f"{sid}.txt")
        with open(o, "w") as f:
            f.write(f"# session {sid}  cwd={cwd}  size={size}  mtime={time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(path)))}  acked_by_handoff={acked}/{size} ({100*acked//max(size,1)}%)\n")
            f.write(f"# edited_paths: {', '.join(r.edited_paths[:40])}\n\n{r.render(max_actions=250, max_conv_bytes=60_000)}\n\n=== LAST 60 TIMELINE LINES ===\n")
            f.write("\n".join(line for _, line in r.timeline[-60:]))
        index.append({"sid": sid, "path": path, "cwd": cwd, "size": size, "pct_acked": 100*acked//max(size,1), "condensed": o,
                      "condensed_bytes": os.path.getsize(o), "mtime": time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(path)))})
    json.dump(index, open(os.path.join(d, "index.json"), "w"), indent=1)
    print(f"claude: {len(index)} sessions -> {sum(i.get('condensed_bytes',0) for i in index)//1024}K ({sum(1 for i in index if i.get('pct_acked',100)<90)} not yet distilled by handoff-v2)")

def condense_codex(days, out):
    d = os.path.join(out, "condensed-codex"); os.makedirs(d, exist_ok=True)
    cutoff = time.time() - days * 86400; index = []
    for path in sorted(glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True), key=os.path.getmtime, reverse=True):
        if os.path.getmtime(path) < cutoff or os.path.getsize(path) < 30_000: continue
        m = re.search(r"rollout-(\S+?)-01a0", os.path.basename(path)); sid = m.group(1) if m else os.path.basename(path)[:30]
        cwd = model = "?"; lines = []; users = turns = 0; edits = set()
        for line in open(path, errors="ignore"):
            try: r = json.loads(line)
            except Exception: continue
            t = r.get("type"); p = r.get("payload", {}) or {}
            if t == "session_meta": cwd = p.get("cwd", cwd)
            elif t == "turn_context": cwd = p.get("cwd", cwd); model = p.get("model", model)
            elif t == "event_msg" and p.get("type") == "thread_settings_applied": model = (p.get("thread_settings") or {}).get("model", model)
            elif t == "event_msg" and p.get("type") == "item_completed" and (p.get("item") or {}).get("type") == "UserMessage":
                txt = " ".join(c.get("text", "") for c in p["item"].get("content", []) if isinstance(c, dict))
                if "<skills_instructions>" in txt or txt.startswith("<environment_context>"): continue
                users += 1; lines.append(f"[USER] {clip(txt, 1500)}")
            elif t == "event_msg" and p.get("type") == "task_complete":
                turns += 1; lines.append(f"[AGENT-FINAL] {clip(p.get('last_agent_message', ''), 2500)}")
            elif t == "response_item" and p.get("type") in ("custom_tool_call", "function_call"):
                for e in re.findall(r"\*\*\* (?:Update|Add|Delete) File: (\S+)", str(p.get("input") or p.get("arguments") or "")): edits.add(e)
        if not users and not turns: continue
        o = os.path.join(d, f"{sid}.txt")
        with open(o, "w") as f:
            f.write(f"# codex session {sid}  cwd={cwd}  model={model}  mtime={time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(path)))}  user_msgs={users} turns={turns}\n# edited_files: {', '.join(sorted(edits))[:1500]}\n\n")
            f.write("\n".join(lines))
        index.append({"sid": sid, "path": path, "cwd": cwd, "model": model, "condensed": o, "condensed_bytes": os.path.getsize(o),
                      "mtime": time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(path)))})
    json.dump(index, open(os.path.join(d, "index.json"), "w"), indent=1)
    print(f"codex: {len(index)} sessions -> {sum(i['condensed_bytes'] for i in index)//1024}K")

def pack(items, n):
    items = sorted(items, key=lambda c: -c["condensed_bytes"]); b = [[] for _ in range(n)]; sz = [0] * n
    for it in items: i = sz.index(min(sz)); b[i].append(it); sz[i] += it["condensed_bytes"]
    return [x for x in b if x]

def batches(out, n_claude, n_codex, days=14):
    cl = [c for c in json.load(open(os.path.join(out, "condensed/index.json"))) if "error" not in c and c["condensed_bytes"] > 1500]
    cx = [c for c in json.load(open(os.path.join(out, "condensed-codex/index.json"))) if c["condensed_bytes"] > 600]
    und = [c for c in cl if c["pct_acked"] < 90]; ack = [c for c in cl if c["pct_acked"] >= 90]; B = []
    f = lambda c: {k: c[k] for k in ("sid", "path", "cwd", "mtime", "model") if k in c} | {"path": c["condensed"]}
    for i, g in enumerate(pack(und, max(1, n_claude // 3))): B.append({"id": f"claude-undistilled-{i+1}", "kind": "claude", "priority": "high (never distilled by handoff-v2 — no vault record exists)", "files": [f(c) for c in g]})
    for i, g in enumerate(pack(ack, n_claude)): B.append({"id": f"claude-distilled-{i+1}", "kind": "claude", "priority": "normal (handoff-v2 distilled these at ~66/80 quality — hunt for loops it missed)", "files": [f(c) for c in g]})
    for i, g in enumerate(pack(cx, n_codex)): B.append({"id": f"codex-{i+1}", "kind": "codex", "priority": "normal (Codex sessions are never distilled into the vault; only user prompts + final agent reports kept)", "files": [f(c) for c in g]})
    cutoff = time.time() - days * 86400
    slugs = sorted({os.path.basename(os.path.dirname(p)) for p in glob.glob(f"{VAULT}/*/RESUME.md") + glob.glob(f"{VAULT}/*/NOTES.md") if os.path.getmtime(p) > cutoff})
    for i in range(3):
        g = slugs[i::3]
        B.append({"id": f"vault-{i+1}", "kind": "vault", "priority": "normal (hand-written + distilled project state)", "files": [{"slug": s, "dir": f"{VAULT}/{s}", "read": [x for x in ("RESUME.md", "NOTES.md", "PLAN.md") if os.path.exists(f"{VAULT}/{s}/{x}")]} for s in g]})
    json.dump(B, open(os.path.join(out, "batches.json"), "w"), indent=1)
    for b in B: print(b["id"], len(b["files"]), "files", sum(os.path.getsize(x["path"]) for x in b["files"] if "path" in x) // 1024, "KB")
    print("batch ids:", json.dumps([b["id"] for b in B]))

ALIAS = {"example-client": "example_client", "example_client": "example_client", "example_app": "example_app", "example_app": "example_app", "example-project-f": "example-project-f", "workflow": "workflow", "handoff": "handoff-v2", "freellmapi": "fleet", "example-photos": "example-photos", "web": "web", "lanes": "lanes"}
def toks(s): return set(re.findall(r"[a-z0-9]{3,}", s.lower()))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
def _result_of(src):
    wrap = json.load(open(src)); data = wrap.get("result", wrap) if isinstance(wrap, dict) else wrap
    if isinstance(data, str): data = json.loads(data)
    return data

def persist_reconciled(src, out):
    data = _result_of(src); d = os.path.join(out, "reconciled"); os.makedirs(d, exist_ok=True); n = 0
    for r in data.get("reconciled", []):
        if not isinstance(r, dict) or not ID_RE.match(str(r.get("cluster", ""))): print("skip: bad cluster record"); continue
        json.dump(r, open(os.path.join(d, f"{r['cluster']}.json"), "w"), indent=1); n += 1
    print(f"persisted {n} clusters; failed: {data.get('failed')}")

def persist_results(src, out):
    data = _result_of(src); d = os.path.join(out, "results"); os.makedirs(d, exist_ok=True); n = 0
    for r in (data if isinstance(data, list) else data.get("results", [])):
        if not isinstance(r, dict) or not ID_RE.match(str(r.get("item_id", ""))): print("skip: bad result record"); continue
        json.dump(r, open(os.path.join(d, f"{r['item_id']}.json"), "w"), indent=1); n += 1
    print(f"persisted {n} results")

def merge(src, out):
    data = _result_of(src)
    loops, closed, notes = [], [], {}
    for b in data["batches"]:
        for l in b["loops"]: l["batch"] = b["batch"]; l["project"] = ALIAS.get(l["project"].lower().strip(), l["project"].lower().strip()); loops.append(l)
        for c in b["closed_loops"]: c["batch"] = b["batch"]; closed.append(c)
        notes[b["batch"]] = b.get("notes", "")
    groups = []
    for l in sorted(loops, key=lambda x: x["last_seen"], reverse=True):
        t = toks(l["title"]) | toks(l["detail"][:150]); hit = None
        for g in groups:
            if g["project"] != l["project"]: continue
            gt = toks(g["title"]) | toks(g["detail"][:150])
            if len(t & gt) / max(1, min(len(t), len(gt))) >= 0.6: hit = g; break
        if hit: hit["dups"].append({"title": l["title"], "batch": l["batch"], "status": l["status"], "last_seen": l["last_seen"], "evidence": l["evidence"][:1]})
        else: l["dups"] = []; groups.append(l)
    json.dump({"loops": groups, "closed": closed, "notes": notes}, open(os.path.join(out, "loops-raw.json"), "w"), indent=1)
    byp = collections.Counter(l["project"] for l in groups)
    print(f"raw {len(loops)} -> {len(groups)} loops, {len(closed)} closed; by status {dict(collections.Counter(l['status'] for l in groups))}")
    for p, n in byp.most_common(): print(f"  {p:32s} {n}")

if __name__ == "__main__":
    def positive(v):
        i = int(v)
        if i < 1: raise argparse.ArgumentTypeError("must be >= 1")
        return i
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["condense", "batches", "merge", "reconciled", "results"]); ap.add_argument("src", nargs="?")
    ap.add_argument("--days", type=positive, default=14); ap.add_argument("--out", default=os.environ.get("OPEN_LOOPS_DIR", ".open-loops"))
    ap.add_argument("--claude-batches", type=positive, default=5); ap.add_argument("--codex-batches", type=positive, default=2)
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    if a.cmd in ("merge", "reconciled", "results") and not a.src: ap.error(f"{a.cmd} needs the workflow output json path")
    if a.cmd == "condense": condense_claude(a.days, a.out); condense_codex(a.days, a.out)
    elif a.cmd == "batches": batches(a.out, a.claude_batches, a.codex_batches, a.days)
    elif a.cmd == "reconciled": persist_reconciled(a.src, a.out)
    elif a.cmd == "results": persist_results(a.src, a.out)
    else: merge(a.src, a.out)
