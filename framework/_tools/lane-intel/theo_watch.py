#!/usr/bin/env python3
"""theo_watch.py: poll Theo's YouTube RSS, skip Shorts, transcribe each new full video (auto-subs, else audio ->
faster-whisper on thor), then one summarize call per video -> Reference/lane-intel/theo/<date>-<id>.md.
Stop at the first wall (feedback_check-robots-before-web-read): a 429, bot check or unexplained caption failure marks the
video "deferred" and ends the run, with no audio fallback. Whisper runs only when yt-dlp says the video has no captions.
  theo_watch.py [--backfill ID,ID] [--out DIR] [--dry-run]   |   theo_watch.py --self-test
--dry-run does everything (incl. the Claude call) except writing the note and the seen-state."""
import argparse, glob, json, os, re, subprocess, sys, tempfile, time, urllib.request
import xml.etree.ElementTree as ET
from common import STATE, TOOL, VAULT_OUT, alert_once, clear, halted, load_json, log, notify, save_json

CHANNEL = "UCbRP3c757lWg9M-U7TyEkXA"
RSS = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
MAX_PER_RUN = 6  # ponytail: bounds cost and GPU time per run; the rest wait for the next run
MAX_TRIES = 3  # then "gave-up": skipped by the daily run, one notify line, hand retry with --backfill=<id>
THOR_DIR = "~/.local/lane-intel"
WHISPER = (f"cd {THOR_DIR} && export LD_LIBRARY_PATH=$(echo venv/lib/python3*/site-packages/nvidia/*/lib | tr ' ' :) && "
           "timeout -s KILL 1200 nice -n 10 venv/bin/python - tmp/{f}")


# yt-dlp wording for a host-level block; an age gate ("Sign in to confirm your age") is one video's problem, not a wall
WALL = re.compile(r"HTTP Error (429|403)|Too Many Requests|not a bot|captcha|rate.?limit", re.I)
NO_SUBS = "There are no subtitles for the requested languages"  # yt-dlp's notice when the video has none (YoutubeDL.py)


class Deferred(Exception):
    """YouTube walled this host (429, bot check, unexplained caption failure): stop for this run, never retry around it."""


def bump(rec):
    rec["tries"] = rec.get("tries", 0) + 1
    rec["status"] = "gave-up" if rec["tries"] >= MAX_TRIES else "failed"


def sh(cmd, timeout, **kw):
    """Run with a hard timeout; subprocess.run kills the child (SIGKILL) when it expires."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def rss():
    root = ET.fromstring(urllib.request.urlopen(RSS, timeout=30).read())  # the official feed endpoint, not page scraping
    return [(e.find("yt:videoId", NS).text, e.find("a:title", NS).text, e.find("a:published", NS).text[:10])
            for e in root.findall("a:entry", NS)]


def meta(vid):
    r = sh(["nice", "yt-dlp", "--skip-download", "--no-warnings", "--print",
            "%(id)s|%(duration)s|%(width)s|%(height)s|%(upload_date)s|%(title)s", f"https://www.youtube.com/watch?v={vid}"], 90)
    if r.returncode and WALL.search(r.stderr):
        raise Deferred(f"metadata: {r.stderr.strip()[-160:]}")
    _, dur, w, h, up, title = r.stdout.strip().split("|", 5)
    num = lambda x: float(x) if x not in ("NA", "None", "") else 0
    # duration NA/0 = live now (or not processed yet): not a Short; main() leaves it for the next run
    return {"short": num(h) > num(w) or 0 < num(dur) < 180, "duration": num(dur), "title": title,
            "date": f"{up[:4]}-{up[4:6]}-{up[6:]}"}


def stamp(ms):
    s = int(ms) // 1000
    return f"[{s // 60:02d}:{s % 60:02d}]"


def json3_text(d):
    out = []
    for ev in d.get("events", []):
        t = "".join(s.get("utf8", "") for s in ev.get("segs", [])).replace("\n", " ").strip()
        if t:
            out.append(f"{stamp(ev.get('tStartMs', 0))} {t}")
    return "\n".join(out)


def transcript(vid):
    cache = os.path.join(STATE, "transcripts", f"{vid}.txt")
    if os.path.exists(cache) and os.path.getsize(cache) > 500:
        return open(cache).read(), "cache"
    url = f"https://www.youtube.com/watch?v={vid}"
    with tempfile.TemporaryDirectory() as tmp:
        text, how = "", "subs"
        try:
            r = sh(["nice", "yt-dlp", "--skip-download", "--write-auto-subs", "--sub-langs", "en",
                    "--sub-format", "json3", "-o", f"{tmp}/%(id)s", url], 60)
            subs = glob.glob(f"{tmp}/*.json3")
            for f in subs:
                text = json3_text(json.load(open(f)))
        except subprocess.TimeoutExpired:
            raise Deferred("auto-subs timed out")
        if len(text) < 500:
            if subs:  # captions came back, just too short: a problem with this video, not a wall (counts a try)
                raise RuntimeError(f"auto-subs too short ({len(text)} chars)")
            said = r.stdout + r.stderr
            if NO_SUBS not in said or WALL.search(said):  # a block, or a failure yt-dlp did not explain: never go around it
                raise Deferred(f"captions not fetched: {(r.stderr.strip() or f'exit {r.returncode}, no file')[-160:]}")
            how = "whisper"
            log(f"{vid}: the video has no captions; audio -> whisper on thor")
            a = sh(["nice", "yt-dlp", "-q", "--no-progress", "--no-warnings", "-f", "bestaudio[ext=m4a]/bestaudio",
                    "-o", f"{tmp}/{vid}.%(ext)s", url], 600)
            if a.returncode:
                if WALL.search(a.stderr):
                    raise Deferred(f"audio: {a.stderr.strip()[-160:]}")
                raise RuntimeError(f"audio download failed ({a.returncode}): {a.stderr.strip()[-200:]}")
            audio = glob.glob(f"{tmp}/{vid}.*")[0]
            f = os.path.basename(audio)
            sh([__import__('os').path.expanduser("~/.local/bin/thor-up")], 150, check=True)
            sh(["ssh", "-o", "BatchMode=yes", "thor", f"mkdir -p {THOR_DIR}/tmp && find {THOR_DIR}/tmp -type f -mmin +120 -delete"], 30, check=True)
            sh(["scp", "-q", "-o", "BatchMode=yes", audio, f"thor:.local/lane-intel/tmp/{f}"], 300, check=True)
            t = time.time()
            try:
                r = sh(["ssh", "-o", "BatchMode=yes", "thor", WHISPER.format(f=f)], 1260,
                       stdin=open(os.path.join(TOOL, "whisper_thor.py")))
            finally:
                subprocess.run(["ssh", "-o", "BatchMode=yes", "thor", f"rm -f {THOR_DIR}/tmp/{f}"], timeout=30, check=False)
            if r.returncode != 0:
                raise RuntimeError(f"whisper failed ({r.returncode}): {r.stderr.strip()[-300:]}")
            text = r.stdout
            log(f"{vid}: whisper {time.time() - t:.0f}s, {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}")
    if len(text) < 500:
        raise RuntimeError("transcript empty")
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    open(cache, "w").write(text)
    return text, how


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", default=""); ap.add_argument("--out", default=VAULT_OUT)
    ap.add_argument("--seed", action="store_true", help="mark every current RSS item not yet in state as seen, process nothing")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--since", default="2026-09-28", help="skip videos uploaded before this date; the user 2026-10-12: only the last two weeks matter")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if halted():
        return 0
    from summarize import video
    path = os.path.join(STATE, "theo.json")
    st = load_json(path, {"videos": {}})
    if a.seed:
        new = [e for e in rss() if e[0] not in st["videos"]]
        for vid, title, pub in new:
            st["videos"][vid] = {"status": "seeded", "title": title, "date": pub}
        print(f"seeded {len(new)}: {[e[0] for e in new]}")
        if not a.dry_run:
            save_json(path, st)
        return 0
    forced = [v for v in a.backfill.split(",") if v]
    if forced:
        todo = [(v, None, None) for v in forced]
    else:
        todo = [e for e in rss() if st["videos"].get(e[0], {}).get("status") not in ("done", "short", "gave-up", "seeded", "too-old")]
    done, failed = [], []
    for vid, title, pub in (todo if forced else todo[:MAX_PER_RUN]):  # an explicit backfill is not capped
        if halted():
            break
        rec = st["videos"].setdefault(vid, {})
        counted = False
        try:
            m = meta(vid)
            rec.update(title=m["title"], date=m["date"])
            if m["date"] and m["date"] < a.since:
                rec["status"] = "too-old"; log(f"{vid}: uploaded {m['date']}, before --since {a.since}, skipped"); continue
            if not m["duration"]:
                log(f"{vid}: no duration yet (live?), left for the next run"); continue
            if m["short"] and not forced:
                rec["status"] = "short"; log(f"{vid}: Short ({m['duration']:.0f}s), skipped"); continue
            bump(rec); counted = True  # counted and saved (with every earlier video) before the slow part: a run killed
            if not a.dry_run:          # mid-video (run.sh KILLs a step at 3600 s) still uses a try instead of retrying forever
                save_json(path, st)
            text, how = transcript(vid)
            log(f"{vid}: transcript via {how}, {len(text)} chars")
            if how == "subs":
                clear("yt_wall")
            res, note = video(vid, text, m["title"], m["date"], a.out, a.dry_run)
            rec.update(status="done", note=note, claims=len(res["lane_claims"]))
            done.append(f"{m['title'][:50]} ({len(res['lane_claims'])} claims)")
        except Deferred as e:  # the host is walled: no try is charged, the rest of the list waits for the next daily run
            if counted:
                rec["tries"] -= 1
            rec.update(status="deferred", error=str(e)[-200:])
            log(f"{vid}: DEFERRED (YouTube wall), stopping this run: {e}")
            alert_once("yt_wall", f"YouTube walled theo_watch on {vid} ({str(e)[-100:]}); deferred to the next daily run, "
                       "no retry around it", a.dry_run)
            break
        except Exception as e:
            if not counted:
                bump(rec)
            rec["error"] = str(e)[-200:]
            log(f"{vid}: FAILED ({rec['status']}, try {rec['tries']}): {e}")
            if rec["status"] == "failed":
                failed.append(vid)
    if a.dry_run:
        print("dry-run: seen-state not written"); return 1 if failed else 0
    gone = [v for v, r in st["videos"].items() if r.get("status") == "gave-up" and not r.get("told")]
    if gone:  # once per video, also for one whose last try was killed (noticed by the next run)
        notify(f"theo_watch gave up on {', '.join(gone)} after {MAX_TRIES} tries "
               f"(last error: {st['videos'][gone[0]].get('error', 'none recorded, run killed mid-video')[-120:]}); "
               f"retry by hand: theo_watch.py --backfill={','.join(gone)}")
        for v in gone:
            st["videos"][v]["told"] = True
    save_json(path, st)
    if done:
        notify(f"new Theo video(s): {'; '.join(done)}")
    if failed:
        notify(f"theo_watch failed on {', '.join(failed)} (will retry, max {MAX_TRIES} tries)")
    log(f"theo_watch: {len(todo)} to do, {len(done)} done, {len(failed)} failed")
    return 1 if failed else 0


def self_test():
    d = {"events": [{"tStartMs": 0, "segs": [{"utf8": "hello"}, {"utf8": " world"}]}, {"tStartMs": 100, "segs": [{"utf8": "\n"}]},
                    {"tStartMs": 754000, "segs": [{"utf8": "use xhigh"}]}]}
    assert json3_text(d) == "[00:00] hello world\n[12:34] use xhigh", json3_text(d)
    feed = ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry>'
            '<yt:videoId>abc</yt:videoId><title>T</title><published>2026-10-01T12:00:00+00:00</published></entry></feed>')
    root = ET.fromstring(feed)
    e = root.find("a:entry", NS)
    assert (e.find("yt:videoId", NS).text, e.find("a:published", NS).text[:10]) == ("abc", "2026-10-01")
    assert "{f}" in WHISPER and "timeout -s KILL 1200" in WHISPER
    retry_cap_test()
    wall_test()
    print("self-test passed")


def wall_test():
    """A 429 or an unexplained caption failure is a wall: Deferred, no audio download, no try charged, run stops, one alert.
    Only yt-dlp's "no subtitles" notice reaches the whisper leg. Temp state; no network, journal or Telegram."""
    import common, summarize
    g, env = globals(), "LANE_INTEL_NOTIFY_FILE"
    saved = ({k: g[k] for k in ("STATE", "halted", "log", "rss", "meta", "sh")}, common.log, common.STATE, summarize.video, os.environ.get(env))
    calls = []

    def fake(out):
        def run(cmd, timeout, **kw):
            calls.append(cmd)
            if "bestaudio[ext=m4a]/bestaudio" in cmd:
                raise LookupError("audio leg reached")
            return subprocess.CompletedProcess(cmd, 1 if "429" in out else 0, out if "[info]" in out else "", "" if "[info]" in out else out)
        return run
    with tempfile.TemporaryDirectory() as d:
        try:
            g.update(STATE=d, halted=lambda: False, log=lambda m: None, rss=lambda: [("v1", "T", "2026-10-10"), ("v2", "T2", "2026-10-11")],
                     meta=lambda vid: {"short": False, "duration": 900.0, "title": "T", "date": "2026-10-10"})
            common.log, common.STATE = (lambda m: None), d
            summarize.video = lambda *a, **k: ({"lane_claims": []}, "note")
            os.environ[env] = f"{d}/notify"
            for out in ("ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests", "", "[youtube] v1: x"):
                calls.clear(); g["sh"] = fake(out)
                try:
                    transcript("v1"); raise AssertionError(f"no Deferred for {out!r}")
                except Deferred:
                    pass
                assert len(calls) == 1, calls  # no audio download after a wall
            calls.clear(); g["sh"] = fake(f"[info] {NO_SUBS}")
            try:
                transcript("v1"); raise AssertionError("no-captions video did not reach whisper")
            except LookupError:
                pass
            # meta(): yt-dlp's captcha and rate-limit wording are walls; an age gate is one video's problem, not a wall
            real_meta = saved[0]["meta"]
            for err, wall in (("ERROR: [youtube] v1: Video unavailable. YouTube is requiring a captcha challenge before playback", True),
                              ("ERROR: [youtube] v1: This content isn't available, try again later. The current session has been rate-limited by YouTube", True),
                              ("ERROR: [youtube] v1: Sign in to confirm your age. This video may be inappropriate for some users.", False)):
                g["sh"] = lambda cmd, timeout, **kw: subprocess.CompletedProcess(cmd, 1, "", err)
                try:
                    real_meta("v1"); raise AssertionError(f"meta passed on {err!r}")
                except Deferred:
                    assert wall, err
                except ValueError:
                    assert not wall, err
            # captions that came back but are too short: not a wall (counts a try), and no audio download
            def short_subs(cmd, timeout, **kw):
                calls.append(cmd)
                if "bestaudio[ext=m4a]/bestaudio" in cmd:
                    raise LookupError("audio leg reached")
                json.dump({"events": [{"tStartMs": 0, "segs": [{"utf8": "hi"}]}]}, open(cmd[cmd.index("-o") + 1].replace("%(id)s", "v1") + ".en.json3", "w"))
                return subprocess.CompletedProcess(cmd, 0, "", "")
            calls.clear(); g["sh"] = short_subs
            try:
                transcript("v1"); raise AssertionError("short captions passed")
            except RuntimeError:
                assert len(calls) == 1, calls
            g["sh"] = fake("ERROR: HTTP Error 429: Too Many Requests")
            calls.clear()
            assert main([]) == 0
            v = load_json(f"{d}/theo.json", {})["videos"]
            assert v["v1"]["status"] == "deferred" and v["v1"]["tries"] == 0 and "v2" not in v, v  # stopped at the first wall
            assert len(calls) == 1 and sum("walled" in ln for ln in open(f"{d}/notify")) == 1
            calls.clear(); assert main([]) == 0 and sum("walled" in ln for ln in open(f"{d}/notify")) == 1  # alerted once
        finally:
            g.update(saved[0]); common.log, common.STATE, summarize.video = saved[1], saved[2], saved[3]
            os.environ.pop(env, None) if saved[4] is None else os.environ.__setitem__(env, saved[4])


def retry_cap_test():
    """Daily runs on a video whose whisper keeps failing: failed, failed, gave-up + ONE notify line, then skipped. A try killed
    mid-video still counts, and the next run sends its gave-up line. Temp state only; no network, journal or Telegram."""
    import common, summarize
    g, env, calls = globals(), "LANE_INTEL_NOTIFY_FILE", []
    saved = ({k: g[k] for k in ("STATE", "halted", "log", "rss", "meta", "transcript")}, common.log, summarize.video, os.environ.get(env))

    def boom(vid):
        calls.append(vid)
        raise KeyboardInterrupt if vid == "v2" else RuntimeError("whisper failed (1): CUDA out of memory")
    with tempfile.TemporaryDirectory() as d:
        try:
            feed = [("v1", "T", "2026-10-10")]
            g.update(STATE=d, halted=lambda: False, log=lambda m: None, rss=lambda: feed, transcript=boom,
                     meta=lambda vid: {"short": False, "duration": 900.0, "title": "T", "date": "2026-10-10"})
            common.log = lambda m: None; summarize.video = lambda *a, **k: ({"lane_claims": []}, "note")
            os.environ[env] = f"{d}/notify"
            vids = lambda: load_json(f"{d}/theo.json", {})["videos"]
            lines = lambda: open(f"{d}/notify").read().splitlines()
            assert [main([]) for _ in range(3)] == [1, 1, 0]
            assert (vids()["v1"]["status"], vids()["v1"]["tries"], len(calls)) == ("gave-up", 3, 3), vids()
            gave = [ln for ln in lines() if "gave up" in ln]
            assert len(gave) == 1 and "v1 after 3 tries" in gave[0] and "CUDA out of memory" in gave[0] and "--backfill=v1" in gave[0], lines()
            n = len(lines())
            assert main([]) == 0 and len(calls) == 3 and len(lines()) == n, "gave-up video retried or re-notified"
            feed.append(("v2", "T2", "2026-10-11"))
            save_json(f"{d}/theo.json", {"videos": {**vids(), "v2": {"status": "failed", "tries": 2}}})
            try:
                main([]); raise AssertionError("kill not simulated")
            except KeyboardInterrupt:
                pass
            assert vids()["v2"]["status"] == "gave-up" and not vids()["v2"].get("told"), vids()["v2"]
            assert main([]) == 0 and len(calls) == 4
            gave = [ln for ln in lines() if "gave up" in ln]
            assert len(gave) == 2 and "gave up on v2 " in gave[1] and "run killed mid-video" in gave[1], gave
        finally:
            g.update(saved[0]); common.log, summarize.video = saved[1], saved[2]
            os.environ.pop(env, None) if saved[3] is None else os.environ.__setitem__(env, saved[3])


if __name__ == "__main__":
    sys.exit(main())
