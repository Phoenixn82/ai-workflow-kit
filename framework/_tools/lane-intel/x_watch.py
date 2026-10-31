#!/usr/bin/env python3
"""x_watch.py: one logged-out `web read https://x.com/theo` per run (pinned post + ~4 newest top-level posts).
New status ids vs STATE/x.json; post time decoded from the snowflake id. New lane candidates (classify(): names a model,
an effort level, a harness/agent tool or a benchmark) go through ONE summarize call into Reference/lane-intel/x/<date>.md;
the rest are only logged.
  x_watch.py [--out DIR] [--dry-run] [--from-file MD]   |   x_watch.py --self-test"""
import argparse, os, re, subprocess, sys
from datetime import datetime, timezone
from common import STATE, VAULT_OUT, alert_once, clear, halted, load_json, log, notify, save_json

RULES = (("effort", re.compile(r"\b(x-?high|max mode|ultrathink|(low|medium|high|max|reasoning|thinking) (effort|reasoning|thinking|budget))\b", re.I)),
         ("bench", re.compile(r"\b(bench(mark)?s?|\w*bench|evals?|leaderboards?|slopalytics|arc-agi)\b", re.I)),
         ("model", re.compile(r"\b(opus|sonnet|haiku|fable|claude|gpt[\w.-]*|sol|astra|luna|gemini|grok|kimi|qwen|deepseek|glm|llama|mistral)\b", re.I)),
         ("tool", re.compile(r"\b(claude code|codex|cursor|windsurf|cline|aider|opencode|copilot|harness(es)?|sub-?agents?|mcp)\b", re.I)))
# ponytail: rules lean to recall ("workbench", "low effort" pass); a stray post rides along in the one batch call for free
# product / marketing post: their own release notes, nightlies, sponsors (they name tools without advising on them)
PROMO = re.compile(r"\b(nightly|release notes?|changelog|update everything|sponsor(ed)?|giveaway|promo code|link (in|below)|merch|"
                   r"we'?re hiring)\b|\bt3\.?\s?(code|chat)\b.*\b(now (has|supports)|new (feature|version|release)|try it|download)\b", re.I)
STATUS = re.compile(r"\]\(/theo/status/(\d+)\)")
JUNK = re.compile(r"^(!\[|\[!\[@|\[Theo - t3\.gg\]|\[\d[\d.,KM]*\]\(/compose|[\d.,KM:]+$|Pinned$|\[x\.com/)")


def classify(text):
    """-> (lane candidate?, why). A candidate names a model, an effort level, a harness/agent tool or a benchmark; a product /
    marketing post names tools anyway, so it counts only with an effort level or a benchmark in it."""
    hits = [n for n, rx in RULES if rx.search(text)]
    promo = bool(PROMO.search(text))
    keep = [h for h in hits if h in ("effort", "bench")] if promo else hits
    return bool(keep), "+".join(keep) or ("promo" if promo else "nothing named")


def snowflake(i):
    return datetime.fromtimestamp(((int(i) >> 22) + 1288834974657) / 1000, timezone.utc).isoformat(timespec="seconds")


def parse(md):
    """-> (pinned id or None, [{"id","time","text"}]) for the top-level list items of the profile page."""
    from summarize import unfence_text
    md = unfence_text(md)
    if md is None:
        return None, []
    items = re.split(r"\n\* ", "\n" + md.split("\n* ", 1)[-1]) if "\n* " in md else []
    pinned, posts = None, []
    for it in items:
        m = STATUS.search(it)
        if not m:
            continue
        own = m.group(1)
        lines = []
        for ln in it.splitlines():
            ln = ln.strip()
            if not ln or JUNK.match(ln) or f"/theo/status/{own})" in ln:
                continue
            q = STATUS.search(ln)
            lines.append(f"(quoting post {q.group(1)}:)" if q and ln.startswith("[") else ln)
        if it.lstrip().startswith("Pinned"):
            pinned = own; continue
        posts.append({"id": own, "time": snowflake(own), "text": " ".join(lines)})
    return pinned, posts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=VAULT_OUT); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-file"); ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if halted():
        return 0
    if a.from_file:
        md = open(a.from_file).read()
    else:
        r = subprocess.run(["web", "read", "--budget", "90", "https://x.com/theo"], capture_output=True, text=True, timeout=120)
        md = r.stdout
    pinned, posts = parse(md)
    if not posts:
        alert_once("x_empty", "x.com/theo read returned 0 status links (layout change or block?)", a.dry_run)
        return 0  # ponytail: alerted once already; a non-zero exit would re-alert through run.sh every day
    clear("x_empty")
    path = os.path.join(STATE, "x.json")
    st = load_json(path, {"seen": {}})
    new = [p for p in posts if p["id"] not in st["seen"]]
    why = {p["id"]: classify(p["text"]) for p in new}
    rel = [p for p in new if why[p["id"]][0]]
    for p in new:
        ok, reason = why[p["id"]]
        log(f"x new {p['id']} {p['time']} [{'lane' if ok else 'skip'}: {reason}]")
    log(f"x: {len(posts)} posts, {len(new)} new, {len(rel)} about models/harness (pinned {pinned} dropped)")
    if a.dry_run:
        print("dry-run: would summarize", [p["id"] for p in rel] or "nothing"); return 0
    if rel:
        from summarize import x_posts
        res, note = x_posts(rel, a.out)
        notify(f"{len(rel)} new @theo post(s) on models/harness, {len(res['lane_claims'])} claim(s) -> {os.path.basename(note)}")
    st["seen"].update({p["id"]: p["time"] for p in new})
    st["seen"] = dict(sorted(st["seen"].items())[-300:])
    save_json(path, st)
    return 0


def test_fenced_posts_equal_original(md):
    sys.path.insert(0, os.environ.get("WEB_PKG_DIR", __import__('os').path.expanduser("~/AI_Projects/_tools/web")))
    from web.fence import fence
    assert parse(fence(md, "https://x.com/theo").text) == parse(md)


def self_test():
    md = ("Theo\n\n* Pinned\n\n  [![@theo](x)](/theo)\n\n  [Theo - t3.gg](/theo)[@theo](/theo)\n\n  [17h](/theo/status/2109395781845389522)\n\n"
          "  pinned text\n\n  [41](/compose/post?in_reply_to=2109395781845389522)\n\n  9\n"
          "* [![@theo](x)](/theo)\n\n  [Theo - t3.gg](/theo)[@theo](/theo)\n\n  [4h](/theo/status/2109593403482948090)\n\n"
          "  Thank you Opus 5.5\n  slop port\n\n  ![](img)\n\n  [Theo - t3.gg](/theo)[@theo](/theo)\n\n"
          "  [5h](/theo/status/2109576403171872912)\n\n  quoted text\n\n  [86](/compose/post?in_reply_to=2109593403482948090)\n\n  46K\n")
    pinned, posts = parse(md)
    assert pinned == "2109395781845389522", pinned
    assert [p["id"] for p in posts] == ["2109593403482948090"], posts
    assert posts[0]["text"] == "Thank you Opus 5.5 slop port (quoting post 2109576403171872912:) quoted text", posts[0]["text"]
    assert posts[0]["time"].startswith("2026-10-12") and classify(posts[0]["text"])[0]
    samples = [  # (post, lane candidate?)  1-3 are real 2026-10-12 posts, the rest written for this test
        ("Thank you Opus 5.5 🫡 Kinda wild that I vibe coded a slop port of the LANGUAGE ITSELF and got a 3x reduction in typechecking", True),
        ('Latest T3 Code nightly now has an "update everything" button. 1 click to: - Update your local app - Update T3 Code on all '
         "environments - Update all the providers (claude code, codex etc) across your environments", False),
        ("people DMing me their vibe coded VS Code extensions", False),
        ("Sol on xhigh reviews everything Opus writes. Cross-lab review is the move.", True),
        ("T3 Chat now supports Gemini 3.5 Pro. Try it today", False),
        ("T3 Code nightly: Codex now defaults to xhigh, high was leaving too much on the table", True),
        ("Claude Code subagents are criminally underrated for big refactors", True),
        ("New SWE-bench numbers are wild, nobody is close", True),
        ("This video is sponsored by Convex, link below", False),
        ("Gym then a flight to SF, see you at the meetup", False),
        ("Max effort is a waste of tokens, stop using it", True),
        ("Release notes for T3 Code v2.3: faster startup, Cursor-style tab completion, bug fixes", False)]
    bad = [(t[:50], want, classify(t)) for t, want in samples if classify(t)[0] != want]
    assert not bad, bad
    assert parse("no posts here") == (None, [])
    test_fenced_posts_equal_original(md)
    print("self-test passed")


if __name__ == "__main__":
    sys.exit(main())
