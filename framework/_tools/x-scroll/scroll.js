// x-scroll scroll.js: the only way x-scroll scrolls. It checks that the page it runs in is x-scroll's own page on the
// planned X URL and only then scrolls it, in one page-side evaluation, so no other agent can take the shared browser's
// active tab between the check and the scroll (a separate `web act scroll` would land on whatever tab is active by
// then). It never clicks, types or navigates, and reads only x-scroll's in-memory mark and the page's own navigation
// timing entry. xscroll.py runs it as `(<this function>)(<plan JSON>)` through `web act eval --allow-eval -b`, pinned
// by sha256 (SCROLL_SHA256 in xscroll.py), so edit both together. The plan is {path, q, f, pf, lf, px, nonce}: the
// planned page's lowercased path without trailing slashes, its feed params as xscroll.feed() gives them (lists, or
// null when absent; f defaults to ["top"]), the pixels to scroll, and the nonce mark.js stored in the memory of the
// document x-scroll's `tab new` loaded. Identity is that in-memory mark (a non-enumerable `xScrollTab` data property on
// window), not the URL, the tab or its storage: another agent's tab on the very same URL, a window opened from ours
// (which gets a copy of our sessionStorage, not our memory), a page another agent loads into our tab and any reload
// are all new documents without it, so none is scrolled. The document's navigation entry must also carry #xs-<nonce>
// (X's pushState/replaceState never change it); a reload is no evidence of ownership.
(plan) => {
  const vals = (params, key) => {
    const v = params.getAll(key).filter((s) => s !== '');
    return v.length ? v : null;
  };
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const host = location.hostname;
  const path = location.pathname.replace(/\/+$/, '').toLowerCase();
  const params = new URLSearchParams(location.search);
  const feed = { q: vals(params, 'q'), f: vals(params, 'f') || ['top'], pf: vals(params, 'pf'), lf: vals(params, 'lf') };
  let mark = null;
  try {
    const d = Object.getOwnPropertyDescriptor(window, 'xScrollTab');   // a data property only: never run a getter
    mark = d && !d.enumerable && 'value' in d ? d.value : null;
  } catch (e) { mark = null; }
  let doc = false;
  try { doc = new URL(performance.getEntriesByType('navigation')[0].name).hash === '#xs-' + plan.nonce; } catch (e) { doc = false; }
  const ours = location.protocol === 'https:' && (host === 'x.com' || host === 'www.x.com') && path === plan.path
    && same(feed.q, plan.q) && same(feed.f, plan.f) && same(feed.pf, plan.pf) && same(feed.lf, plan.lf)
    && typeof plan.nonce === 'string' && plan.nonce !== '' && mark === plan.nonce && doc;
  const y0 = window.scrollY;
  if (ours) window.scrollBy(0, plan.px);
  return JSON.stringify({ v: 5, scrolled: ours, marked: mark === plan.nonce, doc, url: location.href, y0,
    y1: window.scrollY });
}
