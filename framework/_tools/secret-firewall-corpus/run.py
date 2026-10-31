#!/usr/bin/env python3
"""Replay corpus.jsonl through a secret-firewall PreToolUse hook; one PASS/FAIL line per case.

usage: run.py [--hook PATH] [--self-test]
  --hook PATH  hook to test (default: the live ~/.claude/hooks/secret-firewall/block-secrets.py)
  --self-test  check the corpus shape and the scorer against an allow-all stub (no hook run)
Exit 0 only when the gate holds: 0 false negatives and <= 2 false positives.
Read-only: corpus commands are never executed, only fed to the hook as JSON;
the hook's event log is pointed at /dev/null for the run.
"""
import base64, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.expanduser("~/.claude/hooks/secret-firewall/block-secrets.py")
MAX_FP = 2

# ponytail: fake secrets are assembled here, never stored whole, so gitleaks /
# detect-secrets / push protection never fire on the corpus or this file.
F = "Fake0"
FAKES = {
    "GH_PAT": "gh" + "p_" + (F * 8)[:36],
    "OPENAI_KEY": "sk-" + "proj-" + "FAKE0fake1" * 4,
    "AWS_KEY": "AK" + "IA" + "FAKEFAKE0000TEST",
    "AWS_SECRET": "FakeSecret0" * 3 + "FakeSecret0"[:7],
    "TELEGRAM": "123456789" + ":" + ("AAFake0" * 5)[:35],
    "JWT": "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiJmYWtlIn0" + ".ZmFrZXNpZ25hdHVyZQ",
    "PEM": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "MIXED32": "Fa1Ke2Va3Lu4E5xY6zW7qR8tP9mN0bVc",
    "PG_PASS": "Fake" + "Pass1234",
}
FAKES["B64_GH_PAT"] = base64.b64encode(FAKES["GH_PAT"].encode()).decode()

def load():
    with open(os.path.join(HERE, "corpus.jsonl")) as f:
        return [json.loads(l) for l in f if l.strip()]

def payload(c):
    if "raw" in c:
        return c["raw"]
    ti = json.loads(re.sub(r"\{\{(\w+)\}\}", lambda m: FAKES[m.group(1)], json.dumps(c["input"])))
    return json.dumps({"session_id": "corpus", "hook_event_name": "PreToolUse", "tool_name": c["tool"],
                       "tool_input": ti, "cwd": "/nonexistent-corpus-cwd"})

def blocked(argv, c):
    try:
        r = subprocess.run(argv, input=payload(c), text=True, capture_output=True, timeout=10,
                           env={**os.environ, "HOOK_EVENT_LOG": os.devnull})
    except subprocess.TimeoutExpired:  # registered timeout is 10 s; Claude Code lets a timed-out hook's call through
        return False
    if r.returncode == 2:
        return True
    try:
        out = json.loads(r.stdout or "{}")
    except ValueError:
        return False
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny" or out.get("decision") == "block"

def score(argv, cases, quiet=False):
    fn = fp = 0
    for c in cases:
        got = "block" if blocked(argv, c) else "allow"
        ok = got == c["expect"]
        fn += not ok and c["expect"] == "block"
        fp += not ok and c["expect"] == "allow"
        if not quiet:
            shown = c.get("raw") or json.dumps(c["input"])
            print(f"{'PASS' if ok else 'FAIL'} {c['id']} expect={c['expect']:5} got={got:5} {c['why']} :: {shown[:90]}")
    return fn, fp

def self_test():
    cases = load()
    text = open(os.path.join(HERE, "corpus.jsonl")).read()
    ids = [c["id"] for c in cases]
    nb = sum(c["expect"] == "block" for c in cases)
    assert len(cases) >= 40 and nb >= 20 and len(cases) - nb >= 20, "need >= 20 block and >= 20 allow"
    assert len(set(ids)) == len(ids), "duplicate ids"
    assert all(c["expect"] in ("block", "allow") and ("raw" in c or "input" in c) for c in cases)
    assert set(re.findall(r"\{\{(\w+)\}\}", text)) <= set(FAKES), "unknown placeholder"
    assert not any(v in text for v in FAKES.values()), "a whole fake secret is stored in the corpus"
    assert [json.loads(payload(c)) for c in cases if "raw" not in c]
    fn, fp = score(["true"], cases, quiet=True)  # allow-all stub: every block case is a miss
    assert (fn, fp) == (nb, 0), (fn, fp)
    print(f"self-test PASS ({len(cases)} cases: {nb} block / {len(cases) - nb} allow)")

def main():
    if "--self-test" in sys.argv:
        return self_test()
    hook = sys.argv[sys.argv.index("--hook") + 1] if "--hook" in sys.argv else LIVE
    cases = load()
    fn, fp = score([sys.executable, os.path.expanduser(hook)], cases)
    nb = sum(c["expect"] == "block" for c in cases)
    na = len(cases) - nb
    tp = nb - fn
    prec = tp / (tp + fp) if tp + fp else 0.0
    print(f"\n{hook}\nblocked {tp}/{nb} must-block, allowed {na - fp}/{na} must-allow | "
          f"FN={fn} FP={fp} precision={prec:.2f} recall={tp / nb:.2f} | gate (FN=0, FP<={MAX_FP}): "
          f"{'PASS' if fn == 0 and fp <= MAX_FP else 'FAIL'}")
    sys.exit(0 if fn == 0 and fp <= MAX_FP else 1)

if __name__ == "__main__":
    main()
