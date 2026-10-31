#!/usr/bin/env python3
"""slopalytics.py: daily read of slopalytics.com (Theo's model dashboard; numbers from Artificial Analysis).
No API: index.html -> /assets/index-*.js (benchmark object literal, tokenized, never eval'd) -> /data/t3-usage.json.
Every fetch goes through the web skill (`web read --raw`, scraping-tier-order); a block stops the run (alert once, no retry).
3 requests max per run (2 when the bundle name is unchanged). robots.txt: none (404). Snapshot to STATE/slopalytics/<fetchedAt>.json only when
fetchedAt changes, then write Reference/lane-intel/slopalytics/<fetchedAt date>.md.
  slopalytics.py [--out DIR] [--dry-run] [--from-dir DIR]   |   slopalytics.py --self-test"""
import argparse, html, json, os, re, subprocess, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from common import STATE, VAULT_OUT, alert_once, clear, halted, lanes, log, load_json, notify, save_json

SITE = "https://slopalytics.com"
MANGLED = "\x00"  # marks a "<" that web read's HTML parse turned into a tag (see unwrap)
FIELDS = ("id family name variant lab labName released open deprecated effort effortLabel effortOrder estimated "
          "intelligence cost costTotal tokens tokensTotal speed time inputPrice outputPrice context").split()
LAB = {"anthropic": "anthropic", "openai": "openai"}  # lanes.json provider -> slopalytics lab
NUM = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def parse_literal(s, i):
    """Minimal JS object-literal tokenizer/parser (minified Vite output). Returns (value, end index)."""
    while s[i] in " \n\t\r":
        i += 1
    c = s[i]
    if c in "{[":
        close, out = ("}", {}) if c == "{" else ("]", [])
        i += 1
        while True:
            while s[i] in " \n\t\r,":
                i += 1
            if s[i] == close:
                return out, i + 1
            if c == "[":
                v, i = parse_literal(s, i); out.append(v); continue
            if s[i] in "`\"'":
                k, i = parse_literal(s, i)
            else:
                m = IDENT.match(s, i) or NUM.match(s, i)
                k, i = m.group(), m.end()
            while s[i] in " \n\t\r":
                i += 1
            if s[i] != ":":
                raise ValueError(f"expected ':' at {i}: {s[i:i + 40]!r}")
            out[k], i = parse_literal(s, i + 1)
    if c in "`\"'":
        buf, i = [], i + 1
        while s[i] != c:
            if c == "`" and s.startswith("${", i):
                raise ValueError(f"template expression at {i}")
            if s[i] == "\\":
                i += 1
                if s[i] == "u":
                    buf.append(chr(int(s[i + 1:i + 5], 16))); i += 5; continue
                buf.append(ESC.get(s[i], s[i]))
            else:
                buf.append(s[i])
            i += 1
        return "".join(buf), i + 1
    if s.startswith("!0", i):
        return True, i + 2
    if s.startswith("!1", i):
        return False, i + 2
    if s.startswith("void 0", i):
        return None, i + 6
    m = NUM.match(s, i)
    if m:
        t = m.group()
        return (float(t) if any(x in t for x in ".eE") else int(t)), m.end()
    m = IDENT.match(s, i)
    if m and m.group() in ("true", "false", "null", "undefined"):
        return {"true": True, "false": False}.get(m.group()), m.end()
    raise ValueError(f"unsupported token at {i}: {s[i:i + 40]!r}")


def parse_bundle(js):
    m = re.search(r"=\s*\{fetchedAt:", js)
    if not m:
        raise ValueError("benchmark literal ({fetchedAt: ...}) not found in bundle")
    data, _ = parse_literal(js, js.index("{", m.start()))
    recs = [{k: r.get(k) for k in FIELDS} for r in data["models"]]
    if not recs or recs[0]["intelligence"] is None and recs[0]["id"] is None:
        raise ValueError("no model records")
    if MANGLED in str(data["fetchedAt"]) or any(isinstance(v, str) and MANGLED in v for r in recs for v in r.values()):
        raise ValueError("benchmark literal mangled by the HTML parse (a '<' inside it)")
    return data["fetchedAt"], recs


def fam(model):
    return model.replace(".", "-")  # lanes.json gpt-6.1-sol == slopalytics family gpt-6-1-sol


def candidates(recs, provider):
    allowed = {fam(m) for m in lanes()["allowed_models"].get(provider, [])}
    return [r for r in recs if r["lab"] == LAB.get(provider) and r["family"] in allowed and not r["deprecated"]
            and r["cost"] is not None and r["intelligence"] is not None]


def pareto(rows, key):
    return [r for r in rows if not any(o is not r and o[key] is not None and r[key] is not None and
                                       o["intelligence"] >= r["intelligence"] and o[key] <= r[key] and
                                       (o["intelligence"] > r["intelligence"] or o[key] < r[key]) for o in rows)
            and r[key] is not None]


def pick(recs, provider, model, effort):
    return next((r for r in candidates(recs, provider) if r["family"] == fam(model) and r["effort"] == effort), None)


def dominators(recs, provider, model, effort):
    """Same-provider variants (never max) at least as smart and as cheap as the lane pick, better on one."""
    p = pick(recs, provider, model, effort)
    if not p:
        return []
    return [r for r in candidates(recs, provider) if r is not p and r["effort"] != "max" and
            r["intelligence"] >= p["intelligence"] and r["cost"] <= p["cost"] and
            (r["intelligence"] > p["intelligence"] or r["cost"] < p["cost"])]


def usage_7d(u):
    end = datetime.fromisoformat(u["endExclusive"]).date()
    days = {str(end - timedelta(days=n)) for n in range(1, 8)}
    harness = Counter(); models = Counter()
    for r in u.get("sent", []):
        if r["day"] in days:
            harness[r["harness"]] += r["turns"]
    for r in u.get("models", []):
        if r["day"] in days:
            models[r["model"]] += r["turns"]
    return {"from": min(days), "to": max(days), "harness": dict(harness), "models": dict(models)}


def share_table(counter, title, top=12):
    total = sum(counter.values()) or 1
    rows = [f"| {k} | {v:,} | {100 * v / total:.1f}% |" for k, v in sorted(counter.items(), key=lambda x: -x[1])[:top]]
    return f"| {title} | turns | share |\n|---|---|---|\n" + "\n".join(rows)


def render(snap):
    recs, L = snap["records"], lanes()
    out = [f"---\ntype: lane-intel\nsource: slopalytics\ndescription: Slopalytics snapshot of {snap['fetchedAt'][:10]}, model intelligence, cost, speed and T3 usage per provider\ndate: {snap['fetchedAt'][:10]}\nfetchedAt: {snap['fetchedAt']}\n---\n",
           f"# Slopalytics snapshot {snap['fetchedAt'][:10]}\n",
           "Data: [Slopalytics](https://slopalytics.com) (Theo / T3); benchmark numbers by Artificial Analysis. "
           "Intelligence = AA index; $ and seconds are per benchmark task. Usage = T3 Code production turns.\n"]
    for prov in L["allowed_models"]:
        rows = sorted(candidates(recs, prov), key=lambda r: (r["family"], r["effortOrder"] or 0))
        if not rows:
            continue
        pc, pt = pareto(rows, "cost"), pareto(rows, "time")
        out.append(f"## {prov}\n\n| model | effort | intelligence | $/task | s/task | Pareto $ | Pareto time |\n|---|---|---|---|---|---|---|")
        for r in rows:
            t = f"{r['time']:.0f}" if r["time"] is not None else "-"
            out.append(f"| {r['family']} | {r['effort']} | {r['intelligence']:.1f} | {r['cost']:.2f} | {t} | "
                       f"{'yes' if r in pc else ''} | {'yes' if r in pt else ''} |")
        out.append("")
    out.append("## Current lane picks\n\n| lane | model | effort | intelligence | $/task | s/task | on Pareto ($ / time) | dominated by |\n|---|---|---|---|---|---|---|---|")
    for name, lane in L["lanes"].items():
        if lane["provider"] not in LAB:
            continue
        p = pick(recs, lane["provider"], lane["model"], lane["effort"])
        if not p:
            out.append(f"| {name} | {lane['model']} | {lane['effort']} | no data | | | | |"); continue
        rows = candidates(recs, lane["provider"])
        dom = ", ".join(f"{d['family']} {d['effort']}" for d in dominators(recs, lane["provider"], lane["model"], lane["effort"]))
        t = f"{p['time']:.0f}" if p["time"] is not None else "-"
        out.append(f"| {name} | {lane['model']} | {lane['effort']} | {p['intelligence']:.1f} | {p['cost']:.2f} | {t} | "
                   f"{'yes' if p in pareto(rows, 'cost') else 'no'} / {'yes' if p in pareto(rows, 'time') else 'no'} | {dom or '-'} |")
    u = snap.get("usage")
    if u:
        out.append(f"\n## T3 Code usage, {u['from']} .. {u['to']}\n\n" + share_table(Counter(u["harness"]), "harness") +
                   "\n\n" + share_table(Counter(u["models"]), "model"))
    return "\n".join(out) + "\n"


def get(url):
    """One fetch through the web skill. A non-zero exit (blocked, budget, 404) raises: the caller stops for this run."""
    r = subprocess.run(["web", "read", "--raw", "--budget", "90", url], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"web read {url}: exit {r.returncode}: {r.stderr.strip()[-200:]}")
    return r.stdout


def unwrap(raw, strict=False):
    """`web read --raw` returns the parsed DOM re-serialized: a JS or JSON body comes back as <html><body>...</body></html>
    with & < > escaped, and a literal '<' followed by a letter was parsed as a tag. Undo the wrapper and the escapes; mark
    each parsed tag with MANGLED so a value it touched fails closed. strict: any MANGLED raises. An HTML page passes as is."""
    m = re.fullmatch(r"(?s)\s*<html><body>(.*)</body></html>\s*", raw)
    if not m:
        return raw
    text = html.unescape(m.group(1).replace("<", MANGLED))
    if strict and MANGLED in text:
        raise ValueError("body mangled by the HTML parse")
    return text


def run(out, dry_run, from_dir=None):
    sdir = os.path.join(STATE, "slopalytics")
    last = load_json(os.path.join(sdir, "last.json"), {})
    # ponytail: --from-dir reads saved index.html/bundle.js/t3-usage.json instead of the network (tests, re-runs)
    fetch = (lambda name, url: open(os.path.join(from_dir, name)).read()) if from_dir else (lambda name, url: get(url))
    # (a fetch that fails raises into the alert path below: one wall per run, never retried around)
    try:
        index = fetch("index.html", SITE + "/")
        bundle = re.search(r"/assets/index-[\w-]+\.js", index).group()
        if bundle == last.get("bundle") and os.path.exists(last.get("snapshot", "")):
            fetched, recs = last["fetchedAt"], load_json(last["snapshot"], {})["records"]
        else:
            fetched, recs = parse_bundle(unwrap(fetch("bundle.js", SITE + bundle)))
        clear("slop_parse")
    except Exception as e:  # network, layout change, tokenizer
        alert_once("slop_parse", f"slopalytics parse failed: {e}", dry_run)
        return 0  # alerted once; exit 0 so run.sh doesn't repeat it daily
    try:
        u = json.loads(unwrap(fetch("t3-usage.json", SITE + "/data/t3-usage.json"), strict=True))
        usage, ufetched = usage_7d(u), u.get("fetchedAt")
    except Exception as e:
        log(f"t3-usage.json failed: {e}"); usage, ufetched = None, None
    age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched.replace("Z", "+00:00"))
    if age > timedelta(hours=48):
        alert_once(f"slop_stale_{fetched}", f"slopalytics data is stale: fetchedAt {fetched} ({age.days} d old)", dry_run)
    if (fetched, ufetched) == (last.get("fetchedAt"), last.get("usageFetchedAt")):
        log(f"slopalytics unchanged (fetchedAt {fetched}); nothing written"); return 0
    snap = {"fetchedAt": fetched, "usageFetchedAt": ufetched, "bundle": bundle, "records": recs, "usage": usage}
    note = render(snap)
    path = os.path.join(out, "slopalytics", f"{fetched[:10]}.md")
    if dry_run:
        print(f"--- dry-run: would write {path} and a snapshot ---\n{note}"); return 0
    spath = os.path.join(sdir, f"{fetched.replace(':', '')}.json")
    save_json(spath, snap)
    save_json(os.path.join(sdir, "last.json"), {"fetchedAt": fetched, "usageFetchedAt": ufetched, "bundle": bundle, "snapshot": spath})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(note)
    log(f"slopalytics snapshot {fetched}: {len(recs)} records -> {path}")
    notify(f"new slopalytics snapshot {fetched[:10]}")
    return 0


def self_test():
    js = ('x=1;var Lt={fetchedAt:`2026-10-08T02:04:01Z`,models:[{id:`claude-opus-5-5-high`,family:`claude-opus-5-5`,'
          'lab:`anthropic`,deprecated:!1,open:!0,effort:`high`,effortOrder:40,intelligence:53.5,cost:1.82,time:282,'
          'context:1e6,inputPrice:.82,name:"a\\"b",variant:`x\\`y`},{id:`claude-opus-5-5-xhigh`,family:`claude-opus-5-5`,'
          'lab:`anthropic`,deprecated:!1,effort:`xhigh`,effortOrder:50,intelligence:56,cost:1.5,time:400}]};var z=2')
    fetched, recs = parse_bundle(js)
    assert fetched == "2026-10-08T02:04:01Z" and len(recs) == 2
    r = recs[0]
    assert r["open"] is True and r["deprecated"] is False and r["context"] == 1e6 and r["inputPrice"] == 0.82
    assert r["name"] == 'a"b' and r["variant"] == "x`y" and r["speed"] is None
    assert [x["effort"] for x in pareto(recs, "cost")] == ["xhigh"] and len(pareto(recs, "time")) == 2
    assert [d["effort"] for d in dominators(recs, "anthropic", "claude-opus-5-5", "high")] == ["xhigh"]
    assert fam("gpt-6.1-sol") == "gpt-6-1-sol"
    u = {"endExclusive": "2026-10-08", "sent": [{"day": "2026-10-07", "harness": "codex", "turns": 3},
                                                {"day": "2026-09-01", "harness": "codex", "turns": 9}], "models": []}
    assert usage_7d(u)["harness"] == {"codex": 3}
    try:
        parse_literal("{a:`${x}`}", 0); raise AssertionError("template not rejected")
    except ValueError:
        pass
    # web read --raw shapes (seen 2026-10-12): JSON wrapped + escaped; a JS '<' outside the literal is harmless, inside fails
    assert json.loads(unwrap('<html><body>{"q":"a &gt; 1 &amp;&amp; b &lt; 2"}</body></html>', strict=True)) == {"q": "a > 1 && b < 2"}
    assert unwrap("<!DOCTYPE html><html lang=en><head></head></html>") .startswith("<!DOCTYPE")
    wrapped = lambda js: "<html><body>" + js.replace("&", "&amp;").replace(">", "&gt;") + "</body></html>"
    assert parse_bundle(unwrap(wrapped("for(i=0;i<n;i++)f(i);" + js)))[0] == fetched
    for bad in (js.replace("name:\"a", "name:\"<b>a"), js.replace("open:!0", "open:<b>!0")):
        try:
            parse_bundle(unwrap(wrapped(bad))); raise AssertionError("mangled literal accepted")
        except ValueError:
            pass
    try:
        unwrap("<html><body>{\"a\":\"<b>\"}</body></html>", strict=True); raise AssertionError("mangled JSON accepted")
    except ValueError:
        pass
    calls = []
    real = subprocess.run
    try:
        subprocess.run = lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 3, "", "blocked")
        try:
            get(SITE + "/"); raise AssertionError("blocked read not raised")
        except RuntimeError:
            pass
    finally:
        subprocess.run = real
    assert calls == [["web", "read", "--raw", "--budget", "90", SITE + "/"]], calls
    print("self-test passed")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=VAULT_OUT); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-dir"); ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if halted():
        return 0
    return run(a.out, a.dry_run, a.from_dir)


if __name__ == "__main__":
    sys.exit(main())
