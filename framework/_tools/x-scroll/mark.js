// x-scroll mark.js: marks the page x-scroll just opened as its own. x-scroll opens each tab at the planned URL plus a
// fragment `#xs-<nonce>`, a fresh random 128-bit nonce only that `tab new` ever carried; this script, run right after
// (the same step, and nowhere else), stores the nonce in the page's own memory, a non-enumerable read-only property
// `xScrollTab` on window, only when the page in front of it is an https x.com page loaded with that very fragment (it
// is still in the URL, or X's router already dropped it with replaceState but the document's navigation entry, which
// history calls never change, still carries it), so a tab another agent switched in meanwhile (even on the same URL,
// opened at the same moment) is never marked. extract.js and scroll.js then act only on a page whose window holds the
// current nonce. That memory belongs to this one document: it survives X's in-app navigation (pushState, same
// document) but not a reload or any other new document, and a window opened from this one never gets a copy of it
// (sessionStorage, which earlier versions used, is cloned into such windows and kept across reloads and other pages
// loaded into the tab). A page without it is not x-scroll's, whatever its URL, fragment or storage say. It never
// clicks, types or navigates. xscroll.py runs it as `(<this function>)(<nonce JSON>)` through
// `web act eval --allow-eval -b`, pinned by sha256 (MARK_SHA256 in xscroll.py), so edit both together.
(nonce) => {
  const key = 'xScrollTab';
  const host = location.hostname;
  let marked = false;
  try {
    let loaded = '';
    try { loaded = new URL(performance.getEntriesByType('navigation')[0].name).hash; } catch (e) { loaded = ''; }
    if (typeof nonce === 'string' && /^[0-9a-f]{32}$/.test(nonce) && location.protocol === 'https:'
        && (host === 'x.com' || host === 'www.x.com') && (location.hash === '#xs-' + nonce || loaded === '#xs-' + nonce)) {
      Object.defineProperty(window, key, { value: nonce, enumerable: false, writable: false, configurable: true });
    }
    const d = Object.getOwnPropertyDescriptor(window, key);   // a data property only: never run a getter
    marked = !!d && !d.enumerable && 'value' in d && d.value === nonce;
  } catch (e) {
    marked = false;
  }
  return JSON.stringify({ v: 2, marked, url: location.href });
}
