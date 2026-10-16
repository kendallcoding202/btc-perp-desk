// The page's own build, and a new one picked up by itself (index.html and shadow.html). Each deploy is stamped with the
// commit it was built from (tools/stamp_site.py): <meta name="desk-build"> at the start of the page's head, ?v=<short
// sha> on its .js and .css, and version.json. A page left open (a phone, for hours) reads version.json when it comes
// back to the foreground and every 10 minutes while it shows; on a new build it reloads once, at the same address (the
// tab, in its #hash) with ?b=<the new build's short sha> on it, which fetches the page past any cached copy and comes
// off the address as the page loads; with the same storage (the SHADOW key, day or night), and back where it was
// scrolled to. The footer shows the build, so a screenshot says which code the phone runs. No meta (the local server, a
// test page): none of this runs.
// Never a loop, by two guards that don't lean on each other. Pages' CDN can go on serving the old page for a while
// after a deploy, so a reload is remembered for this tab and page (sessionStorage; each page its own) and there's at
// most one per 15 minutes, and none where it can't be remembered; and a page its own reload brought (its ?b=) still
// without that build waits 15 minutes before another, kept in the page itself, so a tab whose storage was lost on the
// way (cleared, or a write the browser dropped) never reloads sooner. A version.json that's missing, unreadable or
// empty, or no network, changes nothing; neither does a form being filled in (the SHADOW page's key: focused, or
// typed in and not sent).
(function () {
  "use strict";
  const KEY = "desk-build", EVERY = 10 * 60 * 1000, ONCE_PER = 15 * 60 * 1000, ASKING = 20 * 1000, KEEP = 10 * 1000;
  const BUILD = /^[0-9a-f]{7,64}$/, MARK = /^[0-9a-f]{7}$/;
  const FIELDS = "textarea, input:not([type]), input[type=text], input[type=password], input[type=search], input[type=email], input[type=url], input[type=tel], input[type=number]";

  // own: this page's build. held: when its own reload brought this page without the build it went for (the CDN's old
  // page), else 0. io: what it reads and does (node runs it with its own): fetch, now, typing, scroll (how far down the
  // page is), this page's record of its last reload in this tab ({ from, to, at, y }: read, and write, which says
  // whether it stuck), reload (onto a build)
  function updater(own, io, held = 0) {
    let asking = 0;
    return async function check() {
      // the foreground's events come together: one request. One that never answers (a page frozen mid-request, back
      // from the back-forward cache) holds the next for 20 s at most
      const started = io.now();
      if (asking && Math.abs(started - asking) < ASKING) return "asking";
      asking = started;
      try {
        const res = await io.fetch(`version.json?t=${io.now()}`, { cache: "no-store" });
        if (!res.ok) return "unread";
        const v = await res.json();
        const build = v && typeof v.build === "string" ? v.build : "";
        if (!BUILD.test(build)) return "unread";
        if (build === own) return "same";
        if (io.typing()) return "typing";   // the next check, once it's sent or left
        const last = io.read(), now = io.now();
        // brought back by its own reload, still the old build: 15 min from then, whatever the tab's storage says
        if (held > now) held = now;         // (the clock set back: from now)
        if (held && now - held < ONCE_PER) return "waiting";
        // reloaded under 15 min ago and still not this build: the CDN's old page; the next check after that tries
        // again. A reload the clock puts in the future (the phone's clock set back) waits its 15 min from now
        if (last && now < last.at) { io.write({ ...last, at: now }); return "waiting"; }
        if (last && now - last.at < ONCE_PER) return "waiting";
        if (!io.write({ from: own, to: build, at: now, y: io.scroll() })) return "unrecorded";
        io.reload(build);
        return "reload";
      } catch {
        return "unread";                    // offline, or not JSON
      } finally {
        if (asking === started) asking = 0;   // (a late answer to an old request leaves a newer one's mark)
      }
    };
  }

  // this load, by its address: the reload this page made for a build (?b=, its short sha) and whether it brought it,
  // { b, got }; null for any other load (by hand, a link, the back button: the page takes ?b= off as it loads)
  function landed(own, search) {
    const b = new URLSearchParams(search).get("b");
    return b && MARK.test(b) ? { b, got: own.startsWith(b) } : null;
  }

  // a field being filled in, on screen: focused (the remember box too), or holding text not sent yet (a field hidden
  // with its form, the SHADOW key's once sent, isn't, even where the browser leaves it focused)
  function typing(doc) {
    const shown = (el) => el.getClientRects().length > 0;
    const a = doc.activeElement;
    return !!(a && a.matches && a.matches("input, textarea, select") && shown(a)) ||
      [...doc.querySelectorAll(FIELDS)].some((el) => el.value !== "" && shown(el));
  }

  // Back where it was: the scroll position the reload saved (y), once the page is tall enough for it (its first
  // render: the board drawn, the SHADOW board opened), and put back as the page lays itself out, for 10 s. Never
  // against the owner (a touch, the wheel, a key or a click ends it) nor the browser (a page it has already scrolled
  // itself is left where it is)
  function restore(y) {
    if (!(y > 0) || scrollY > 0 || typeof ResizeObserver === "undefined") return;
    const root = document.documentElement, INPUT = ["touchstart", "wheel", "keydown", "mousedown"];
    const ro = new ResizeObserver(() => { if (root.scrollHeight - innerHeight >= y && Math.abs(scrollY - y) >= 1) scrollTo(0, y); });
    const stop = () => { ro.disconnect(); clearTimeout(end); INPUT.forEach((e) => removeEventListener(e, stop, true)); };
    const end = setTimeout(stop, KEEP);
    INPUT.forEach((e) => addEventListener(e, stop, { capture: true, passive: true }));
    ro.observe(document.body);
  }

  // "9:12 am MT" (America/Denver's own rules, whatever zone the phone is in); no time where the browser can't
  const MT = (() => {
    try { return new Intl.DateTimeFormat("en-US", { timeZone: "America/Denver", hour: "numeric", minute: "2-digit", hour12: true }); } catch { return null; }
  })();
  const clock = (ms) => {
    if (!MT || !ms) return "";
    const p = {};
    for (const x of MT.formatToParts(new Date(ms))) p[x.type] = x.value;
    return ` at ${p.hour}:${p.minute} ${String(p.dayPeriod || "").toLowerCase()} MT`;
  };
  const footText = (own, at) => `build\u00a0${own.slice(0, 7)}` + (at ? ` · this page updated itself to the newest version${clock(at)}` : "");

  if (typeof document !== "undefined") {
    const meta = document.querySelector('meta[name="desk-build"]');
    const own = meta ? String(meta.content).trim() : "";
    if (BUILD.test(own)) {
      const slot = `${KEY} ${location.pathname.replace(/index\.html$/, "")}`;   // this page's record ("./" and index.html: one)
      const io = {
        fetch: (url, opt) => fetch(url, opt), now: () => Date.now(), typing: () => typing(document), scroll: () => Math.round(scrollY),
        read() { try { return JSON.parse(sessionStorage.getItem(slot)); } catch { return null; } },
        write(rec) { try { const s = JSON.stringify(rec); sessionStorage.setItem(slot, s); return sessionStorage.getItem(slot) === s; } catch { return false; } },
        // the same address and #hash, ?b= added: a page load like a reload's, never a copy the browser or the CDN kept
        reload(build) { const q = new URLSearchParams(location.search); q.set("b", build.slice(0, 7)); location.replace(`${location.pathname}?${q}${location.hash}`); },
      };
      const came = landed(own, location.search), q = new URLSearchParams(location.search);
      if (q.has("b")) {                     // off the address, so a reload by hand, a bookmark or a shared link is plain
        q.delete("b");
        try { history.replaceState(history.state, "", `${location.pathname}${String(q) ? "?" + q : ""}${location.hash}`); } catch { /* it stays: harmless */ }
      }
      const el = document.getElementById("build");
      if (el) {
        el.textContent = footText(own, came && came.got ? io.now() : 0);
        el.title = `the code this page runs: commit ${own}. a new version reloads the page by itself`;
        el.hidden = false;
      }
      const last = io.read();
      if (came && last && String(last.to).startsWith(came.b)) restore(last.y);
      const check = updater(own, io, came && !came.got ? io.now() : 0);
      const shown = () => { if (!document.hidden) check(); };
      document.addEventListener("visibilitychange", shown);
      addEventListener("pageshow", shown);  // the first load, and a page back from the back-forward cache
      addEventListener("focus", shown);
      setInterval(shown, EVERY);
    }
  }
  if (typeof module !== "undefined") module.exports = { updater, landed, typing, footText, BUILD, EVERY, ONCE_PER, ASKING, KEEP, KEY };
})();
