#!/usr/bin/env python3
"""open-loops close — zero-token vault closure.

Reads <root>/reconciled/*.json (verdicts), <root>/results/*.json (build results, each with `loop_ids`),
the verified-id set (<root>/checks.json {"verified":[...]} and/or --verified ids), optional <root>/sol/*-review.md,
and the plan's "## 3. USER ASKS" section. Appends ONE dated sweep entry per project NOTES.md (idempotent by
marker `<!-- id:<run>-sweep -->`) and writes the canonical record Projects/workflow/open-loops-<date>.md,
where <date> is the run id without its `ol-` prefix (run `ol-2026-10-02` -> `open-loops-2026-10-02.md`).

A built item is "Done" only if its result status is `done` AND its item_id is verified. Everything else keeps its
status and blocker. Projects with no vault directory are listed under "Unmapped projects" in the record.

  close.py --root DIR --run ol-YYYY-MM-DD [--plan FILE] [--verified id ...] [--dry-run]
"""
import argparse, glob, json, os, re, sys

VAULT = __import__('os').path.expanduser("~/AI_Projects/_system/second-brain/Projects")
HOME = {  # projects without a vault dir of their own -> where their lines go
    "home-network": "fleet", "loki": "fleet", "raven": "fleet", "raven-setup": "fleet", "thor": "fleet",
    "example-console": "fleet", "home": "fleet", "media-stack": "fleet", "hardware": "fleet",
    "example-study": "example-project-h", "study": "example-project-h", "printing": "example-hobby",
    "mobile": "web", "omarchy": "workflow",
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")

def load(root):
    recon = {}
    for f in sorted(glob.glob(f"{root}/reconciled/*.json")):
        for l in json.load(open(f))["loops"]:
            if l["id"] in recon: print(f"warn: duplicate loop id {l['id']} ({f})", file=sys.stderr)
            recon[l["id"]] = l
    results = {}
    for f in sorted(glob.glob(f"{root}/results/*.json")):
        r = json.load(open(f))
        if not isinstance(r, dict): continue  # other ledgers (asks-*.json, exec-*.json) are lists; not build results
        if not ID_RE.match(r.get("item_id", "")): print(f"warn: skipping result with bad item_id in {f}", file=sys.stderr); continue
        r.setdefault("loop_ids", []); results[r["item_id"]] = r
    sol = {}
    for f in sorted(glob.glob(f"{root}/sol/*-review*.md")):
        m = re.findall(r"^VERDICT:\s*(\w+)", open(f).read(), re.M)
        key = re.sub(r"-review(-\d+)?\.md$", "", os.path.basename(f))
        if m: sol[key] = m[-1]  # the latest review file for a key wins (sorted order: review.md < review-2.md)
    verified = set()
    cj = f"{root}/checks.json"
    if os.path.exists(cj): verified |= set(json.load(open(cj)).get("verified", []))
    return recon, results, sol, verified

def asks_by_project(plan_path):
    if not plan_path or not os.path.exists(plan_path): return {}
    txt = open(plan_path).read()
    m = re.search(r"^## 3\. USER ASKS.*?(?=^## 4\.)", txt, re.S | re.M)
    if not m: return {}
    out, cur = {}, None
    for line in m.group(0).splitlines():
        h = re.match(r"^### (.+)", line)
        if h: cur = h.group(1).strip(); out[cur] = []; continue
        if cur and line.startswith("- "): out[cur].append(line[2:].strip())
    return out

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--root", required=True); ap.add_argument("--run", required=True)
    ap.add_argument("--plan"); ap.add_argument("--verified", nargs="*", default=[]); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not re.match(r"^ol-\d{4}-\d{2}-\d{2}$", a.run): sys.exit("--run must look like ol-YYYY-MM-DD")
    date = a.run[3:]
    recon, results, sol, verified = load(a.root); verified |= set(a.verified)
    asks = asks_by_project(a.plan or f"{a.root}/PLAN-open-loops-v2.md")
    # exact join: loop id -> list of (result, state)
    by_loop = {}
    for r in results.values():
        st = r["status"]
        for k, v in sol.items():
            if r["item_id"].startswith(k) and v == "REVISE": st = f"{st} (Sol P1 open)"
        state = "done" if (r["status"] == "done" and r["item_id"] in verified) else f"attempted: {st}" + ("" if r["item_id"] in verified or r["status"] != "done" else " (unverified)")
        for lid in r["loop_ids"]: by_loop.setdefault(lid, []).append((r, state))
    by_proj, unmapped = {}, {}
    for l in recon.values():
        p = HOME.get(l["project"], l["project"])
        (by_proj if os.path.isdir(f"{VAULT}/{p}") else unmapped).setdefault(p, []).append(l)
    summary = [f"# open-loops sweep {date}\n", f"_run `{a.run}` · reconciled {len(recon)} loops · build results {len(results)} · verified {len(verified)} · Sol verdicts {json.dumps(sol)}_\n"]
    touched = []
    for p, loops in sorted(by_proj.items()):
        notes = f"{VAULT}/{p}/NOTES.md"; existing = open(notes).read() if os.path.exists(notes) else ""
        marker = f"<!-- id:{a.run}-sweep -->"
        lines = [f"\n## {date} — open-loops sweep ({a.run})  {marker}"]
        done_now, attempted, closed, openl, need, ext = [], [], [], [], [], []
        for l in loops:
            hits = by_loop.get(l["id"], [])
            if any(s == "done" for _, s in hits): done_now.append((l, [r for r, s in hits if s == "done"]))
            elif hits: attempted.append((l, hits))
            elif l["verdict"] in ("closed", "stale_superseded"): closed.append(l)
            elif l["verdict"] in ("confirmed_open", "do_now"): openl.append(l)
            elif l["verdict"] == "needs_you": need.append(l)
            elif l["verdict"] == "waiting_external": ext.append(l)
        if done_now: lines.append("- **Done this sweep (verified):** " + "; ".join(f"{l['title']} — {', '.join(r['item_id'] for r in rs)}" for l, rs in done_now))
        if attempted:
            def _att(l, hs):
                states = ", ".join(r["item_id"] + " " + s for r, s in hs)
                return f"{l['title']} — {states}; blocker: {hs[0][0].get('could_not_do', '') or 'see result'}"
            lines.append("- **Attempted, not closed:** " + "; ".join(_att(l, hs) for l, hs in attempted))
        if closed: lines.append("- **Closed/superseded (evidence in reconciled record):** " + "; ".join(f"{l['title']} [{l['verdict']}]" for l in closed))
        if openl: lines.append("- **Still open (agent-doable, not run this sweep):** " + "; ".join(f"{l['title']} (tier {l['tier']}, risk {l['risk']})" for l in openl))
        if need: lines.append("- **Needs the user:** " + "; ".join(f"{l['title']} → {l['you_ask']}" for l in need if l.get("you_ask")))
        if ext: lines.append("- **Waiting external:** " + "; ".join(f"{l['title']} (resume: {(l['action_spec'] or l['verification'])[:140]})" for l in ext))
        lines.append(f"- Full record: `Projects/workflow/open-loops-{date}.md`.")
        summary.append(f"\n## {p}\n" + "\n".join(lines[1:]))
        if marker in existing: print(f"skip {p} (marker present)"); continue
        touched.append(notes)
        if not a.dry_run:
            with open(notes, "a") as f: f.write("\n".join(lines) + "\n")
    if unmapped:
        summary.append("\n## Unmapped projects (no vault dir; nothing appended)\n")
        for p, loops in sorted(unmapped.items()): summary.append(f"- **{p}**: " + "; ".join(f"{l['title']} [{l['verdict']}]" for l in loops))
        print(f"warn: {len(unmapped)} unmapped projects: {sorted(unmapped)}", file=sys.stderr)
    summary.append("\n## the user asks (verbatim from PLAN v2 §3)\n")
    for h, items in asks.items():
        summary.append(f"\n### {h}"); summary += [f"- {i}" for i in items]
    out = f"{VAULT}/workflow/open-loops-{date}.md"
    if not a.dry_run: open(out, "w").write("\n".join(summary) + "\n")
    print(f"notes appended: {len(touched)}; record: {out}{' (dry-run)' if a.dry_run else ''}")

if __name__ == "__main__": main()
