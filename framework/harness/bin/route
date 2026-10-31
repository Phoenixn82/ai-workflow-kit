#!/usr/bin/env python3
"""route: which lane (model + effort) fits a task class, and how to dispatch it. There is no default lane.
Reads the "routes" table in _system/lanes.json on every call (lanes say model + effort; routes say which lane fits which job).
  route list                 compact table: class, lane, model, effort, use
  route <class>              how to dispatch that class (Agent / Workflow / claude -p, cx, or lane)
  route --json [<class>]     the same as JSON, for scripts
  route --cx <stage|class>   "model effort" for cx: plan|check|build, or a class whose lane is codex.* (exit 2 otherwise)
  route --workflow-args      JSON {routes: {class: {model, effort}} for Claude-lane classes, codex: [classes shifted to
                             Codex right now]}: a Workflow script can't run route, so the main loop passes this in args
  route --lanes-path         the lanes.json this would read: $ROUTE_LANES, else $KIT_HOME/framework/_system/lanes.json
                             when that exists, else ~/AI_Projects/_system/lanes.json, else (when that is missing) the
                             table in the tree route itself lives in (a fresh kit clone: <clone>/framework/_system)
  route --self-test          synthetic checks, no network
Unknown class: exit 2 and list the classes.
Quota pace (fluid, no fixed default): when Claude's 7-day usage runs ahead of the week (or the 5-hour window is at 80%+),
a class whose route names an "alt" lane resolves to it (a Codex lane), so the work stops drawing on Claude's limit. Data:
the agents widget's usage records; stale or missing data = no shift. ROUTE_PACE=over|ok forces it."""
import json, os, sys
from datetime import datetime, timedelta, timezone

AIP_LANES = __import__('os').path.expanduser("~/AI_Projects/_system/lanes.json")
# The table two levels above this file's real location: _tools/lane-intel/route.py and harness/bin/route both sit two
# levels below the folder that holds _system/, in AI_Projects and in a kit clone's framework/ alike.
OWN_LANES = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "_system", "lanes.json"))


def lanes_path(env=os.environ, aip=AIP_LANES, own=OWN_LANES):
    """ROUTE_LANES, then the kit clone's table ($KIT_HOME/framework/_system/lanes.json, if present), then AI_Projects;
    when AI_Projects has no table, the one in the tree this file lives in (a kit clone run in place, no env set)."""
    if env.get("ROUTE_LANES"):
        return env["ROUTE_LANES"]
    kit = os.path.join(env.get("KIT_HOME") or "", "framework", "_system", "lanes.json")
    if env.get("KIT_HOME") and os.path.isfile(kit):
        return kit
    return own if not os.path.isfile(aip) and os.path.isfile(own) else aip


LANES = lanes_path()
ALIAS = {"claude-opus": "opus", "claude-haiku": "haiku", "claude-fable": "fable", "claude-sonnet": "sonnet"}
NOTE = {"claude.main": "no dispatch: this is the user's own session (lane claude.main, applied by apply_lanes.py)",
        "claude.distill": "automatic: handoff-v2 runs it at SessionEnd (HANDOFF_DISTILLER=auto); nothing to dispatch by hand"}


USAGE = os.path.expanduser("~/.local/state/omarchy/agents/usage")  # claude.json / codex.json, refreshed by the bar widget
WEEK = timedelta(days=7)


class RouteError(Exception):
    pass


def pace(usage_dir=USAGE, env=os.environ, now=None):
    """(shift, note, over). over = Claude's weekly use is more than 5 points ahead of the share of the week gone, or the
    5-hour window is at 80%+. shift = over and Codex is not near its own cap (an unknown or expired Codex limit counts
    as room). Stale data (old file, or the collector's "stale (age)" fallback) = not over."""
    if env.get("ROUTE_PACE") in ("over", "ok"):
        f = env["ROUTE_PACE"] == "over"
        return f, f"forced by ROUTE_PACE={env['ROUTE_PACE']}", f
    now = now or datetime.now(timezone.utc)
    try:
        u = json.load(open(os.path.join(usage_dir, "claude.json")))
        if now - datetime.fromisoformat(u["updatedAt"]) > timedelta(hours=3) or u.get("usageStatusText", "").startswith("stale"):
            return False, f"Claude usage data is stale ({u.get('usageStatusText') or u['updatedAt'][:16]}); no shift", False
        lim = {x["label"].split()[0]: x for x in u.get("limits", [])}
        wk, ses = lim.get("Weekly"), lim.get("Session", {})
        gone = min(max(1 - (datetime.fromisoformat(wk["resetsAt"]) - now) / WEEK, 0.0), 1.0) if wk else None
        used, s5 = (wk or {}).get("percent", 0), ses.get("percent", 0)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return False, f"no Claude usage data ({type(e).__name__}); no shift", False
    if gone is None and not ses:
        return False, "no Claude limits in the usage data; no shift", False
    note = (f"Claude weekly {used:.0%} used with {gone:.0%} of the week gone, " if gone is not None else "Claude ") + f"5-hour window {s5:.0%}"
    over = (gone is not None and used > gone + 0.05) or s5 >= 0.8
    if not over:
        return False, note + ": on pace", False
    try:
        cx = [x["percent"] for x in json.load(open(os.path.join(usage_dir, "codex.json"))).get("limits", [])
              if not x.get("resetsAt") or datetime.fromisoformat(x["resetsAt"]) > now]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        cx = []
    if cx and max(cx) >= 0.9:
        return False, note + f": over pace, but Codex is at {max(cx):.0%} of its own limit; no shift", True
    return True, note + ": over pace, classes with an alt lane run on it", True

def load(path=LANES):
    return json.load(open(path))


def routes(L):
    return {k: v for k, v in L.get("routes", {}).items() if not k.startswith("_")}


def alias(model):
    return next((a for p, a in ALIAS.items() if model.startswith(p + "-")), model)


def unknown(L, cls):
    return RouteError(f"route: unknown task class {cls!r}. Classes: {', '.join(routes(L))}")


def resolve(L, cls, over=False):
    r = routes(L).get(cls)
    if r is None:
        raise unknown(L, cls)
    for k in ("lane", "alt"):
        if r.get(k) and r[k] not in L["lanes"]:
            raise RouteError(f"route: class {cls!r} points at {'alt ' if k == 'alt' else ''}lane {r[k]!r}, which lanes.json does not define")
    name = r["alt"] if over and r.get("alt") else r["lane"]
    lane = L["lanes"][name]
    m, e = lane["model"], lane["effort"]
    d = {"class": cls, "lane": name, "provider": lane["provider"], "model": m, "effort": e,
         "use": r.get("use", ""), "why": r.get("why", ""), "source": r.get("source", "")}
    if r.get("alt"):
        d["alt"] = r["alt"]
    if name != r["lane"]:
        d["shifted_from"] = r["lane"]
    if lane["provider"] == "anthropic":
        a = alias(m)
        d["dispatch"] = {"agent": {"model": a, "effort": e}, "workflow": {"model": a, "effort": e},
                         "headless": f"claude -p --model {m} --effort {e}"}
        if r.get("agent"):  # a lean agent type: starts without CLAUDE.md / the memory index
            d["dispatch"]["agent"]["subagent_type"] = d["dispatch"]["workflow"]["agentType"] = r["agent"]
    elif name.startswith("codex."):
        sb = "workspace-write" if name == "codex.build" else "read-only"
        d["dispatch"] = {"cx": f"cx {cls} {sb} <outfile> '<prompt>'"}
    else:
        d["dispatch"] = {"lane": "lane <admitted-class> --tier private < input   (lane admission lists the classes)"}
    if name in NOTE:
        d["dispatch"]["note"] = NOTE[name]
    return d


def cx_lane(L, arg, over=False):
    """(lane name, model, effort) for cx. plan|check|build keep their old meaning; a class must route to a codex.* lane
    (its alt counts while Claude is over pace)."""
    if arg in ("plan", "check", "build"):
        name = "codex." + arg
    else:
        name = resolve(L, arg, over)["lane"]
        if not name.startswith("codex."):
            raise RouteError(f"cx: class {arg!r} routes to lane {name} ({L['lanes'][name]['model']}), not a Codex lane; "
                             f"dispatch it through the Agent tool, a Workflow agent() or `lane` (run `route {arg}`)")
    lane = L["lanes"][name]
    return name, lane["model"], lane["effort"]


def workflow_args(L, over=False):
    rows = [resolve(L, c, over) for c in routes(L)]
    return {"routes": {r["class"]: r["dispatch"]["workflow"] for r in rows if "workflow" in r["dispatch"]},
            "codex": [r["class"] for r in rows if "shifted_from" in r]}


def show(d):
    out = [f"{d['class']}: lane {d['lane']} = {d['model']} {d['effort']}", f"  use:    {d['use']}",
           f"  why:    {d['why']}", f"  source: {d['source']}"]
    if "shifted_from" in d:
        out.insert(1, f"  pace:   shifted from {d['shifted_from']} (Claude is over its weekly pace)")
    elif "alt" in d:
        out.insert(1, f"  alt:    {d['alt']} when Claude is over its weekly pace")
    x = d["dispatch"]
    if "agent" in x:
        st = f", subagent_type: \"{x['agent']['subagent_type']}\"" if "subagent_type" in x["agent"] else ""
        at = f", agentType: '{x['workflow']['agentType']}'" if "agentType" in x["workflow"] else ""
        out += [f"  Agent tool:       model: \"{x['agent']['model']}\", effort: \"{x['agent']['effort']}\"{st}",
                f"  Workflow agent(): {{model: '{x['workflow']['model']}', effort: '{x['workflow']['effort']}'{at}}}",
                f"  claude -p:        {x['headless']}"]
    for k in ("cx", "lane"):
        if k in x:
            out.append(f"  {k}: {x[k]}")
    if "note" in x:
        out.append(f"  note: {x['note']}")
    return "\n".join(out)


def table(L, over=False, note=""):
    rows = [resolve(L, c, over) for c in routes(L)]
    w = max(len(r["class"]) for r in rows), max(len(r["lane"]) + ("shifted_from" in r) for r in rows), max(len(r["model"]) for r in rows)
    out = [f"{'class':{w[0]}}  {'lane':{w[1]}}  {'model':{w[2]}}  effort  use"]
    for r in rows:
        use = r["use"] if len(r["use"]) <= 70 else r["use"][:67] + "..."
        lane = r["lane"] + ("*" if "shifted_from" in r else "")
        out.append(f"{r['class']:{w[0]}}  {lane:{w[1]}}  {r['model']:{w[2]}}  {r['effort']:6}  {use}")
    if L.get("not_routed"):
        out.append("not routed: " + "; ".join(L["not_routed"]))
    if note:
        out.append("pace: " + note + ("; * = shifted to its alt lane" if over else ""))
    return "\n".join(out)


def main(argv):
    try:
        if argv == ["--self-test"]:
            return self_test()
        if argv in (["-h"], ["--help"]):
            print(__doc__); return 0
        if argv == ["--lanes-path"]:
            print(LANES); return 0
        try:
            L = load()
        except (OSError, ValueError) as e:
            raise RouteError(f"route: cannot read {LANES}: {e} (set ROUTE_LANES to a lanes.json, or KIT_HOME to the kit clone)")
        over, note = pace()[:2]
        if argv in ([], ["list"]):
            print(table(L, over, note)); return 0
        if argv[0] == "--json" and len(argv) <= 2:
            print(json.dumps(resolve(L, argv[1], over) if len(argv) == 2 else
                             {"routes": [resolve(L, c, over) for c in routes(L)], "not_routed": L.get("not_routed", {}),
                              "pace": {"over": over, "note": note}}, indent=1))
            return 0
        if argv == ["--workflow-args"]:
            print(json.dumps(workflow_args(L, over))); return 0
        if argv[0] == "--cx" and len(argv) == 2:
            print(*cx_lane(L, argv[1], over)[1:]); return 0
        if len(argv) == 1 and not argv[0].startswith("-"):
            print(show(resolve(L, argv[0], over))); return 0
        print(__doc__, file=sys.stderr); return 2
    except RouteError as e:
        print(e, file=sys.stderr); return 2


def self_test():
    L = {"lanes": {"claude.subagent": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"},
                   "claude.sweep": {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "medium"},
                   "codex.check": {"provider": "openai", "model": "gpt-6.1-sol", "effort": "high"},
                   "codex.plan": {"provider": "openai", "model": "gpt-6-astra", "effort": "xhigh"},
                   "codex.build": {"provider": "openai", "model": "gpt-6.1-sol", "effort": "high"},
                   "local.text": {"provider": "ollama-thor", "model": "q:27b", "effort": "n/a"}},
         "routes": {"_how": "x", "feature-build": {"lane": "claude.subagent", "use": "u", "why": "w", "source": "s"},
                    "bulk-sweep": {"lane": "claude.sweep", "use": "u" * 90, "why": "w", "source": "s"},
                    "code-review": {"lane": "codex.check", "use": "u", "why": "w", "source": "s"},
                    "long-build": {"lane": "codex.build", "use": "u", "why": "w", "source": "s"},
                    "private-data": {"lane": "local.text", "use": "u", "why": "w", "source": "s"},
                    "broken": {"lane": "claude.gone", "use": "u", "why": "w", "source": "s"}},
         "not_routed": {"claude-sonnet-5-5": "dominated"}}
    assert list(routes(L)) == ["feature-build", "bulk-sweep", "code-review", "long-build", "private-data", "broken"]
    f = resolve(L, "feature-build")["dispatch"]
    assert f["agent"] == {"model": "opus", "effort": "high"} and f["workflow"] == {"model": "opus", "effort": "high"}, f
    assert f["headless"] == "claude -p --model claude-opus-5-5 --effort high"
    assert resolve(L, "bulk-sweep")["dispatch"]["workflow"] == {"model": "haiku", "effort": "medium"}
    L["routes"]["bulk-sweep"]["agent"] = "lean"
    bs = resolve(L, "bulk-sweep")
    assert bs["dispatch"]["agent"]["subagent_type"] == "lean" and bs["dispatch"]["workflow"]["agentType"] == "lean", bs
    assert "subagent_type: \"lean\"" in show(bs) and "agentType: 'lean'" in show(bs)
    del L["routes"]["bulk-sweep"]["agent"]
    assert resolve(L, "code-review")["dispatch"]["cx"] == "cx code-review read-only <outfile> '<prompt>'"
    assert "workspace-write" in resolve(L, "long-build")["dispatch"]["cx"]
    assert resolve(L, "private-data")["dispatch"]["lane"].startswith("lane <admitted-class> --tier private")
    for bad in ("nope", "_how"):
        try:
            resolve(L, bad); raise AssertionError(bad)
        except RouteError as e:
            assert "feature-build" in str(e) and "_how" not in str(e).split("Classes:")[1]
    try:
        resolve(L, "broken"); raise AssertionError("missing lane")
    except RouteError as e:
        assert "claude.gone" in str(e)
    assert cx_lane(L, "plan") == ("codex.plan", "gpt-6-astra", "xhigh") and cx_lane(L, "build")[0] == "codex.build"
    assert cx_lane(L, "code-review") == ("codex.check", "gpt-6.1-sol", "high")
    for bad, want in (("feature-build", "claude.subagent"), ("private-data", "local.text"), ("nope", "unknown")):
        try:
            cx_lane(L, bad); raise AssertionError(bad)
        except RouteError as e:
            assert want in str(e), e
    # alt lanes: used only while over pace; a missing alt lane is an error
    L["routes"]["feature-build"]["alt"] = "codex.build"
    assert resolve(L, "feature-build")["lane"] == "claude.subagent" and "shifted_from" not in resolve(L, "feature-build")
    sh = resolve(L, "feature-build", over=True)
    assert sh["lane"] == "codex.build" and sh["shifted_from"] == "claude.subagent" and "workspace-write" in sh["dispatch"]["cx"], sh
    assert cx_lane(L, "feature-build", over=True)[0] == "codex.build" and "pace:   shifted" in show(sh)
    assert resolve(L, "bulk-sweep", over=True)["lane"] == "claude.sweep"  # no alt: never shifts
    L["routes"]["bad-alt"] = {"lane": "claude.sweep", "alt": "codex.gone"}
    try:
        resolve(L, "bad-alt"); raise AssertionError("missing alt lane")
    except RouteError as e:
        assert "codex.gone" in str(e)
    del L["routes"]["bad-alt"]
    # pace: weekly use vs share of the week gone (+5 points slack), 5-hour >= 80%, Codex cap, stale data, ROUTE_PACE
    now = datetime(2026, 10, 13, 17, 0, tzinfo=timezone.utc)
    def pc(weekly, session=0.0, hours_left=144.0, codex=(), age_h=0.5, env=None, status=""):
        with tempfile.TemporaryDirectory() as d:
            lim = [{"label": "Session (5-hour)", "percent": session, "resetsAt": now.isoformat()},
                   {"label": "Weekly (7-day)", "percent": weekly, "resetsAt": (now + timedelta(hours=hours_left)).isoformat()}]
            json.dump({"updatedAt": (now - timedelta(hours=age_h)).isoformat(), "limits": lim, "usageStatusText": status},
                      open(f"{d}/claude.json", "w"))
            json.dump({"limits": [{"label": "Weekly", "percent": c, "resetsAt": (now + timedelta(hours=h)).isoformat()} for c, h in codex]},
                      open(f"{d}/codex.json", "w"))
            return pace(d, env or {}, now)
    import tempfile
    assert pc(0.35)[0] and "over pace" in pc(0.35)[1]          # 35% used, 14% of the week gone
    assert not pc(0.15)[0] and pc(0.15)[1].endswith("on pace")  # within 5 points of an even burn
    assert pc(0.05, session=0.85)[0]                            # 5-hour window nearly full
    assert not pc(0.95, hours_left=6)[0]                        # late week, on pace
    full = pc(0.35, codex=[(0.95, 24)])
    assert full[:1] == (False,) and "Codex is at 95%" in full[1] and full[2]  # still over pace, just no room to shift
    assert pc(0.35, codex=[(0.5, 24)])[0] and pc(0.35, codex=[(0.95, -1)])[0]  # an expired Codex window is room
    assert not pc(0.35, age_h=4)[0] and "stale" in pc(0.35, age_h=4)[1]
    assert not pc(0.35, status="stale (5h)")[0] and "stale (5h)" in pc(0.35, status="stale (5h)")[1]
    with tempfile.TemporaryDirectory() as d:  # a session-only record still triggers on the 5-hour window
        json.dump({"updatedAt": now.isoformat(), "limits": [{"label": "Session (5-hour)", "percent": 0.9}]}, open(f"{d}/claude.json", "w"))
        assert pace(d, {}, now)[0], pace(d, {}, now)
    assert pace("/nonexistent", {}, now) == (False, "no Claude usage data (FileNotFoundError); no shift", False)
    assert pc(0.0, env={"ROUTE_PACE": "over"})[0] and not pc(0.9, env={"ROUTE_PACE": "ok"})[0]
    del L["routes"]["broken"]
    wa = workflow_args(L, True)
    assert wa["codex"] == ["feature-build"] and "feature-build" not in wa["routes"], wa
    assert wa["routes"]["bulk-sweep"] == {"model": "haiku", "effort": "medium"} and "code-review" not in wa["routes"], wa
    assert workflow_args(L)["routes"]["feature-build"] == {"model": "opus", "effort": "high"} and not workflow_args(L)["codex"]
    t2 = table(L, True, "n")
    assert "codex.build* " in t2 and t2.endswith("pace: n; * = shifted to its alt lane"), t2
    t = table(L)
    assert t.splitlines()[0].startswith("class") and "..." in t and t.splitlines()[-1] == "not routed: claude-sonnet-5-5", t
    assert "Workflow agent(): {model: 'opus', effort: 'high'}" in show(resolve(L, "feature-build"))
    # lanes.json lookup order: ROUTE_LANES, then a kit clone that has the table, then AI_Projects, then route's own tree
    import tempfile
    with tempfile.TemporaryDirectory() as kit:
        k = os.path.join(kit, "framework", "_system", "lanes.json")
        aip, own = os.path.join(kit, "aip.json"), os.path.join(kit, "own.json")
        lp = lambda env: lanes_path(env, aip, own)  # noqa: E731
        assert lp({"KIT_HOME": kit}) == aip  # no table in the clone, none anywhere: name the AI_Projects path
        os.makedirs(os.path.dirname(k)); open(k, "w").write("{}")
        assert lp({"KIT_HOME": kit}) == k and lp({"KIT_HOME": kit, "ROUTE_LANES": "/x.json"}) == "/x.json"
        assert lp({}) == lp({"KIT_HOME": ""}) == lp({"ROUTE_LANES": ""}) == aip
        open(own, "w").write("{}")
        assert lp({}) == own and lp({"KIT_HOME": kit}) == k  # no AI_Projects table: route's own tree, after KIT_HOME
        open(aip, "w").write("{}")
        assert lp({}) == aip  # an AI_Projects table always wins over route's own tree
    assert OWN_LANES.endswith(os.path.join("_system", "lanes.json"))
    # the real table resolves end to end and the CLI exit codes hold (skipped when there is no table to read)
    if not os.path.isfile(LANES):
        print(f"route self-test passed (synthetic only: no lanes.json at {LANES}; set ROUTE_LANES or KIT_HOME)")
        return 0
    real = load()
    for c in routes(real):
        resolve(real, c)
    import contextlib, io
    def run(*argv):
        with contextlib.redirect_stdout(io.StringIO()) as o, contextlib.redirect_stderr(io.StringIO()) as e:
            rc = main(list(argv))
        return rc, o.getvalue(), e.getvalue()
    assert run("list")[0] == 0 and run()[0] == 0 and run("--help")[0] == 0
    rc, _, err = run("no-such-class")
    assert rc == 2 and "Classes:" in err, err
    rc, out, _ = run("--json")
    assert rc == 0 and len(json.loads(out)["routes"]) == len(routes(real))
    c0 = next(iter(routes(real)))
    assert json.loads(run("--json", c0)[1])["class"] == c0 and run(c0)[0] == 0
    rc, out, _ = run("--cx", "check")
    assert rc == 0 and out.split() == [real["lanes"]["codex.check"]["model"], real["lanes"]["codex.check"]["effort"]]
    print("route self-test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
