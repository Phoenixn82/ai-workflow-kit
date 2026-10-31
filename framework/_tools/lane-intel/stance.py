#!/usr/bin/env python3
"""stance.py: fold validated theo/ and x/ claim sidecars into Reference/lane-intel/stance.md + STATE/stance.json.
One row per lane role x provider: newest source wins, ordered by (date, source time as UTC epoch: the X post's snowflake
time, the video's `published:` frontmatter or else 00:00 UTC of its date); inside one source a claim naming an effort
beats one that doesn't, then the later offset into the video. Avoid and conditional claims are listed separately (they never set a pick).
  stance.py [--out DIR] [--dry-run]   |   stance.py --self-test"""
import argparse, glob, json, os, re, sys, tempfile
from datetime import datetime, timezone
from common import STATE, VAULT_OUT, halted, log, save_json


def epoch(iso):
    """ISO date or datetime (naive = UTC) -> UTC epoch seconds; 0 when missing or unparseable."""
    try:
        t = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())

ROW = re.compile(r"^\| (plan|build|review|subagent|design|headless|other) \| (.*?) \| (.*?) \| (.*?) \| (use|avoid|conditional) \| (.*?) \| (.*) \|$", re.M)


def claims(out, state=STATE):
    from summarize import load_sources, validate_claims
    from common import load_json
    found = []
    from common import canon, lanes
    allowed = lanes().get("allowed_models", {})
    for path in sorted(glob.glob(os.path.join(out, "theo", "*.md")) + glob.glob(os.path.join(out, "x", "*.md"))):
        candidates = load_json(path + ".claims.json", [])
        for c in validate_claims(candidates, load_sources(path, state)):
            found.append({**c, "model": canon(c["model"], allowed), "note": os.path.basename(path)})
    return found


def fold(cs):
    rank = lambda c: (c["date"], c["ts"], bool(c["effort"]), c["offset"])
    roles, avoid, cond = {}, {}, []
    for c in sorted(cs, key=rank):
        if c["verdict"] == "use" and c["model"]:
            roles[f"{c['role']}/{c['provider']}"] = c  # later in sort order = newer / more explicit: overwrites
        elif c["verdict"] == "avoid" and (c["model"] or c["effort"]):
            avoid[f"{c['provider']}/{c['model'] or '*'}/{c['effort'] or '*'}"] = c
        elif c["verdict"] in ("avoid", "conditional"):  # a model-less, effort-less avoid ("they'll start refusing",
            # 2026-10-22) is a situational remark, not a provider-wide lane avoid: listed as conditional, never as an avoid
            cond.append(c)
    for key, c in roles.items():  # same note + day naming different model/effort picks = ambiguous: propose.py won't apply it
        alts = {f"{x['model']} {x['effort']}" for x in cs if x["verdict"] == "use" and x["model"] and x["effort"] and
                f"{x['role']}/{x['provider']}" == key and (x["note"], x["date"]) == (c["note"], c["date"])}
        c["ambiguous"] = sorted(alts) if len(alts) > 1 else []
    return {"roles": roles, "avoid": avoid, "conditional": cond[-12:]}


def render(st):
    eff = lambda c: (c["effort"] or "-") + (f" (ambiguous: {' / '.join(c['ambiguous'])})" if c.get("ambiguous") else "")
    row = lambda c: f"{c['model'] or '-'} | {eff(c)} | {c['date']} | {c['source']} | {c['quote']} |"
    out = ["---\ntype: lane-intel\nsource: stance\ndescription: Theo's current per-role model and effort picks, folded from validated lane-intel claims\n---\n", "# Theo's current lane stance\n",
           "Folded from validated claim sidecars under theo/ and x/ by stance.py. Newest explicit claim per role wins.\n",
           "## Picks (use)\n\n| role | provider | model | effort | date | source | quote |\n|---|---|---|---|---|---|---|"]
    out += [f"| {k.split('/')[0]} | {k.split('/')[1]} | {row(c)}" for k, c in sorted(st["roles"].items())]
    out += ["\n## Avoid\n\n| provider | model | effort | date | source | quote |\n|---|---|---|---|---|---|"]
    out += [f"| {c['provider']} | {row(c)}" for c in sorted(st["avoid"].values(), key=lambda c: c["date"], reverse=True)]
    out += ["\n## Conditional (newest 12)\n\n| role | model | effort | date | source | quote |\n|---|---|---|---|---|---|"]
    out += [f"| {c['role']} | {row(c)}" for c in reversed(st["conditional"])]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=VAULT_OUT); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if halted():
        return 0
    cs = claims(a.out, state=STATE)
    st = fold(cs)
    md = render(st)
    if a.dry_run:
        print(md); return 0
    save_json(os.path.join(STATE, "stance.json"), st)
    os.makedirs(a.out, exist_ok=True)
    open(os.path.join(a.out, "stance.md"), "w").write(md)
    log(f"stance: {len(cs)} claims -> {len(st['roles'])} picks, {len(st['avoid'])} avoids")
    return 0


def test_unvalidated_notes_and_sidecars():
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        note = Path(d) / "x" / "2026-10-12.md"; note.parent.mkdir()
        note.write_text("---\nsource: x\ndate: 2999-01-01\n---\n"
                        "| build | openai | gpt-6-astra | xhigh | use | 9999999999999999999 | invented |\n")
        assert claims(d) == []
        sidecar = Path(str(note) + ".claims.json")
        sidecar.write_text(json.dumps([{"post_id": "9999999999999999999", "role": "build", "provider": "openai",
                                       "model": "gpt-6-astra", "effort": "xhigh", "verdict": "use", "quote": "invented"}]))
        assert claims(d) == []


def self_test():
    test_unvalidated_notes_and_sidecars()
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(f"{d}/theo"); os.makedirs(f"{d}/x")
        open(f"{d}/theo/2026-09-25-v1.md", "w").write(
            "---\ntype: lane-intel\nsource: youtube\nvideo: v1\ndate: 2026-09-25\n---\n\n"
            "| build | anthropic | claude-opus-5-5 | xhigh | use | 14:38 | leave it at xhigh |\n"
            "| build | anthropic | claude-fable-5-1 | - | use | 24:29 | prefer coding with Fable |\n"
            "| build | anthropic | claude-opus-5-5 | high | use | 14:34 | high or xhigh both fine |\n"
            "| build | anthropic | claude-opus-5-5 | max | avoid | 28:35 | do not touch max |\n"
            "| subagent | anthropic | claude-haiku-5-5 | - | conditional | 05:00 | only for wasted fan-outs |\n"
            "| other | openai | - | - | avoid | 06:00 | they will start refusing |\n")
        open(f"{d}/x/2026-10-10.md", "w").write(
            "---\ntype: lane-intel\nsource: x\ndate: 2026-10-10\n---\n\n"
            "| review | openai | gpt-6.1-sol | high | use | 2108486588271607991 | Sol reviews everything |\n")
        from backfill_claims import backfill
        from pathlib import Path
        state = Path(d) / "state"; (state / "transcripts").mkdir(parents=True)
        (state / "transcripts" / "v1.txt").write_text("[14:38] leave it at xhigh\n[24:29] prefer coding with Fable\n[14:34] high or xhigh both fine\n[28:35] do not touch max\n[05:00] only for wasted fan-outs\n[06:00] they will start refusing")
        save_json(str(state / "theo.json"), {"videos": {"v1": {"date": "2026-09-25"}}})
        from summarize import store_sources
        xp = f"{d}/x/2026-10-10.md"
        posts = [{"id": "2108486588271607991", "time": "2026-10-09T09:15:49Z", "text": "Sol reviews everything"}]
        store_sources(xp, posts)
        backfill(d, state, apply=True)
        st = fold(claims(d, state=str(state)))
        b = st["roles"]["build/anthropic"]
        assert (b["model"], b["effort"]) == ("claude-opus-5-5", "xhigh"), b  # effort-naming claim beats a later vague one
        assert b["ambiguous"] == ["claude-opus-5-5 high", "claude-opus-5-5 xhigh"], b
        r = st["roles"]["review/openai"]
        assert r["date"] == "2026-10-09" and r["source"] == "x.com/theo/status/2108486588271607991" and not r["ambiguous"], r
        assert "anthropic/claude-opus-5-5/max" in st["avoid"] and len(st["conditional"]) == 2
        # 2026-10-22 refusal remark: no model, no effort -> not a lane avoid (stance.md showed a blanket "avoid openai")
        assert "openai/*/*" not in st["avoid"] and st["conditional"][-1]["quote"] == "they will start refusing", st
        from propose import plan
        from common import lanes
        _, pr, hd = plan(lanes(), st, None)
        assert not any("[06:00]" in x for x in pr + hd), (pr, hd)  # never reaches propose.py's lane matching at all
        assert "| build | anthropic | claude-opus-5-5 | xhigh (ambiguous: claude-opus-5-5 high / claude-opus-5-5 xhigh) |" in render(st)
        # same UTC day: X post 2026-10-12T10:33:56Z vs a video published 14:00Z whose claim sits 59 min in (3540 s, which
        # the old tie-break compared with the post's 10:33 = 37980 s of day). The later source wins, not the bigger number.
        x2 = "---\ntype: lane-intel\nsource: x\ndate: 2026-10-12\n---\n\n| headless | anthropic | claude-opus-5-5 | high | use | 2109593403482948090 | post |\n"
        v2 = ("---\ntype: lane-intel\nsource: youtube\nvideo: v2\ndate: 2026-10-12\n{pub}---\n\n"
              "| headless | anthropic | claude-haiku-5-5 | medium | use | 59:00 | video |\n")
        open(f"{d}/x/2026-10-12.md", "w").write(x2)
        open(f"{d}/theo/2026-10-12-v2.md", "w").write(v2.format(pub="published: 2026-10-12T14:00:00+00:00\n"))
        xposts = [{"id": "2109593403482948090", "time": "2026-10-12T10:33:56Z", "text": "post"}]
        store_sources(f"{d}/x/2026-10-12.md", xposts)
        (state / "transcripts" / "v2.txt").write_text("[59:00] video")
        save_json(str(state / "theo.json"), {"videos": {"v1": {"date": "2026-09-25"}, "v2": {"date": "2026-10-12T14:00:00+00:00"}}})
        backfill(d, state, apply=True)
        h = fold(claims(d, state=str(state)))["roles"]["headless/anthropic"]
        assert h["quote"] == "video" and h["ts"] == 1791813600 and h["offset"] == 3540, h
        open(f"{d}/theo/2026-10-12-v2.md", "w").write(v2.format(pub="published: 2026-10-12T05:00:00-05:00\n"))  # 10:00Z
        store_sources(f"{d}/theo/2026-10-12-v2.md", [{"id": "video:v2", "kind": "youtube", "time": "2026-10-12T05:00:00-05:00", "text": "[59:00] video"}])
        assert fold(claims(d, state=str(state)))["roles"]["headless/anthropic"]["quote"] == "post"
        open(f"{d}/theo/2026-10-12-v2.md", "w").write(v2.format(pub=""))  # date only -> 00:00 UTC, the post is later
        store_sources(f"{d}/theo/2026-10-12-v2.md", [{"id": "video:v2", "kind": "youtube", "time": "2026-10-12", "text": "[59:00] video"}])
        h = fold(claims(d, state=str(state)))["roles"]["headless/anthropic"]
        assert h["quote"] == "post" and h["ts"] == epoch("2026-10-12T10:33:56Z") and epoch("2026-10-12") == 1791763200, h
        assert epoch("") == 0 and epoch("NA--") == 0
    print("self-test passed")


if __name__ == "__main__":
    sys.exit(main())
