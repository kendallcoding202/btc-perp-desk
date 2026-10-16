// The page's own build, and a new one picked up by itself (index.html and shadow.html). Each deploy is stamped with the
// commit it was built from (tools/stamp_site.py): <meta name="desk-build"> in the page's head, ?v=<short sha> on its .js
// and .css, and version.json. A page left open (a phone, for hours) reads version.json when it comes back to the
// foreground and every 10 minutes while it shows; on a new build it reloads once, at the same address (the tab, in its
// #hash) with the same storage (the SHADOW key, day or night). The footer shows the build, so a screenshot says which
// code the phone runs. No meta (the local server, a test page): none of this runs.
// Never a loop: Pages' CDN can go on serving the old page for a while after a deploy, so a reload is remembered for this
// tab and page (sessionStorage; each page its own) and there's at most one per 15 minutes, and none where it can't be
// remembered. A version.json that's missing, unreadable or empty, or no network, changes nothing; neither does a form
// being filled in (the SHADOW page's key: focused, or typed in and not sent).
(function () {
  "use strict";
  const KEY = "desk-build", EVERY = 10 * 60 * 1000, ONCE_PER = 15 * 60 * 1000, ASKING = 20 * 1000;
  const BUILD = /^[0-9a-f]{7,64}$/;
  const FIELDS = "textarea, input:not([type]), input[type=text], input[type=password], input[type=search], input[type=email], input[type=url], input[type=tel], input[type=number]";

  // own: this page's build. io: what it reads and does (node runs it with its own): fetch, now, typing, this page's
  // record of its last reload in this tab ({ from, to, at, told }: read, and write, which says whether it stuck), reload
  function updater(own, io) {
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
        // reloaded under 15 min ago and still not this build: the CDN's old page; the next check after that tries
        // again. A reload the clock puts in the future (the phone's clock set back) waits its 15 min from now
        if (last && now < last.at) { io.write({ ...last, at: now }); return "waiting"; }
        if (last && now - last.at < ONCE_PER) return "waiting";
        if (!io.write({ from: own, to: build, at: now })) return "unrecorded";
        io.reload();
        return "reload";
      } catch {
        return "unread";                    // offline, or not JSON
      } finally {
        if (asking === started) asking = 0;   // (a late answer to an old request leaves a newer one's mark)
      }
    };
  }

  // the reload this page just made, when it brought the build it went for: when it was. Only the load right after it
  // says so: a reload the CDN answered with the old page says nothing, and nor does a load by hand after
  function told(own, io) {
    const last = io.read();
    if (!last || last.told) return 0;
    io.write({ ...last, told: true });
    return last.to === own ? last.at || 0 : 0;
  }

  // a field being filled in, on screen: focused (the remember box too), or holding text not sent yet (a field hidden
  // with its form, the SHADOW key's once sent, isn't, even where the browser leaves it focused)
  function typing(doc) {
    const shown = (el) => el.getClientRects().length > 0;
    const a = doc.activeElement;
    return !!(a && a.matches && a.matches("input, textarea, select") && shown(a)) ||
      [...doc.querySelectorAll(FIELDS)].some((el) => el.value !== "" && shown(el));
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
        fetch: (url, opt) => fetch(url, opt), now: () => Date.now(), typing: () => typing(document), reload: () => location.reload(),
        read() { try { return JSON.parse(sessionStorage.getItem(slot)); } catch { return null; } },
        write(rec) { try { const s = JSON.stringify(rec); sessionStorage.setItem(slot, s); return sessionStorage.getItem(slot) === s; } catch { return false; } },
      };
      const el = document.getElementById("build");
      if (el) {
        el.textContent = footText(own, told(own, io));
        el.title = `the code this page runs: commit ${own}. a new version reloads the page by itself`;
        el.hidden = false;
      }
      const check = updater(own, io);
      const shown = () => { if (!document.hidden) check(); };
      document.addEventListener("visibilitychange", shown);
      addEventListener("pageshow", shown);  // the first load, and a page back from the back-forward cache
      addEventListener("focus", shown);
      setInterval(shown, EVERY);
    }
  }
  if (typeof module !== "undefined") module.exports = { updater, told, typing, footText, BUILD, EVERY, ONCE_PER, ASKING, KEY };
})();
