# x-scroll

One slow, read-only scroll of an X account per day through odin's identity browser (`web act`). Every post it sees
is saved to JSONL, then one `claude -p` call (route `bulk-sweep`, Haiku today) writes a daily digest and a rolling profile into the vault. Runs on odin only,
because `web act` refuses `--host thor`.

**Approach:** it follows **XActions** (https://github.com/nirholas/XActions, ★586, Apache-2.0, last pushed 2026-10-11).
A page script reads the posts on screen in a real logged-in browser. It scrolls less than one screen at a time because
X drops posts that scroll far out of view. It stops after a few scrolls with no new posts, and a Retry button means a
rate limit. `extract.js` adapts XActions' `parseTweet` / `extractTweets` (license in `third_party/`) and also reads the
"xds" layout X served logged-out on 2026-10-12. No recent YouTube walkthrough was found. From the Facebook scraper
(the example-project-f persona) it takes only the loop shape (session, scroll, extract, log every post, dedupe, store,
distill), none of its topic.

## the user, once
1. **Pick the account.** A new scroll-only X account is safest. `x-scroll login` uses Sign in with Google with the
   identity browser's Google account (<your-github>). If your main X account uses that Gmail, Google sign-in opens the
   main account. So either make the scroll account on a second Google account (add it to the identity browser with a
   `web handoff`), or pass `--account main` on purpose.
2. **Set up the account** (your phone is fine). Follow the people you want. On Home, tap the **Following** tab once;
   X remembers it, and x-scroll never clicks. Turn video autoplay off: Settings > Accessibility > Autoplay: Never.
3. **Install, then log in** (on odin, in this folder): `./install.sh` (puts `x-scroll` on your PATH; enables
   nothing), then `x-scroll login --account dedicated`. If it prints a link, tap it on your phone, then run
   `x-scroll login --wait`.
4. **Check one run:** watch `x-scroll session --minutes 5`, then `x-scroll distill`. If the Home posts are mostly
   from people you don't follow, the identity browser is on "For you": open a `web handoff` on x.com/home and tap
   Following there once. If both look right, run `systemctl --user enable --now x-scroll.timer`.

## What it watches
    x-scroll watch add @handle "search words"   # a handle opens its profile; words open a Latest search
    x-scroll watch rm @handle  |  x-scroll watch list  |  x-scroll watch minutes 12
The list lives in `~/.config/x-scroll/watch.json`: handles, searches, an optional `"list": "https://x.com/i/lists/<id>"`
and minutes. Each session spends about 40% of its time on the Following timeline, then visits handles and searches,
least recently visited first. A profile or search page stops at the first post older than 14 days or older than the
newest post it saw there last time.

## Where data lives
- **Raw data**, in `~/.local/state/x-scroll/` (0700, never in the vault): `captures/<date>/<sid>.jsonl` (every post
  seen, deleted after 60 days), `sessions.jsonl`, `seen.tsv`, `pages.json`, `pause-until`, `profile.json`
- **Vault notes:**
  - `Research/x/<date>.md`: the daily digest
  - `Research/x/profile.md`: the rolling profile
  - `Reference/lane-intel/x/<date>-x-scroll.md`: claims about models and harnesses. They are written as bullets so
    lane-intel's stance.py won't fold strangers' claims in on its own.

## Stop it
`systemctl --user disable --now x-scroll.timer`, or create `_system/automations/HALT`. `x-scroll status` shows state.

## Ban-risk notes
- **It can't interact.** `xscroll.py` sends `web act` only open, wait, get url, its own tab, and eval of `mark.js`,
  `extract.js` or `scroll.js` (each checked by sha256; `mark.js` and `extract.js` take only a tab nonce, `scroll.js`
  only a validated plan of path, feed params, pixels and that nonce). It cannot like, post, follow or reply.
- **Light, irregular use.** One session a day, at most 20 minutes (8 for the first two weeks). It starts at a random
  time between 19:00 and 22:00 and rests about one day a week. It waits 2-6 s between scrolls, with a 20-60 s pause
  every 15-30 scrolls. It uses the home IP, one tab, and autoplay off.
- **It stops at the first warning sign:** a login wall, CAPTCHA, rate limit or Retry button, "Something went wrong",
  a locked account or an unusual-activity notice. It then pauses for 72 hours and sends one silent `tg misc` line.
  It never starts a handoff on its own.
- **It can be detected.** agent-browser connects to the browser through Chrome's debugging port (CDP) for every
  command, and web's own test found that connection detectable. Use an account you can afford to lose.
- **The browser is shared.** x-scroll uses its own labelled tab, closes only that tab, and never runs `web act close`.
  Before every capture step it checks that this tab is active and on the planned page; if not, it re-selects the tab,
  re-opens the page once in a fresh labelled tab (never `open` on the active tab), and otherwise ends the session with
  `wrong-tab`. It checks again right before every scroll, and the scroll itself is `scroll.js`, which re-checks the
  page's URL and x-scroll's tab mark and scrolls in the same page-side evaluation (never `web act scroll`, which hits
  whatever tab is active by then). If the tab changed hands, nothing scrolls and the session ends with `wrong-tab`.
  **Page identity is a secret in the page's memory, not the URL or the tab:** each tab x-scroll opens gets a fresh
  random nonce in its URL (`x.com/home#xs-<nonce>`), and `mark.js`, run right after in the same step, stores it in that
  page's own memory (a non-enumerable `xScrollTab` property on `window`) only on a page loaded with that fragment (still
  in the URL, or already dropped by X's router but kept in the page's navigation entry). `extract.js` reads and
  `scroll.js` scrolls only where the page's memory holds the nonce (`scroll.js` also requires the navigation entry to
  carry the fragment). That memory belongs to the one document x-scroll's `tab new` loaded: it survives X's in-app
  navigation, but not a reload, and no other page has it, so another agent's tab on the very same URL (such as
  `x.com/home`, even one opened at the same moment, or at x-scroll's full URL copied from `tab list`), a page another
  agent loads into x-scroll's tab, a window opened from that tab (which gets a copy of its sessionStorage, not its
  memory) and any reloaded page are never read or scrolled; the session ends with `wrong-tab`. Nothing marks a page
  except x-scroll's own open step. An unmarked sign-in or lock page in x-scroll's labelled tab (X sent it there with a
  full page load) is not read either, but judged from its URL alone, so it still ends the session as a block and
  pauses the account (a sign-in page on the first page is `needs_login`), once `tab list` shows that labelled tab is
  still the active one and on such a page; another agent's own X sign-in or lock tab switched in for the read ends
  the session with `wrong-tab`, without a pause. If the tab in front never carries the
  fragment (another agent took it, or X hard-redirects before the mark, e.g. to sign in), the session ends with
  `error:Unmarked` before reading anything. To confirm on the first supervised live run: the first page's session does
  not end `Unmarked` or `wrong-tab` (X keeps the `#xs-` fragment through its first document and routes in-app after it).
  "The planned page" means the same path and, for a search, the same `q`, `f` (no `f` is Top, never Latest), `pf` and `lf`.

Commands: `x-scroll session [--minutes N] [--dry-run]`, `distill [--date D] [--input F] [--out DIR] [--force]`, `status`,
`--self-test`; `./install.sh [--dry-run]` links the CLI and units (a different file already there is kept as `<name>.bak-<time>`) and never enables the timer.
