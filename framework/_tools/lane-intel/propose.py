#!/usr/bin/env python3
"""propose.py: compare validated claim sidecars + the newest slopalytics snapshot with _system/lanes.json.
APPLY (the user: "adjust the lane setup as they see fit") when Theo's newest explicit claim for a lane role names a model in
allowed_models and an effort other than max, differs from the lane, and is newer than the lane's last recorded change:
edit lanes.json (+history), run apply_lanes.py, log to Reference/lane-intel/changes.md, one Telegram line.
A Claude effort raise is HELD while Claude is over its weekly pace (route.pace) and re-checked on the next daily run.
Everything else (no effort named, model not allowed, avoid hits, slopalytics dominance) is only PROPOSED in changes.md.
claude.design, claude.sweep, claude.deep, claude.distill, codex.vision and local.text are never touched (not in MAP).
Each changes.md line names the task routes (lanes.json "routes") that use the lane, so a role claim shows which jobs move.
  propose.py [--out DIR] [--dry-run] [--lanes FILE]   |   propose.py --self-test"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile
from datetime import date, datetime
from common import LANES, STATE, TOOL, VAULT_OUT, halted, lanes, load_json, log, notify

MAP = {"build/anthropic": ["claude.main"], "subagent/anthropic": ["claude.subagent"], "review/anthropic": ["claude.judge"],
       "headless/anthropic": ["claude.headless"], "plan/openai": ["codex.plan"], "review/openai": ["codex.check"],
       "build/openai": ["codex.build", "codex.interactive"]}
AUTO = {n for ns in MAP.values() for n in ns}
EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]


ROLE_OF = {n: [k for k, ns in MAP.items() if n in ns] for n in AUTO}  # lane -> the role/provider keys that map to it


def fmt(model, effort):
    """'gpt-6-astra xhigh', 'gpt-6-astra', 'max': no empty slots or double spaces in changes.md."""
    return " ".join(x for x in (model, effort) if x)


def avoid_lanes(L, c):
    """Lanes an avoid claim is about: the lanes of its role; a build claim (also the prompt's bucket for general
    statements) also hits every lane of that provider running the named model or effort."""
    out = []
    for name in sorted(AUTO):
        lane = L["lanes"][name]
        if lane["provider"] != c["provider"] or c["model"] not in ("", lane["model"]) or c["effort"] not in ("", lane["effort"]):
            continue
        if f"{c['role']}/{c['provider']}" in ROLE_OF[name] or c["role"] == "build":
            out.append(name)
    return out


def norm(line):
    """Dedupe key for a changes.md proposal: no date/status prefix, routes tag, [mm:ss] or mm:ss offsets, whitespace runs."""
    line = re.sub(r"^- \d{4}-\d{2}-\d{2} (PROPOSED|RESOLVED \d{4}-\d{2}-\d{2}) ", "", line.strip())
    line = re.sub(r" \[routes: [^\]]*\]| -> resolved:.*$", "", line)
    return " ".join(re.sub(r"\[?\b\d{1,2}:\d{2}\b\]?", "", line).split())


def routes_tag(L, name):
    """' [routes: a, b]': the task classes whose route points at this lane."""
    rs = [k for k, v in L.get("routes", {}).items() if not k.startswith("_") and v.get("lane") == name]
    return f" [routes: {', '.join(rs) or 'none'}]"


def last_change(L, name):
    """Newest history date that touched this lane; entries not written by lane-intel count for every lane."""
    return max([h["date"] for h in L["history"] if h.get("by") != "lane-intel" or h["change"].startswith(name + ":")] or [""])


def raises(lane, c):
    """Same model, higher effort."""
    e = [x if x in EFFORTS else "none" for x in (lane["effort"], c["effort"])]
    return c["model"] == lane["model"] and EFFORTS.index(e[1]) > EFFORTS.index(e[0])


def plan(L, st, snap, over=False):
    apply, propose, held = [], [], []
    allowed = L["allowed_models"]
    for key, names in MAP.items():
        c = st.get("roles", {}).get(key)
        if not c:
            continue
        prov = key.split("/")[1]
        for name in names:
            lane = L["lanes"][name]
            if c["model"] == lane["model"] and c["effort"] in ("", lane["effort"]):
                continue
            why = f"Theo {c['date']} ({c['source']}): \"{c['quote'][:120]}\""
            if c["model"] not in allowed.get(prov, []):
                propose.append(f"{name}: Theo picks {fmt(c['model'], c['effort'])} for {key} (not in allowed_models). {why}")
            elif c["effort"] == "max":
                held.append(f"{name}: {c['model']} max ignored (never max)")
            elif c["date"] <= last_change(L, name):
                held.append(f"{name}: {c['model']} {c['effort'] or '?'} from {c['date']} is not newer than the lane's last change {last_change(L, name)}")
            elif c["model"] == "claude-fable-5-1":
                propose.append(f"{name}: Theo picks Fable for {key}; Fable is deliberate-pick only, not auto-applied. {why}")
            elif c.get("ambiguous"):
                propose.append(f"{name}: Theo's newest source names several picks for {key} ({' / '.join(c['ambiguous'])}); lane is {lane['model']} {lane['effort']}. {why}")
            elif not c["effort"]:
                propose.append(f"{name}: Theo picks {c['model']} (no effort named) for {key}; lane is {lane['model']} {lane['effort']}. {why}")
            elif over and prov == "anthropic" and raises(lane, c):
                held.append(f"{name}: {c['model']} {lane['effort']} -> {c['effort']} waits: Claude is over its weekly pace (re-checked next run)")
            else:
                apply.append((name, c))
    for c in st.get("avoid", {}).values():
        if not c["model"] and not c["effort"]:  # "avoid OpenAI models" with no model or effort names no lane to change
            held.append(f"provider-wide avoid ignored (no model or effort named): {c['provider']} ({c['date']}, {c['source']})")
            continue
        for name in avoid_lanes(L, c):
            lane = L["lanes"][name]
            newer = [st["roles"][k] for k in ROLE_OF[name] if k in st.get("roles", {}) and st["roles"][k]["model"] == lane["model"]
                     and (st["roles"][k]["date"], st["roles"][k].get("ts", 0)) >= (c["date"], c.get("ts", 0))]
            if newer:
                held.append(f"{name}: avoid {fmt(c['model'], c['effort'])} ({c['date']}) superseded by Theo's newer pick {newer[0]['source']}")
                continue
            propose.append(f"{name}: Theo says avoid {fmt(c['model'], c['effort'])} for {c['role']} work ({c['date']}, {c['source']}) but the lane uses {fmt(lane['model'], lane['effort'])}")
    if snap:
        from slopalytics import dominators, pick
        for name in sorted(AUTO):
            lane = L["lanes"][name]
            p = pick(snap["records"], lane["provider"], lane["model"], lane["effort"])
            for d in dominators(snap["records"], lane["provider"], lane["model"], lane["effort"]):
                propose.append(f"{name}: slopalytics {snap['fetchedAt'][:10]}: {d['family']} {d['effort']} dominates {lane['model']} {lane['effort']} "
                               f"(intelligence {d['intelligence']:.1f} vs {p['intelligence']:.1f}, ${d['cost']:.2f} vs ${p['cost']:.2f}/task)")
    propose = list(dict.fromkeys(p + routes_tag(L, p.split(":", 1)[0]) for p in propose))  # one line per proposal
    return apply, propose, held


def lanes_text(text, L):
    """Surgical edit keeps lanes.json's hand layout; falls back to a plain dump if the result doesn't round-trip."""
    t = text
    for name, lane in L["lanes"].items():
        for k in ("model", "effort"):
            t = re.sub(rf'^(\s*"{re.escape(name)}":.*?"{k}": )"[^"]*"', lambda m: m.group(1) + json.dumps(lane[k]), t, flags=re.M)
    t = re.sub(r'^(\s*"updated": )"[^"]*"', lambda m: m.group(1) + json.dumps(L["updated"]), t, flags=re.M)
    new = L["history"][len(json.loads(text)["history"]):]
    t = re.sub(r"\}(\s*\n\s*\]\s*\n\}\s*)$", lambda m: "}" + "".join(",\n    " + json.dumps(h) for h in new) + m.group(1), t)
    try:
        if json.loads(t) == L:
            return t
    except ValueError:
        pass
    return json.dumps(L, indent=2) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=VAULT_OUT); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lanes", default=LANES); ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if halted():
        return 0
    text = open(a.lanes).read()
    L = json.loads(text)
    from stance import claims, fold
    st = fold(claims(a.out, state=STATE))  # Recheck evidence even if the preceding stance step failed.
    last = load_json(os.path.join(STATE, "slopalytics", "last.json"), {})
    snap = load_json(last.get("snapshot", ""), None) if last else None
    from route import pace
    apply, propose, held = plan(L, st, snap, pace()[2])  # [2] = Claude over pace, whether or not Codex has room
    today = str(date.today())
    for h in held:
        log(f"held: {h}")
    cpath = os.path.join(a.out, "changes.md")
    old = open(cpath).read() if os.path.exists(cpath) else "---\ntype: lane-intel\nsource: changes\ndescription: Log of the lane changes lane-intel applied or proposed for the weekly review\n---\n\n# Lane changes by lane-intel\n\nAPPLIED = written to _system/lanes.json + apply_lanes.py. PROPOSED = for the user / the weekly review.\n\n"
    seen = {norm(ln) for ln in old.splitlines() if " PROPOSED " in ln or " RESOLVED " in ln}
    fresh = [p for p in propose if norm(p) not in seen]  # a standing or resolved proposal is logged once (offsets and route tag ignored)
    lines = []
    for name, c in apply:
        lane = L["lanes"][name]
        change = f"{name}: {lane['model']} {lane['effort']} -> {c['model']} {c['effort']}"
        L["history"].append({"date": today, "by": "lane-intel", "change": change, "source": f"{c['source']} \"{c['quote'][:160]}\""})
        lane.update(model=c["model"], effort=c["effort"])
        lines.append(f"- {today} APPLIED {change} (Theo {c['date']}, {c['source']}){routes_tag(L, name)}")
    lines += [f"- {today} PROPOSED {p}" for p in fresh]
    for ln in lines:
        log(ln)
    if a.dry_run:
        print(f"dry-run: {len(apply)} apply, {len(fresh)} new proposals ({len(propose) - len(fresh)} already logged), {len(held)} held; nothing written")
        return 0
    if apply:
        L["updated"] = today
        os.makedirs(os.path.join(STATE, "backups"), exist_ok=True)
        shutil.copy(a.lanes, os.path.join(STATE, "backups", f"lanes-{datetime.now():%Y%m%d-%H%M%S}.json"))
        open(a.lanes + ".tmp", "w").write(lanes_text(text, L)); os.replace(a.lanes + ".tmp", a.lanes)
        if a.lanes == LANES:
            try:
                r = subprocess.run([sys.executable, os.path.join(TOOL, "apply_lanes.py")], capture_output=True, text=True, timeout=60)
                rc = r.returncode
                log(f"apply_lanes.py exit {rc}: {r.stdout.strip()} {r.stderr.strip()[-300:]}")
            except (OSError, subprocess.TimeoutExpired) as e:
                rc = type(e).__name__
                log(f"apply_lanes.py did not finish: {e}")
            if rc:  # restore the old table (backup kept) so the next run sees the same diff and retries; nothing is logged APPLIED
                open(a.lanes + ".tmp", "w").write(text); os.replace(a.lanes + ".tmp", a.lanes)
                notify(f"apply_lanes.py FAILED ({rc}); lanes.json restored, the change stays pending for the next run")
                return 1
    if lines:
        os.makedirs(a.out, exist_ok=True)
        open(cpath, "w").write(old + "\n".join(lines) + "\n")
    if apply:
        notify("lanes changed: " + "; ".join(f"{n} -> {c['model']} {c['effort']} (Theo {c['date']})" for n, c in apply))
    elif fresh:
        notify(f"{len(fresh)} new lane proposal(s) in Reference/lane-intel/changes.md")
    return 0


def test_fabricated_claims_do_not_change_lanes():
    from pathlib import Path
    from unittest import mock
    from summarize import x_posts
    from stance import claims, fold
    import copy
    L = lanes(LANES)
    original = copy.deepcopy(L)
    p = {"id": "2109593403482948090", "time": "2026-10-12T10:33:56Z", "text": "Sol reviews everything on high"}
    c = {"role": "build", "provider": "openai", "model": "gpt-6-astra", "effort": "xhigh", "verdict": "use",
         "post_id": "9999999999999999999", "timestamp": "9999999999999999999", "quote": p["text"]}
    result = {"lane_claims": [c, {**c, "post_id": p["id"], "timestamp": p["id"], "quote": "Sol is best for all reviews"}],
              "summary": "synthetic", "harness_tips": []}
    with tempfile.TemporaryDirectory() as d, mock.patch("summarize.claude_json", return_value=(result, 0)), mock.patch("summarize.log"):
        res, note = x_posts([p], d)
        assert res["lane_claims"] == []
        assert plan(L, fold(claims(d)), None) == ([], [], [])
        assert L == original
        # A poisoned cached stance must not bypass source validation in main().
        state = Path(d) / "state"; state.mkdir()
        (state / "stance.json").write_text(json.dumps({"roles": {"build/openai": {**c, "date": "2999-01-01", "source": "fake"}}}))
        lp = Path(d) / "lanes.json"; lp.write_text(json.dumps(L))
        before = lp.read_bytes()
        notify = mock.Mock()
        with mock.patch.dict(globals(), STATE=str(state), halted=lambda: False, log=lambda msg: None, notify=notify), \
                mock.patch("route.pace", return_value=(0, 0, False)):
            assert main(["--out", d, "--lanes", str(lp)]) == 0
            assert lp.read_bytes() == before and not notify.called


def self_test():
    L = lanes(LANES)
    L["history"] = [{"date": "2026-10-01", "by": "claude", "change": "initial", "source": "x"}]
    c = lambda m, e, d, q="q": {"model": m, "effort": e, "date": d, "source": "video t", "quote": q}
    st = {"roles": {"review/openai": c("gpt-6-astra", "xhigh", "2026-10-05"), "build/anthropic": c("claude-opus-5-5", "max", "2026-10-05"),
                    "subagent/anthropic": c("claude-haiku-5-5", "", "2026-10-05"), "review/anthropic": c("claude-fable-5-1", "high", "2026-10-05"), "headless/anthropic": c("claude-sonnet-5-5", "high", "2026-09-01"),
                    "plan/openai": {**c("gpt-6.1-sol", "high", "2026-10-05"), "ambiguous": ["gpt-6.1-sol high", "gpt-6-astra xhigh"]}},
          "avoid": {}}
    apply, propose, held = plan(L, st, None)
    assert [(n, x["model"]) for n, x in apply] == [("codex.check", "gpt-6-astra")], apply
    assert any("never max" in h for h in held) and any("not newer" in h for h in held), held
    assert any(p.startswith("claude.subagent: Theo picks claude-haiku-5-5 (no effort") for p in propose), propose
    assert any(p.startswith("codex.plan: Theo's newest source names several") for p in propose), propose
    st_up = {"roles": {"build/anthropic": c(L["lanes"]["claude.main"]["model"], "xhigh", "2026-10-05")}, "avoid": {}}
    if L["lanes"]["claude.main"]["effort"] in ("low", "medium", "high"):  # an effort raise waits while over pace
        assert [n for n, _ in plan(L, st_up, None)[0]] == ["claude.main"]
        ap, _, hd = plan(L, st_up, None, over=True)
        assert not ap and any("over its weekly pace" in h for h in hd), hd
    assert any(p.startswith("claude.judge: Theo picks Fable") for p in propose), propose
    assert all("[routes: " in p for p in propose), propose
    jr = [k for k, v in L["routes"].items() if not k.startswith("_") and v["lane"] == "claude.judge"]
    assert any(p.startswith("claude.judge:") and p.endswith(f"[routes: {', '.join(jr)}]") for p in propose), propose
    assert not AUTO & {"claude.deep", "codex.vision", "claude.design", "claude.sweep", "claude.distill", "local.text"}, AUTO
    # avoid claims (changes.md 2026-10-12..14): no "avoid all  (" lines, role-aware, superseded by a newer pick, deduped
    sol, astra = L["lanes"]["codex.check"]["model"], L["lanes"]["codex.plan"]["model"]
    av = lambda role, m, e, d, ts=0: {**c(m, e, d), "role": role, "provider": "openai", "ts": ts}  # noqa: E731
    st_av = {"roles": {"review/openai": {**c(sol, "", "2026-10-14"), "ts": 2}},
             "avoid": {"a": av("build", "", "", "2026-10-10"), "b": av("build", sol, "", "2026-10-13", 1),
                       "c": av("build", astra, "", "2026-10-13"), "d": av("design", sol, "", "2026-10-13")}}
    _, pr, hd = plan(L, st_av, None)
    names = sorted(p.split(":")[0] for p in pr)
    assert any("provider-wide avoid ignored" in h for h in hd) and not any("avoid all" in p or "  " in p for p in pr), pr
    assert "codex.check" not in names and any(h.startswith("codex.check: avoid") and "superseded" in h for h in hd), (pr, hd)
    assert "codex.plan" in names and names.count("codex.build") == 1, names  # design avoid of Sol does not hit codex.build
    assert norm("- 2026-10-12 PROPOSED codex.plan: x  (video v [01:02]) y") == norm("codex.plan: x (video v [00:57]) y [routes: a]")
    assert norm("- 2026-10-12 RESOLVED 2026-10-18 codex.plan: x (video v 31:44) y -> resolved: kept") == norm("codex.plan: x (video v [31:44]) y")
    from common import canon
    assert canon("GPT-6.1", L["allowed_models"]) == "gpt-6.1-sol" and canon("Opus 5", L["allowed_models"]) == "Opus 5"
    text = open(LANES).read()
    L2 = json.loads(text)
    day = "2999-01-01"  # later than any real history entry
    L2["lanes"]["codex.check"]["effort"] = "xhigh"; L2["updated"] = day
    L2["history"].append({"date": day, "by": "lane-intel", "change": "codex.check: a -> b", "source": "s"})
    t = lanes_text(text, L2)
    assert json.loads(t) == L2 and len(t.splitlines()) == len(text.splitlines()) + 1, "surgical edit failed"
    assert last_change(L2, "codex.check") == day and last_change(L2, "codex.build") < day
    test_fabricated_claims_do_not_change_lanes()
    end_to_end()
    print("self-test passed")


def end_to_end():
    """main() on a scratch lanes.json (real layout, synthetic history) + fake settings.json / codex config.toml. Only temp
    files are written: no real lanes/settings, no journal lines, no Telegram."""
    import common, contextlib, io, apply_lanes
    from pathlib import Path
    g, env = globals(), "LANE_INTEL_NOTIFY_FILE"
    saved = ({k: g[k] for k in ("STATE", "halted", "log", "LANES", "TOOL")}, common.log, os.environ.get(env),
             (apply_lanes.LANES, apply_lanes.SETTINGS, apply_lanes.CODEX))
    logged = []
    with tempfile.TemporaryDirectory() as d:
        try:
            g.update(STATE=f"{d}/state", halted=lambda: False, log=logged.append); common.log = logged.append
            os.environ[env] = f"{d}/notify"
            text = re.sub(r'("history": \[\n).*?(\n  \]\n\}\n?)$', lambda m: m.group(1) + '    {"date": "2026-10-01", "by": "claude", '
                          '"change": "initial", "source": "test"}' + m.group(2), open(LANES).read(), flags=re.S)
            L = json.loads(text)
            assert len(L["history"]) == 1, "history rewrite failed"
            lp = f"{d}/lanes.json"; open(lp, "w").write(text)
            ln = L["lanes"]
            m, ci = ln["claude.main"], ln["codex.interactive"]
            json.dump({"model": m["model"], "effortLevel": m["effort"], "modelSettings": {m["model"]: {"effortLevel": m["effort"]}},
                       "env": {}}, open(f"{d}/settings.json", "w"))
            open(f"{d}/config.toml", "w").write(f'model = "{ci["model"]}"\nmodel_reasoning_effort = "{ci["effort"]}"\n')
            apply_lanes.LANES, apply_lanes.SETTINGS, apply_lanes.CODEX = lp, f"{d}/settings.json", f"{d}/config.toml"

            def quiet(argv):
                with contextlib.redirect_stdout(io.StringIO()):
                    return apply_lanes.main(argv)
            assert quiet(["--check"]) == 0, "fake settings start out of sync"
            new_eff = "xhigh" if ln["codex.build"]["effort"] != "xhigh" else "medium"
            os.makedirs(f"{d}/state")
            from summarize import store_sources, merge_claims, validate_claims
            fixture = [("build", "openai", ln["codex.build"]["model"], new_eff, "2026-10-05", "newer explicit pick"),
                       ("build", "anthropic", "claude-opus-5-5", "max", "2026-10-05", "go max"),
                       ("review", "anthropic", "claude-fable-5-1", "high", "2026-10-05", "Fable reviews"),
                       ("subagent", "anthropic", "claude-haiku-5-5", "medium", "2026-09-20", "older than the lane")]
            for i, (role, provider, model, effort, day, quote) in enumerate(fixture):
                note = Path(d) / "out" / "theo" / f"{day}-v{i}.md"
                note.parent.mkdir(parents=True, exist_ok=True); note.write_text("synthetic note")
                posts = [{"id": f"video:v{i}", "kind": "youtube", "time": day, "text": "[01:00] " + quote}]
                claim = {"post_id": f"video:v{i}", "role": role, "provider": provider, "model": model,
                         "effort": effort, "verdict": "use", "quote": quote}
                store_sources(note, posts); merge_claims(str(note) + ".claims.json", validate_claims([claim], posts))
            args = ["--lanes", lp, "--out", f"{d}/out"]
            os.makedirs(f"{d}/failtool")  # apply_lanes.py fails: lanes.json restored, exit 1, nothing APPLIED, retried next run
            open(f"{d}/failtool/apply_lanes.py", "w").write("import sys; sys.exit('stub apply failure')\n")
            g.update(LANES=lp, TOOL=f"{d}/failtool")
            assert main(args) == 1, "failed apply must exit non-zero"
            g.update(LANES=saved[0]["LANES"], TOOL=saved[0]["TOOL"])
            assert open(lp).read() == text, "lanes.json not restored after a failed apply"
            assert [open(f"{d}/state/backups/{b}").read() for b in os.listdir(f"{d}/state/backups")] == [text], "backup lost"
            assert not os.path.exists(f"{d}/out/changes.md"), "a failed apply was logged as APPLIED"
            notes = open(f"{d}/notify").read().splitlines()
            assert len(notes) == 1 and notes[0].startswith("apply_lanes.py FAILED"), notes
            shutil.rmtree(f"{d}/state/backups"); os.remove(f"{d}/notify")
            assert main(args) == 0
            L2 = json.load(open(lp))
            for name in ("codex.build", "codex.interactive"):  # build/openai maps to both
                assert L2["lanes"][name]["effort"] == new_eff, L2["lanes"][name]
            for name in ("claude.main", "claude.judge", "claude.subagent", "claude.deep", "codex.vision"):  # max, Fable, older claim, unmapped: never applied
                assert L2["lanes"][name] == ln[name], (name, L2["lanes"][name])
            hist = L2["history"][1:]
            assert [h["change"].split(":")[0] for h in hist] == ["codex.build", "codex.interactive"], hist
            assert all(h["by"] == "lane-intel" and "newer explicit pick" in h["source"] for h in hist), hist
            ch = open(f"{d}/out/changes.md").read()
            assert "APPLIED codex.build:" in ch and "APPLIED codex.interactive:" in ch and "[routes: " in ch and "PROPOSED claude.judge: Theo picks Fable" in ch, ch
            assert any(x.startswith("held: claude.main: claude-opus-5-5 max ignored") for x in logged), logged
            assert any(x.startswith("held: claude.subagent:") and "not newer than the lane's last change 2026-10-01" in x for x in logged), logged
            notes = open(f"{d}/notify").read().splitlines()
            assert len(notes) == 1 and notes[0].startswith("lanes changed: codex.build -> "), notes
            assert len(os.listdir(f"{d}/state/backups")) == 1
            assert quiet(["--check"]) == 1, "codex.interactive changed, so the codex config must show drift before the push"
            assert quiet([]) == 0 and quiet(["--check"]) == 0, "apply_lanes.py --check fails after the push"
            assert main(args) == 0 and json.load(open(lp)) == L2 and open(f"{d}/notify").read().splitlines() == notes, "second run re-applied"
        finally:
            g.update(saved[0]); common.log = saved[1]
            os.environ.pop(env, None) if saved[2] is None else os.environ.__setitem__(env, saved[2])
            apply_lanes.LANES, apply_lanes.SETTINGS, apply_lanes.CODEX = saved[3]


if __name__ == "__main__":
    sys.exit(main())
