#!/usr/bin/env python3
"""check_lanes_doc.py: docs/agents-and-lanes.md and docs/img/routes.svg still match framework/_system/lanes.json.
Both carry a dated snapshot of the lane and route tables; a sync that changes lanes.json makes them stale silently.
Checks: the doc's routes table lists exactly the routed classes, each with its lane; the doc's lanes table lists exactly
the lanes, each with its model and effort; the SVG names every class, every routed lane and its model/effort pair.
  python3 tools/check_lanes_doc.py [repo root]     exit 0 = in sync, 1 = drift (each mismatch printed)"""
import json, os, re, sys


def display(model):
    """claude-opus-5-5 -> Opus 5.5, gpt-6.1-sol -> GPT-6.1 Sol, qwen3.8-text:27b -> qwen3.8 27B."""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)", model)
    if m:
        return f"{m[1].title()} {m[2]}.{m[3]}"
    m = re.fullmatch(r"gpt-([\d.]+)-([a-z]+)", model)
    if m:
        return f"GPT-{m[1]} {m[2].title()}"
    name, _, size = model.partition(":")
    return f"{name.replace('-text', '')} {size.upper()}".strip()


def rows(text, heading):
    """Body rows of the first Markdown table after `heading`, as lists of stripped cells."""
    sec = text.split(heading, 1)[1]
    out = []
    for line in sec.splitlines()[1:]:
        if line.startswith("## "):
            break
        if line.startswith("|") and not re.match(r"\|\s*-", line):
            out.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif out and not line.startswith("|"):
            break
    return out[1:]  # drop the header row


def check(root):
    L = json.load(open(os.path.join(root, "framework/_system/lanes.json")))
    lanes = L["lanes"]
    routes = {k: v["lane"] for k, v in L["routes"].items() if not k.startswith("_")}
    doc = open(os.path.join(root, "docs/agents-and-lanes.md")).read()
    svg = open(os.path.join(root, "docs/img/routes.svg")).read()
    errs = []
    m = re.search(r"^## The (\d+) routes$", doc, re.M)
    rt = {r[0].strip("`"): r[1] for r in rows(doc, m[0])} if m else {}
    if not m or int(m[1]) != len(routes):
        errs.append(f"doc: wants a '## The {len(routes)} routes' heading ({len(routes)} classes in lanes.json)")
    for c, lane in routes.items():
        cell = rt.get(c)
        if cell is None:
            errs.append(f"doc routes table: missing class {c}")
        elif not cell.startswith(f"`{lane}`"):
            errs.append(f"doc routes table: {c} shows {cell!r}, lanes.json says {lane}")
    errs += [f"doc routes table: {c} is not a class in lanes.json" for c in rt if c not in routes]
    lt = {r[0].strip("`"): r for r in rows(doc, "## The lanes")}
    for name, lane in lanes.items():
        r = lt.get(name)
        if r is None:
            errs.append(f"doc lanes table: missing lane {name}")
            continue
        want = display(lane["model"])
        if not all(w in r[1] for w in want.split()):
            errs.append(f"doc lanes table: {name} model {r[1]!r}, lanes.json says {lane['model']} ({want})")
        if r[2] != lane["effort"]:
            errs.append(f"doc lanes table: {name} effort {r[2]!r}, lanes.json says {lane['effort']}")
    errs += [f"doc lanes table: {n} is not a lane in lanes.json" for n in lt if n not in lanes]
    used = sorted(set(routes.values()))  # the SVG draws job -> route -> lane -> model, so only routed lanes appear
    for t in list(routes) + used:
        if f">{t}<" not in svg:
            errs.append(f"routes.svg: no box for {t}")
    for lane in (lanes[n] for n in used if n in lanes):
        label = f"{display(lane['model'])} · {lane['effort'] if lane['effort'] != 'n/a' else 'local'}"
        if f">{label}<" not in svg:
            errs.append(f"routes.svg: no model box {label!r}")
    return errs


def self_test():
    assert display("claude-opus-5-5") == "Opus 5.5" and display("claude-fable-5-1") == "Fable 5.1"
    assert display("gpt-6.1-sol") == "GPT-6.1 Sol" and display("gpt-6-astra") == "GPT-6 Astra"
    assert display("qwen3.8-text:27b") == "qwen3.8 27B"
    t = "## The lanes\n\n| Lane | Model | Effort |\n|---|---|---|\n| `a.b` | Opus 5.5 | high |\n\nprose\n## Next\n"
    assert rows(t, "## The lanes") == [["`a.b`", "Opus 5.5", "high"]], rows(t, "## The lanes")
    print("check_lanes_doc self-test passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    errs = check(root)
    for e in errs:
        print(e)
    print("lanes doc: in sync with lanes.json" if not errs else f"lanes doc: {len(errs)} mismatch(es)")
    sys.exit(1 if errs else 0)
