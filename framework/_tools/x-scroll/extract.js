// x-scroll extract.js: a read-only reader for the X posts rendered on the current page. It returns one JSON string and
// never clicks, types or navigates. It reads nothing unless the page is x-scroll's own: its window must hold the nonce
// mark.js stored in this document's memory (a non-enumerable `xScrollTab` data property; see mark.js), else it returns
// only {marked: false, url}, so another agent's tab, even on the same URL, opened from ours (with a copy of our
// sessionStorage) or reloaded, is never captured. xscroll.py runs it as `(<this function>)(<nonce JSON>)` through
// `web act eval --allow-eval -b`, pinned by sha256 (EXTRACT_SHA256 in xscroll.py), so edit both together.
//
// Two layouts. (1) The React app with data-testid hooks, adapted from XActions (https://github.com/nirholas/XActions),
// Copyright (c) 2024-2026 nich (@nichxbt), Apache-2.0 (copy in third_party/XActions-LICENSE): parseTweet in
// src/timelineScraper.js (2026-03-30) and extractTweets in scripts/twitter/scrape-profile-posts.js v2.2.0 (2026-10-09).
// Kept from XActions: article[data-testid="tweet"], the permalink from the <time> element's own anchor (the first
// /status/ link can be the quoted post), textOf (keeps emoji alt text), the show-more truncation flag, socialContext in
// an anchor = repost / plain = pinned, in-reply-to, and a Retry button meaning a rate-limited timeline. Changed: nothing
// is clicked (XActions clicks Retry; here it is a stop signal), counts from the action bar's aria-label, plus quoted
// post, links, alt text, promoted flag and page-state flags. (2) The newer "xds" layout X served logged-out on
// 2026-10-12 (no data-testid, no <time>, quote = nested article in div[role=link][data-href], counts in
// [data-engagement-action]); written for x-scroll from that page. Post time then comes from the id (snowflake).
(nonce) => {
  let mark = null;
  try {
    const d = Object.getOwnPropertyDescriptor(window, 'xScrollTab');   // a data property only: never run a getter
    mark = d && !d.enumerable && 'value' in d ? d.value : null;
  } catch (e) { mark = null; }
  if (typeof nonce !== 'string' || nonce === '' || mark !== nonce) return JSON.stringify({ v: 3, marked: false, url: location.href });
  const vh = window.innerHeight;
  const textOf = (el) => {
    if (!el) return '';
    return Array.from(el.childNodes).map((n) => {
      if (n.nodeType === Node.TEXT_NODE) return n.data;
      if (n.nodeType !== Node.ELEMENT_NODE) return '';
      if (n.tagName === 'IMG') return n.getAttribute('alt') || '';
      if (n.tagName === 'BR') return '\n';
      return textOf(n);
    }).join('');
  };
  const num = (s) => {
    const m = String(s || '').replace(/,/g, '').trim().match(/^([\d.]+)\s*([KMB])?$/i);
    if (!m) return null;
    const mult = { k: 1e3, m: 1e6, b: 1e9 }[(m[2] || '').toLowerCase()] || 1;
    return Math.round(parseFloat(m[1]) * mult);
  };
  const KEYS = [['repl', 'replies'], ['repost', 'reposts'], ['like', 'likes'], ['bookmark', 'bookmarks'], ['view', 'views']];
  const isXHost = (h) => /(^|\.)(x\.com|twitter\.com)$/i.test(h);
  const statusId = (href) => {
    const m = String(href || '').match(/\/status\/(\d+)/);
    return m ? m[1] : null;
  };
  const hostOf = (href) => { try { return new URL(href, 'https://x.com/').hostname; } catch (e) { return ''; } };
  const abs = (href) => { try { return new URL(href, 'https://x.com/').href; } catch (e) { return href; } };
  const pathOf = (href) => { try { return new URL(href, 'https://x.com/').pathname; } catch (e) { return ''; } };
  const atHandle = (root, own) => {
    const s = Array.from(root.querySelectorAll('span')).find((x) => own(x) && /^@\w{1,15}$/.test(x.textContent.trim()));
    return s ? s.textContent.trim().slice(1) : null;
  };

  const counts = (art, own) => {
    const out = { replies: null, reposts: null, likes: null, bookmarks: null, views: null };
    const g = Array.from(art.querySelectorAll('[role="group"][aria-label]')).find(own);
    for (const m of (g ? g.getAttribute('aria-label') : '').matchAll(/([\d][\d,.]*)\s+(repl|repost|like|bookmark|view)/gi)) {
      const k = KEYS.find(([p]) => m[2].toLowerCase().startsWith(p));
      if (k) out[k[1]] = num(m[1]);
    }
    for (const [sel, key] of [['[data-testid="reply"]', 'replies'], ['[data-testid="retweet"], [data-testid="unretweet"]', 'reposts'],
      ['[data-testid="like"], [data-testid="unlike"]', 'likes'], ['[data-testid="bookmark"], [data-testid="removeBookmark"]', 'bookmarks']]) {
      if (out[key] !== null) continue;
      const b = Array.from(art.querySelectorAll(sel)).find(own);
      const m = b && (b.getAttribute('aria-label') || '').match(/^([\d][\d,.]*)\s/);
      if (m) out[key] = num(m[1]);
    }
    for (const [act, key] of [['reply', 'replies'], ['retweet', 'reposts'], ['like', 'likes'], ['bookmark', 'bookmarks'], ['generic', 'views']]) {
      if (out[key] !== null) continue;
      const box = Array.from(art.querySelectorAll(`[data-engagement-action="${act}"]`)).find(own);
      if (!box) continue;
      if (act === 'generic' && !/view/i.test((box.querySelector('[aria-label]') || box).getAttribute('aria-label') || '')) continue;
      const n = box.querySelector('span[dir="ltr"]');
      if (n) out[key] = num(n.textContent);
    }
    if (out.views === null) {
      const v = Array.from(art.querySelectorAll('a[href*="/analytics"]')).find(own);
      const m = v && (v.getAttribute('aria-label') || '').match(/([\d][\d,.]*)\s+view/i);
      if (m) out.views = num(m[1]);
    }
    return out;
  };

  const parse = (art, i) => {
    const quoteBox = Array.from(art.querySelectorAll('div[role="link"]'))
      .find((d) => d.querySelector('[data-testid="User-Name"], article'));
    const own = (el) => !quoteBox || !quoteBox.contains(el);
    const first = (sel) => Array.from(art.querySelectorAll(sel)).find(own) || null;
    const textEl = first('[data-testid="tweetText"]') || first('div[dir="auto"]');
    const inText = (el) => textEl && textEl.contains(el);

    const time = first('time');
    const link = (time && time.closest('a[href*="/status/"]'))
      || Array.from(art.querySelectorAll('a[href*="/status/"]')).find((a) => own(a) && !inText(a)
        && /^\/\w{1,15}\/status\/\d+$/.test(pathOf(a.getAttribute('href'))))
      || first('a[href*="/status/"]');
    const id = statusId(link && link.getAttribute('href'));
    if (!id) return null;
    const nameEl = first('[data-testid="User-Name"]');
    let handle = null, name = null;
    if (nameEl) {
      const a = nameEl.querySelector('a[href^="/"]');
      handle = a ? a.getAttribute('href').slice(1).split(/[/?]/)[0] : atHandle(nameEl, own);
      const ns = nameEl.querySelector('span');
      name = ns ? textOf(ns).trim() : null;
    } else {
      handle = atHandle(art, own);
      const na = handle && Array.from(art.querySelectorAll(`a[href="/${handle}"]`)).find((a) => own(a) && a.textContent.trim()
        && !a.textContent.trim().startsWith('@'));
      name = na ? textOf(na).trim() : null;
    }

    let quote = null;
    if (quoteBox) {
      const qOwn = () => true;
      const qName = quoteBox.querySelector('[data-testid="User-Name"]');
      const qLink = quoteBox.querySelector('a[href*="/status/"]');
      const qText = quoteBox.querySelector('[data-testid="tweetText"]') || quoteBox.querySelector('div[dir="auto"]');
      const qTime = quoteBox.querySelector('time');
      const qid = statusId(quoteBox.getAttribute('data-href')) || statusId(qLink && qLink.getAttribute('href'));
      const qHandle = qName ? ((qName.querySelector('a[href^="/"]') || { getAttribute: () => null }).getAttribute('href') || '').slice(1).split(/[/?]/)[0]
        || atHandle(qName, qOwn) : atHandle(quoteBox, qOwn);
      quote = { id: qid, url: qid && qHandle ? `https://x.com/${qHandle}/status/${qid}` : null, handle: qHandle || null,
        created_at: qTime ? qTime.getAttribute('datetime') : null, text: textOf(qText).trim() };
    }

    let reposter = null, pinned = false;
    const social = first('[data-testid="socialContext"]');
    if (social) {
      const sa = social.closest('a[href^="/"]');
      reposter = sa ? sa.getAttribute('href').slice(1).split(/[/?]/)[0] : null;
      pinned = !sa;
    } else if (art.firstElementChild && !art.firstElementChild.querySelector('a[href*="/status/"], img')) {
      const head = (art.firstElementChild.innerText || '').trim();   // xds: "Pinned" or "<name> reposted" above the post
      pinned = /^pinned$/i.test(head);
      if (/reposted$/i.test(head)) {
        const ra = art.firstElementChild.querySelector('a[href^="/"]');
        reposter = ra ? ra.getAttribute('href').slice(1).split(/[/?]/)[0] : head.replace(/\s*reposted$/i, '');
      }
    }

    const links = [];
    for (const a of (textEl ? textEl.querySelectorAll('a[href]') : [])) {
      const href = abs(a.getAttribute('href'));
      if (!isXHost(hostOf(href))) links.push({ url: href, text: a.textContent.trim() });
    }
    const card = first('[data-testid="card.wrapper"]');
    const cardA = card ? card.querySelector('a[href]') : null;
    if (cardA && !isXHost(hostOf(cardA.getAttribute('href'))) && !links.some((l) => l.url === abs(cardA.getAttribute('href')))) {
      links.push({ url: abs(cardA.getAttribute('href')), text: (card.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160) });
    }
    const photos = Array.from(art.querySelectorAll('[data-testid="tweetPhoto"] img, img[src*="pbs.twimg.com/media"]'))
      .filter((p, k, all) => own(p) && all.indexOf(p) === k);
    const video = !!first('[data-testid="videoPlayer"], [data-testid="videoComponent"], video');
    const gif = video && !!first('[data-testid="gifPlayer"]');
    const poll = !!first('[data-testid="cardPoll"], [data-testid="poll"]');
    const mediaType = poll ? 'poll' : gif ? 'gif' : video ? 'video' : photos.length ? 'image' : card ? 'card' : 'none';
    const promoted = Array.from(art.querySelectorAll('span')).some((s) => own(s) && !inText(s) && s.children.length === 0
      && /^(Ad|Promoted)$/.test(s.textContent.trim()));
    const reply = !!first('[data-testid="in-reply-to"]')
      || Array.from(art.querySelectorAll('div[dir]')).some((el) => own(el) && (el.innerText || '').startsWith('Replying to'));
    const truncated = !!first('[data-testid="tweet-text-show-more-link"]')
      || Array.from(art.querySelectorAll('a, button, span[role="button"]')).some((b) => own(b) && /^show more$/i.test(b.textContent.trim()));
    const r = art.getBoundingClientRect();
    return {
      id, url: handle ? `https://x.com/${handle}/status/${id}` : abs(link.getAttribute('href')).split('?')[0],
      handle, name, created_at: time ? time.getAttribute('datetime') : null,
      text: textOf(textEl).trim(), is_truncated: truncated, lang: textEl ? textEl.getAttribute('lang') : null,
      counts: counts(art, own), quote, reposted_by: reposter, is_pinned: pinned, is_reply: reply, is_promoted: promoted,
      media: { type: mediaType, count: photos.length || (video ? 1 : 0),
        alt: photos.map((p) => p.alt || '').filter((a) => a && a !== 'Image') },
      links, position: i, in_view: r.bottom > 0 && r.top < vh,
    };
  };

  const arts = Array.from(document.querySelectorAll('article')).filter((a) => !a.parentElement.closest('article')
    && (a.getAttribute('data-testid') === 'tweet' || (!a.hasAttribute('data-testid') && a.querySelector('a[href*="/status/"]'))));
  const posts = [];
  arts.forEach((art, i) => {
    try { const p = parse(art, i); if (p) posts.push(p); } catch (e) { /* one odd article must not lose the rest */ }
  });

  // Page state. Text checks run on the page chrome only (main column minus the posts, plus dialogs and toasts), so a
  // post that merely mentions "rate limit" can't stop the session.
  const primary = document.querySelector('[data-testid="primaryColumn"]') || document.querySelector('main');
  let chrome = primary ? (primary.innerText || '') : '';
  for (const a of arts) { const t = a.innerText || ''; if (t) chrome = chrome.split(t).join(' '); }
  for (const el of document.querySelectorAll('#layers, [role="dialog"], [role="alertdialog"], [role="alert"]')) {
    chrome += '\n' + (el.innerText || '');
  }
  const path = location.pathname;
  const retry = Array.from(document.querySelectorAll('[role="button"], button'))
    .some((b) => !b.closest('article') && /^(retry|try again)$/i.test((b.innerText || '').trim()));
  const frames = Array.from(document.querySelectorAll('iframe')).map((f) => f.src || '').join(' ');
  const loginLinks = !!document.querySelector('a[href*="mode=login"], a[href*="mode=signup"], a[href^="/i/flow/login"], '
    + 'a[href^="/i/flow/signup"], a[href="/login"], [data-testid="loginButton"]');
  const flags = {
    login_page: /^\/(i\/flow\/(login|signup)|i\/jf\/onboarding|login)\b/.test(path),
    account_page: /^\/account\/(access|suspended|locked)/.test(path),
    captcha: /arkoselabs|funcaptcha|recaptcha|hcaptcha|challenges\.cloudflare\.com/i.test(frames),
    retry,
    something_wrong: /something went wrong/i.test(chrome),
    rate_limit: /rate limit exceeded|over the daily limit|too many requests/i.test(chrome),
    locked: /your account (is|has been) (locked|suspended|temporarily limited)|account (is|has been) locked/i.test(chrome),
    unusual: /unusual (login )?activity|(verify|confirm) (that )?it(’|')s you/i.test(chrome),
    no_such_account: /this account doesn(’|')t exist|account suspended/i.test(chrome),
    no_results: /no results for/i.test(chrome),
    login_links: loginLinks,
  };
  const legacyIn = !!document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="AppTabBar_Profile_Link"]');
  const navIn = !!document.querySelector('a[href="/notifications"], a[href="/i/bookmarks"], a[href="/messages"], a[href^="/i/chat"]');
  return JSON.stringify({ v: 3, marked: true, url: location.href, title: document.title, logged_in: legacyIn || (navIn && !loginLinks),
    layout: document.querySelector('[data-testid]') ? 'testid' : 'xds', vh, scroll_y: window.scrollY, flags, posts });
}
