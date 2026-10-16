// The page's two editions: day (cream paper) and night (dark paper). "auto" follows the device's light / dark setting;
// day or night picked in the footer is remembered on this device only (localStorage, and the page works without it).
// index.html's head sets html[data-edition] and the phone's status bar (theme-color) before the first paint; this wires
// the footer switch, keeps the status bar in step, and tells app.js and spider.js to repaint their canvases ("desk:theme").
(() => {
  "use strict";
  const root = document.documentElement, KEY = "desk-edition";
  const dark = matchMedia("(prefers-color-scheme: dark)");
  const store = {
    get() { try { return localStorage.getItem(KEY); } catch { return null; } },
    set(v) { try { if (v === "auto") localStorage.removeItem(KEY); else localStorage.setItem(KEY, v); } catch { /* private mode */ } },
  };
  // index.html has one theme-color per edition, each with its own media query; a picked edition sets both to its color
  // (index.html's head already did, before the first paint, and kept each one's own color in data-own)
  const metas = [...document.querySelectorAll('meta[name="theme-color"]')];
  const color = { day: "", night: "" };
  metas.forEach((m) => { if (!m.dataset.own) m.dataset.own = m.content; color[/dark/.test(m.media) ? "night" : "day"] = m.dataset.own; });
  const picked = () => { const v = root.dataset.edition; return v === "day" || v === "night" ? v : "auto"; };
  const shown = () => picked() === "auto" ? (dark.matches ? "night" : "day") : picked();
  let last = shown();

  function apply(choice) {
    if (choice === "day" || choice === "night") root.dataset.edition = choice; else delete root.dataset.edition;
    metas.forEach((m) => { m.content = choice === "auto" ? m.dataset.own : color[choice]; });
    document.querySelectorAll("[data-edition-pick]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.editionPick === choice)));
    const now = shown();
    if (now !== last) { last = now; dispatchEvent(new CustomEvent("desk:theme", { detail: { edition: now } })); }
  }

  const saved = store.get();
  apply(saved === "day" || saved === "night" ? saved : "auto");
  document.querySelectorAll("[data-edition-pick]").forEach((b) => b.addEventListener("click", () => {
    store.set(b.dataset.editionPick);
    apply(b.dataset.editionPick);
  }));
  const follow = () => apply(picked());   // auto: the device switched between light and dark
  if (dark.addEventListener) dark.addEventListener("change", follow); else if (dark.addListener) dark.addListener(follow);
})();
