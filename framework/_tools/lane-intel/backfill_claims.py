#!/usr/bin/env python3
"""One-time legacy claim validation. Defaults to a read-only dry run."""
import argparse, json, re, sys, tempfile
from common import STATE, VAULT_OUT
from summarize import load_sources, merge_claims, store_sources, validate_claims
from pathlib import Path


def legacy_claims(note):
    from stance import ROW
    text = Path(note).read_text()
    fm = dict(re.findall(r"^(\w+): (.*)$", text.split("\n---\n", 1)[0], re.M))
    found = []
    for m in ROW.finditer(text):
        role, provider, model, effort, verdict, at, quote = (g.strip() for g in m.groups())
        found.append({"role": role, "provider": provider, "model": "" if model == "-" else model,
                      "effort": "" if effort == "-" else effort, "verdict": verdict, "timestamp": at, "quote": quote,
                      "post_id": at if fm.get("source") == "x" else "video:" + fm.get("video", "")})
    return found


def backfill(out, state, apply=False):
    """Count every legacy row; only explicit --apply writes evidence and claims."""
    out = Path(out)
    result = []
    for note in sorted(list((out / "theo").glob("*.md")) + list((out / "x").glob("*.md"))):
        candidates = legacy_claims(note)
        sources = load_sources(note, str(state))
        accepted = validate_claims(candidates, sources)
        result.append({"note": str(note.relative_to(out)), "kept": len(accepted), "dropped": len(candidates) - len(accepted)})
        if apply:
            store_sources(note, sources)
            merge_claims(Path(str(note) + ".claims.json"), accepted)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=VAULT_OUT); ap.add_argument("--state", default=STATE)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write validated sidecars at cutover")
    mode.add_argument("--dry-run", action="store_true", help="read only (the default)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    results = backfill(a.out, a.state, a.apply)
    for r in results:
        print(json.dumps(r, sort_keys=True))
    print(json.dumps({"mode": "apply" if a.apply else "dry-run", "notes": len(results),
                      "kept": sum(r["kept"] for r in results), "dropped": sum(r["dropped"] for r in results)}, sort_keys=True))
    return 0


def test_legacy_note_backfill():
    with tempfile.TemporaryDirectory() as d:
        out, state = Path(d) / "notes", Path(d) / "state"
        (out / "theo").mkdir(parents=True); (state / "transcripts").mkdir(parents=True)
        note = out / "theo" / "2026-10-12-v1.md"
        note.write_text("---\nsource: youtube\nvideo: v1\ndate: 2999-01-01\n---\n"
                        "| build | openai | gpt-6.1-sol | high | use | 99:59 | Use Sol on high |\n"
                        "| build | openai | gpt-6-astra | xhigh | use | 99:59 | Astra is always better |\n")
        (state / "transcripts" / "v1.txt").write_text("[01:02] Use Sol on high\n[03:00] Other text")
        (state / "theo.json").write_text(json.dumps({"videos": {"v1": {"date": "2026-10-12T14:00:00Z"}}}))
        before = {p: p.read_bytes() for p in Path(d).rglob("*") if p.is_file()}
        result = backfill(out, state)
        assert result == [{"note": "theo/2026-10-12-v1.md", "kept": 1, "dropped": 1}]
        assert {p: p.read_bytes() for p in Path(d).rglob("*") if p.is_file()} == before
        assert backfill(out, state, apply=True) == result
        from stance import claims
        cs = claims(str(out), state=str(state))
        assert len(cs) == 1 and cs[0]["date"] == "2026-10-12" and cs[0]["offset"] == 62
        assert cs[0]["timestamp"] == "[01:02]" and cs[0]["ts"] == 1791813600
        assert note.read_bytes() == before[note]
        assert backfill(out, state, apply=True) == result
        assert len(claims(str(out), state=str(state))) == 1


def test_backfill_x_and_cli():
    import contextlib, io
    with tempfile.TemporaryDirectory() as d:
        out, state = Path(d) / "notes", Path(d) / "state"
        (out / "x").mkdir(parents=True); state.mkdir()
        note = out / "x" / "2026-10-12.md"
        note.write_text("---\nsource: x\ndate: 2999-01-01\n---\n"
                        "| build | openai | gpt-6.1-sol | high | use | 2109593403482948090 | Use Sol on high |\n"
                        "| build | openai | gpt-6.1-sol | high | use | 9999999999999999999 | Use Sol on high |\n")
        # Seen metadata alone cannot prove the quote.
        (state / "x.json").write_text(json.dumps({"seen": {"2109593403482948090": "2026-10-12T10:33:56Z"}}))
        assert backfill(out, state) == [{"note": "x/2026-10-12.md", "kept": 0, "dropped": 2}]
        store_sources(note, [{"id": "2109593403482948090", "time": "2026-10-12T10:33:56Z", "text": "Use Sol on high"}])
        before = {p: p.read_bytes() for p in Path(d).rglob("*") if p.is_file()}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main(["--out", str(out), "--state", str(state)]) == 0
        assert json.loads(buf.getvalue().splitlines()[-1]) == {"mode": "dry-run", "notes": 1, "kept": 1, "dropped": 1}
        assert before == {p: p.read_bytes() for p in Path(d).rglob("*") if p.is_file()}
        assert backfill(out, state, apply=True) == [{"note": "x/2026-10-12.md", "kept": 1, "dropped": 1}]
        from stance import claims
        assert len(claims(str(out), state=str(state))) == 1


def self_test():
    test_legacy_note_backfill()
    test_backfill_x_and_cli()
    print("self-test passed")


if __name__ == "__main__":
    sys.exit(main())
