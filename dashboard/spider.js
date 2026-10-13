// The walking crawler. A pixel spider that goes to whatever the desk just did, so you can watch the desk
// work through its checks. Every move comes from the board's real state:
//   a new board (a run landed, or another tab)   the agent cards in the order the desk checks their gates (the
//                                                board's own gate list: kills, data, hard, scan, soft, size),
//                                                staying longest on the one holding the entry back, then the
//                                                newest candle, then the BUY / SHORT LINE
//   a live price tick (at most one per 20 s)     a quick hop to the price in the ticker, then back
//   a new fill (BOUGHT, SOLD, SHORTED, ...)      it runs to that event line and stays a few seconds
//   nothing new                                  a slow walk along the panel edges, long rests, now and then a
//                                                drop on silk
//   no board yet, or one too old to trust        it curls up at home and keeps still until a fresh board lands
// On the VT basket's tab (no agents, chart or trigger line) the same moves read the basket instead:
//   a new board                                  each coin's row in #holdings (BTC, ETH, SOL), by its order or its
//                                                50-day average, never on their words (a row with no room for it
//                                                is skipped): longer on a coin in a different state from the other
//                                                two, a twitch on one with an order waiting for the next open;
//                                                then the bank in the basket's ticker. A row that is scrolled
//                                                away (a phone, at the top of the page) has the coin's name in
//                                                the VT tab's label stand in for it, and waits for later too
//   a live tick that changes the bank's figure   a quick hop to it, at most one per 20 s (the basket has no price)
//   a new fill (BOUGHT, SOLD, ADDED, TRIMMED)    its event line, or that coin's row when the line is scrolled away
// It only goes where you can see it. A stop that is scrolled out of view waits (one peek from the screen's edge
// towards it) and is visited when you scroll to it; it never scrolls the page for you.
// It is decoration. It changes nothing about how the desk trades: the rules are fixed in desk/thresholds.py.
// app.js tells it about boards and ticks with two window events ("desk:board", "desk:tick"); everything else
// it reads from the page. It never takes a click (pointer-events: none), stops while the tab is hidden, sits
// still under prefers-reduced-motion, and can be switched off in the footer.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
  const rand = (a, b) => a + Math.random() * (b - a);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const ease = (k) => (k < 0.5 ? 2 * k * k : 1 - (2 - 2 * k) ** 2 / 2);

  // The CRAWLER card's sprite (X = body, k = eye), a second leg pose for walking, and curled up asleep.
  const POSES = [
    ["X..........X", ".X........X.", "..X.XXXX.X..", "...XXXXXX...", "XXXXkXXkXXXX", "...XXXXXX...",
     "..XXXXXXXX..", ".X.XXXXXX.X.", "X...XXXX...X", "...X....X...", "..X......X..", "............"],
    [".X........X.", ".X........X.", "..X.XXXX.X..", "X..XXXXXX..X", ".XXXkXXkXXX.", "...XXXXXX...",
     "X.XXXXXXXX.X", ".X.XXXXXX.X.", "....XXXX....", "..XX....XX..", ".X........X.", "............"],
    ["............", "............", "............", "....XXXX....", "..XXXXXXXX..", ".XXkkXXkkXX.",
     ".XXXXXXXXXX.", "X.XXXXXXXX.X", ".X.XXXXXX.X.", "X.X.XXXX.X.X", ".X.X....X.X.", "............"],
  ];
  // filter.py's order, by the card that owns each gate. Used only when a board lists no gates.
  const ORDER = ["risk", "chief", "crawler", "vet", "crowd", "fills", "scan", "size"];
  const TRADE = /\b(BOUGHT|SOLD|SHORTED|COVERED|ADDED|TRIMMED|STOP|HALT)\b/;
  const HOP_EVERY = 20000;              // at most one hop to the ticker per 20 s
  const RETOUR = 5 * 60000;             // same coin, same bar: a fresh `updated` tours again after 5 min (live server)
  // a board older than this means the desk is not running. The static board is published by every desk run:
  // GitHub's 15-min schedule fires only now and then, and the hourly backup makes sure of at least one run per
  // 2h bar, so a board up to ~2h old is normal and one past 2.5h has missed a bar. The live server steps every
  // minute or so
  const STALE_STATIC = 150 * 60000, STALE_LIVE = 5 * 60000;

  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const phone = matchMedia("(max-width: 560px)");
  const store = {
    get() { try { return localStorage.getItem("desk-spider"); } catch { return null; } },
    set(v) { try { localStorage.setItem("desk-spider", v); } catch { /* private mode */ } },
  };

  // ---- the sprite, in a layer clipped to the frame: it can never move, widen or lengthen the page ------
  const frameEl = document.querySelector(".frame") || document.body;
  const layer = document.createElement("div");
  layer.className = "spider-layer";
  layer.setAttribute("aria-hidden", "true");
  layer.style.visibility = "hidden";   // until the first board lays the page out (or 2.5 s without one): see reveal()
  const silkEl = document.createElement("i");
  silkEl.className = "spider-silk";
  const body = document.createElement("canvas");
  body.className = "spider";
  body.width = body.height = 12;
  layer.append(silkEl, body);
  frameEl.append(layer);

  const pink = getComputedStyle(root).getPropertyValue("--pink").trim() || "#ff5fd2";
  const frames = POSES.map((map) => {
    const c = document.createElement("canvas");
    c.width = c.height = 12;
    const g = c.getContext("2d");
    map.forEach((row, y) => [...row].forEach((ch, x) => {
      if (ch === ".") return;
      g.fillStyle = ch === "k" ? "#0d0f1f" : pink;
      g.fillRect(x, y, 1, 1);
    }));
    return c;
  });
  const g = body.getContext("2d");
  let drawn = -1;
  function pose(n) {
    if (n === drawn) return;
    drawn = n;
    g.clearRect(0, 0, 12, 12);
    g.drawImage(frames[n], 0, 0);
  }

  // ---- geometry: element boxes in page coordinates, measured when a visit starts, never per frame -------
  let S = 0, fx = 0, fy = 0, fw = 0, fh = 0, dirty = true;
  const boxes = new Map();
  function refresh() {
    boxes.clear();
    dirty = false;
    const size = phone.matches ? 24 : 36;
    if (size !== S) { S = size; body.style.width = body.style.height = `${S}px`; }
    const r = frameEl.getBoundingClientRect();
    fx = r.left + scrollX + frameEl.clientLeft; fy = r.top + scrollY + frameEl.clientTop;
    fw = frameEl.clientWidth; fh = frameEl.clientHeight;
  }
  function box(el) {
    if (dirty) refresh();
    if (!el) return null;
    if (!boxes.has(el)) {
      const r = el.isConnected ? el.getBoundingClientRect() : null;
      boxes.set(el, r && r.width > 0 && r.height > 0 ? { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height } : null);
    }
    return boxes.get(el);
  }
  const touch = () => { dirty = true; };
  // is page-y on screen, `m` px clear of the top and bottom? (m = S / 2: the whole sprite shows; -S / 2: any of it)
  const seen = (y, m) => { if (dirty) refresh(); return y > scrollY + m && y < scrollY + innerHeight - m; };
  // where it stands on an element's top (or bottom) edge, a fraction f of the way along; `inset` > 0 is inside
  const edge = (b, top, f, inset = -0.22) => ({ x: b.x + b.w * f, y: top ? b.y + S * inset : b.y + b.h - S * inset });

  // ---- the spots it visits ------------------------------------------------------------------------
  // the VT basket's tab is showing (app.js's render() sets the class before it tells the spider anything)
  const basket = () => document.body.classList.contains("kind-basket");
  const tickerEl = () => $(basket() ? "vt-ticker" : "ticker");
  function home() {               // desktop: the hub app.js spins the chart web from. narrower: the stage's top edge
    const wrap = box(document.querySelector(".chart-wrap"));   // (VT: no stage; the basket panel's top edge)
    if (wrap && innerWidth >= 980) return { x: wrap.x + wrap.w * 0.56, y: wrap.y + wrap.h * 0.78 };
    const st = box($("stage")) || box($("basket"));
    return st ? { x: st.x + st.w - S * 1.6, y: st.y - S * 0.22 } : { x: fx + S, y: fy + S };
  }
  function cardSpot(key, blocker) {
    const el = $(`card-${key}`), b = box(el);
    if (!b) return null;
    // CHIEF's card goes red on any drop (STAND DOWN): that is the summary, not the gate that dropped it
    const found = key === blocker || (key !== "chief" && el.classList.contains("blocked"));
    return { ...edge(b, true, 0.62, 0.12), found, perch: { el, top: true } };
  }
  let candle = null;              // app.js's getter: the newest candle's high in #chart px, as last drawn
  function candleSpot() {
    const c = typeof candle === "function" ? candle() : candle, b = c && box($("chart"));
    return b ? { x: b.x + c.x, y: b.y + c.y - S * 0.4 } : null;
  }
  function lineSpot() {           // the next close on the BUY LINE / SHORT LINE, when the board shows one
    const el = document.querySelector("#outlook .ol-rows .now") || document.querySelector("#outlook .ol-head");
    const b = box(el);              // on the box's top-right corner, clear of the heading's words and the level
    return b ? { x: b.x + b.w - S * 0.2, y: b.y - S * 0.22, perch: { el, top: true } } : null;
  }
  function textBox(t, re) {       // the first match of `re` in element t, as a page box (its first line, if it wraps)
    if (!t) return null;
    const walk = document.createTreeWalker(t, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const m = re.exec(n.data);
      if (!m) continue;
      const range = document.createRange();
      range.setStart(n, m.index); range.setEnd(n, m.index + m[0].length);
      const r = [...range.getClientRects()].find((q) => q.width > 2);
      if (r) return { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height };
    }
    return null;
  }
  function amountSpot(t, re) {    // just above the first match of `re` in element t
    const r = textBox(t, re);
    return r ? { x: r.x + r.w / 2, y: r.y - S * 0.45 } : null;
  }
  function words(el) {            // every line of text in el, as page boxes
    const out = [];
    if (!el) return out;
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT), range = document.createRange();
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) if (r.width) out.push({ l: r.left + scrollX, r: r.right + scrollX, t: r.top + scrollY, b: r.bottom + scrollY });
    }
    return out;
  }
  // would the sprite standing at p touch none of these words? (its drawn part: S/2 either side, S/2 above, 11/24 S
  // below; with `shake`, also while it twitches there: 2 px either side, 4 px up)
  function clear(p, ws, shake) {
    const sx = S / 2 + (shake ? 2 : 0), up = S / 2 + (shake ? 4 : 0);
    return !ws.some((q) => q.r > p.x - sx && q.l < p.x + sx && q.b > p.y - up && q.t < p.y + S * 11 / 24);
  }
  const priceSpot = () => amountSpot($("ticker"), /\$[\d,.]+/);    // the coin's price: the ticker's first $ amount
  // the basket's bank in its ticker. A phone wraps it after BANK: it then stands above that word, on the panel's
  // edge, not over the ticker's first line
  const BANK = /BANK -?\$[\d,.]+/;
  const bankSpot = () => amountSpot($("vt-ticker"), BANK);
  function bankText() {
    const t = $("vt-ticker"), m = t && BANK.exec(t.textContent);
    return m ? m[0] : null;
  }
  // the basket's rows, as renderBasket draws them: COIN, VS 50-DAY AVG, 10-DAY VOL, UNITS, HELD, UNREALISED, ORDER
  // (an order waiting for the next open is the cell marked .pend). app.js redraws them on every price, so a row is
  // looked up by its coin each time, never kept
  const AVG = 1;
  const coinOf = (row) => { const b = row.querySelector(".h-coin b"); return b ? b.textContent : ""; };
  const sleeves = () => [...document.querySelectorAll("#holdings .h-row:not(.h-head)")];
  // where it stands in a row's cell, at the cell's right-hand end: beside the cell's words if they leave room there,
  // or (under) just below them, reaching no further than the next row's top padding. Only where it touches no word
  // in the basket (all)
  function inCell(el, row, all, under, shake) {
    const b = box(el), rb = box(row);
    if (!b || !rb) return null;
    const ws = words(el), x = b.x + b.w - S * 0.62;
    const p = under ? { x, y: Math.max(b.y, ...ws.map((q) => q.b)) + S / 2 + (shake ? 5 : 1) }
      : ws.every((q) => q.r <= b.x + b.w - S * 1.2) ? { x, y: b.y + b.h / 2 } : null;
    return p && p.y + S * 11 / 24 <= rb.y + rb.h + 7 && clear(p, all, shake) ? p : null;
  }
  // a coin's row: at its order when one is waiting, else at its 50-day average, else at another of its cells (a
  // narrow table): beside the cell's words, or under them. A row with no room anywhere is skipped, never stood on.
  // An order waiting gets the twitch, or where that has no room, the same long look standing still
  function sleeveSpot(coin, twitch) {
    const row = sleeves().find((r) => coinOf(r) === coin);
    if (!row) return null;
    const pend = row.querySelector(".pend"), all = words($("basket"));
    const cells = [pend, ...[...row.children].slice(AVG).filter((c) => c !== pend)].filter(Boolean);
    const place = (shake) => {
      for (const c of cells) { const p = inCell(c, row, all, false, shake) || inCell(c, row, all, true, shake); if (p) return p; }
      return null;
    };
    return shaken(place, !!(twitch && pend));
  }
  function shaken(place, found) { // a spot with room for the twitch, else (found) one to stand still on as long
    const spot = place(found);
    if (spot) return { ...spot, found };
    const still = found && place(false);
    return still ? { ...still, dwell: 2600 } : null;
  }
  // a row that is scrolled away (a phone, at the top of the page): the coin's name in the VT tab's own label
  // ("BTC·ETH·SOL") stands in for it, with the row's own order waiting, if the sprite fits just under the name
  function nameSpot(coin) {
    const row = sleeves().find((r) => coinOf(r) === coin);
    const r = row && /^[A-Z0-9]+$/.test(coin) && textBox(document.querySelector(".coin.on .c-name i"), new RegExp(`\\b${coin}\\b`));
    if (!r) return null;
    const ws = words($("coins"));
    return shaken((shake) => {
      const p = { x: r.x + r.w / 2, y: r.y + r.h + S / 2 + (shake ? 5 : 1) };
      return clear(p, ws, shake) ? p : null;
    }, !!row.querySelector(".pend"));
  }
  const FILL = /\b(?:BOUGHT|SOLD|ADDED|TRIMMED) [\d.]+ ([A-Z]+)\b/;   // voltrend.py's fill lines name the coin
  const eventLine = (text) => [...document.querySelectorAll("#events li")].find((li) => li.textContent === text);
  function eventSpot(text) {
    const b = box(eventLine(text));
    return b ? { x: b.x + S * 0.35, y: b.y + S * 0.1 } : null;     // on the agent's name; the trade text stays clear
  }
  function tradeSpot(text) {      // a trade whose line is scrolled away also shows in the ticker's position, or the tab
    const m = basket() && FILL.exec(text || ""), row = m && sleeveSpot(m[1]);   // VT: first that coin's row
    if (row && seen(row.y, S / 2)) return row;
    const chip = tickerEl() && tickerEl().querySelector(".hl"), c = box(chip);
    if (c && seen(c.y, S / 2)) return { x: c.x + c.w / 2, y: c.y - S * 0.45, perch: { el: chip, top: true } };
    const tab = document.querySelector(".coin.on"), t = box(tab);
    if (t && seen(t.y, S / 2)) return { ...edge(t, true, 0.5), perch: { el: tab, top: true } };
    return null;
  }

  // ---- motion: a visit becomes a plan of small actions; each returns ms until it next needs a frame, -1 = done,
  // Infinity = nothing until something wakes it. A run to a fill gets every frame, a tour walk ~30 a second, a stroll ~16.
  const sp = { x: 0, y: 0, ox: 0, oy: 0, legs: 0, pose: 0, asleep: false };   // legs: ms per step while walking
  let silk = null;                // {x, y}: where the thread is tied while it hangs from one
  let perch = null;               // {el, top}: the edge it is sitting on
  let queue = [], visit = null, plan = [], act = null;
  let later = [];                 // tour stops and a trade that were scrolled out of view, for when you scroll there

  const legMs = (pace) => (pace >= 400 ? 60 : pace >= 150 ? 100 : 170);
  const gap = (pace) => (pace >= 400 ? 0 : pace >= 150 ? 36 : 60);   // a run: every frame; a tour walk: ~30 fps; a stroll: ~16
  function walk(x, y, ms, legs, arc, every) {
    let x0, y0, t0;
    return {
      travel: true,
      begin(now) { x0 = sp.x; y0 = sp.y; t0 = now; },
      run(now) {
        const k = Math.min(1, (now - t0) / ms), e = ease(k);
        sp.x = x0 + (x - x0) * e;
        sp.y = y0 + (y - y0) * e - Math.sin(Math.PI * k) * arc;
        sp.legs = arc ? 0 : legs;
        return k < 1 ? every : -1;
      },
    };
  }
  function lower(y, ms, every = 0) {   // tie a thread here and drop straight down
    let y0, t0;
    return {
      travel: true,
      begin(now) { y0 = sp.y; t0 = now; silk = { x: sp.x, y: sp.y - S * 0.1 }; },
      run(now) {
        const k = Math.min(1, (now - t0) / ms);
        sp.y = y0 + (y - y0) * ease(k); sp.legs = 0;
        return k < 1 ? every : -1;
      },
    };
  }
  function climb(ms) {            // back up the thread; the thread goes with it
    let y0, t0;
    return {
      begin(now) { y0 = sp.y; t0 = now; },
      run(now) {
        if (!silk) return -1;
        const k = Math.min(1, (now - t0) / ms);
        sp.x = silk.x; sp.y = y0 + (silk.y + S * 0.1 - y0) * ease(k); sp.legs = 170;
        if (k < 1) return 45;
        silk = null;
        return -1;
      },
    };
  }
  function reel(ms) {             // let go of the thread: it pulls in to the spider
    let a0, t0;
    return {
      begin(now) { a0 = silk ? silk.y : 0; t0 = now; },
      run(now) {
        if (!silk) return -1;
        const k = Math.min(1, (now - t0) / ms);
        silk.y = a0 + (sp.y - a0) * k;
        if (k < 1) return 0;
        silk = null;
        return -1;
      },
    };
  }
  // "read": still, a slow shuffle; "idle": a slower one; "found": a twitch first; "hang": sway on the thread;
  // "sleep": curled up, no frames at all until something wakes it
  function sit(ms, how) {
    let t0;
    return {
      begin(now) { t0 = now; },
      run(now) {
        const t = now - t0;
        sp.legs = 0;
        if (how === "sleep") { sp.ox = sp.oy = 0; sp.pose = 2; sp.asleep = true; return Infinity; }
        if (t >= ms) { sp.ox = sp.oy = 0; sp.pose = 0; return -1; }
        if (how === "found" && t < 680) {
          sp.ox = Math.floor(t / 45) % 2 ? 2 : -2; sp.oy = t % 340 < 120 ? -4 : 0; sp.pose = Math.floor(t / 85) % 2;
          return 0;
        }
        sp.oy = 0;
        if (how === "hang") { sp.ox = Math.sin(t / 520) * 2.5; return 66; }
        const every = how === "idle" ? 1800 : 700;
        sp.ox = 0; sp.pose = Math.floor(t / every) % 2;
        return Math.min(ms - t, every - (t % every));
      },
    };
  }
  const arrive = (p) => ({ begin() { perch = p; }, run() { return -1; } });

  function route(to, pace, hop) {
    const out = [];
    let from = { x: sp.x, y: sp.y };
    if (silk) {
      if (to.y < sp.y - 20) { out.push(climb(clamp((sp.y - silk.y) / 0.12, 250, 900))); from = { x: silk.x, y: silk.y + S * 0.1 }; }
      else out.push(reel(220));
    }
    const dx = to.x - from.x, dy = to.y - from.y, d = Math.hypot(dx, dy);
    if (d < 2) return out;
    if (hop) out.push(walk(to.x, to.y, clamp(d * 0.9, 300, 650), 0, Math.min(56, 14 + d * 0.12), 36));
    else if (dy > 90 && dy < 600 && Math.abs(dx) < dy * 0.4 && pace < 400) {   // mostly straight down: drop on silk
      if (Math.abs(dx) > 2) out.push(walk(to.x, from.y, clamp(Math.abs(dx) / pace * 1000, 150, 2000), legMs(pace), 0, gap(pace)));
      out.push(lower(to.y, clamp(dy / 0.35, 350, 1600), 36));
    } else out.push(walk(to.x, to.y, clamp(d / pace * 1000, 250, pace < 100 ? 9000 : 2400), legMs(pace), 0, gap(pace)));
    return out;
  }
  function keepIn(p) {
    if (dirty) refresh();
    return { ...p, x: clamp(p.x, fx + S / 2, fx + fw - S / 2), y: clamp(p.y, fy + S / 2, fy + fh - S / 2) };
  }

  function wander() {             // nothing new: a calm walk along the panel edges in view, then a long rest
    const top = scrollY, bottom = scrollY + innerHeight, r = Math.random();
    const here = perch && box(perch.el);
    if (here && !silk && r < 0.2) return { kind: "idle", drop: rand(45, 110) };
    if (r < 0.32 && innerWidth >= 980) {
      const h = home();
      if (seen(h.y, S / 2)) return { kind: "idle", at: () => h, dwell: rand(5000, 10000), pace: 70 };
    }
    if (here && r < 0.72) {
      const lo = here.x + S, hi = here.x + here.w - S, y = perch.top ? here.y - S * 0.22 : here.y + here.h + S * 0.22;
      if (hi - lo > 110 && seen(y, S / 2)) {
        const x = sp.x - lo > hi - sp.x ? rand(lo, Math.max(lo, sp.x - 50)) : rand(Math.min(hi, sp.x + 50), hi);
        const p = perch;
        return { kind: "idle", at: () => ({ x, y, perch: p }), dwell: rand(4000, 9000), pace: phone.matches ? 32 : 45 };
      }
    }
    const edges = [];
    const mine = Array.isArray(board.agents) ? board.agents : null;   // a card left over from another tab is not a perch
    document.querySelectorAll(".panel:not(.act-monitor), .card").forEach((el) => {   // not on the monitor's live numbers
      if (mine && el.classList.contains("card") && !mine.includes(el.id.replace(/^card-/, ""))) return;
      const b = box(el);
      if (!b || b.w < S * 3) return;
      if (b.y > top + S && b.y < bottom - S) edges.push({ el, b, top: true });
      if (b.y + b.h > top + S && b.y + b.h < bottom - S) edges.push({ el, b, top: false });
    });
    if (!edges.length) return { kind: "idle", at: () => ({ x: sp.x, y: sp.y }), dwell: 6000 };
    const e = edges[Math.floor(Math.random() * edges.length)];
    const spot = { ...edge(e.b, e.top, rand(0, 1)), perch: { el: e.el, top: e.top } };
    spot.x = clamp(spot.x, e.b.x + S, e.b.x + e.b.w - S);
    return { kind: "idle", at: () => spot, dwell: rand(4000, 9000), pace: phone.matches ? 60 : 80 };
  }

  function peekAt(target) {       // the screen's edge nearest a stop that is scrolled away, in line with it
    return {
      kind: "tour", peek: true, batch: target.batch, dwell: 1100,
      at() {
        const t = target.at();
        if (!t || seen(t.y, S / 2)) return null;
        return { x: t.x, y: t.y > scrollY + innerHeight / 2 ? scrollY + innerHeight - S * 0.7 : scrollY + S * 0.7 };
      },
    };
  }
  function takeLater() {          // the waiting stops that are on screen now, in their order; the rest keep waiting
    const now = [], still = [], t = Date.now();
    later.forEach((v) => {
      if (v.until && t > v.until) return;                  // a trade from long ago is not news any more
      const s = v.at();
      if (s) (seen(s.y, S / 2) ? now : still).push(v);
    });
    later = still;
    return now;
  }

  function go(spot) {
    const to = keepIn(spot);
    const pace = visit.pace || (phone.matches ? 190 : 260);
    if (!seen(sp.y, -S / 2)) {    // it is off screen: come in from the nearest edge (or just be there, if that is too)
      sp.x = to.x; silk = null;
      sp.y = !seen(to.y, -S / 2) ? to.y : sp.y < scrollY ? scrollY - S : scrollY + innerHeight + S;
    }
    plan = route(to, pace, visit.hop);
    const how = spot.found ? "found" : visit.kind === "idle" ? "idle" : "read";
    plan.push(arrive(spot.perch || null), sit(spot.found ? 2600 : spot.dwell || visit.dwell, how));
  }
  function sleep() {              // no board, or a stale one: home, curl up, and no frames until a fresh board
    queue = []; later = [];
    visit = { kind: "sleep", at: home };
    plan = [];
    const h = keepIn(home()), d = Math.hypot(h.x - sp.x, h.y - sp.y);
    if (d > 2) {
      if (d < 500 && seen(sp.y, -S / 2) && seen(h.y, -S / 2)) plan = route(h, 70);
      else { sp.x = h.x; sp.y = h.y; silk = null; }
    }
    plan.push(arrive(null), sit(0, "sleep"));
    return true;
  }

  function startNext() {
    if (!alive()) return sleep();
    let peekFor = null;
    for (let tries = 0; tries < 40; tries++) {
      if (!queue.length) {
        if (peekFor) { queue.push(peekAt(peekFor)); peekFor = null; continue; }
        const back = takeLater();
        queue.push(...(back.length ? back : [wander()]));
      }
      visit = queue.shift();
      if (visit.drop) {
        if (dirty) refresh();
        const room = Math.min(visit.drop, scrollY + innerHeight - sp.y - S, fy + fh - sp.y - S);
        if (room < 30 || silk) continue;
        const tk = box(tickerEl()), y = sp.y + room;           // never hang over the ticker's numbers
        if (tk && y + S / 2 > tk.y && y - S / 2 < tk.y + tk.h) continue;
        plan = [lower(y, room / 0.09, 45), sit(rand(1400, 2400), "hang"), climb(room / 0.07), sit(rand(3000, 6000), "idle")];
        return true;
      }
      let spot = visit.at();
      if (!spot) continue;
      if (!seen(spot.y, S / 2)) {
        if (visit.kind === "event") {       // its line is scrolled away: go to where the trade also shows
          later = later.filter((v) => v.kind !== "event");
          later.push(visit);
          spot = tradeSpot(visit.text);
          if (!spot) continue;
        } else if (visit.kind === "tour") {
          if (visit.peek) continue;
          later.push(visit);
          const alt = visit.alt && visit.alt();             // VT: the coin's name in its tab, while its row is away
          if (alt && seen(alt.y, S / 2)) {
            visit = { ...visit, at: visit.alt, alt: null, peek: true };   // like a peek, never kept for later
            spot = alt;
          } else {
            if (visit.batch && !visit.batch.peeked) { visit.batch.peeked = true; peekFor = visit; }   // one peek a tour
            continue;
          }
        }
      }
      if (peekFor) { queue.unshift(peekAt(peekFor), visit); peekFor = null; continue; }
      go(spot);
      return true;
    }
    visit = null;
    return false;
  }
  function cut() { visit = null; plan = []; act = null; sp.ox = sp.oy = 0; sp.legs = 0; sp.pose = 0; sp.asleep = false; }
  const keep = (v) => v && (v.kind === "tour" || v.kind === "event" || v.kind === "hop");

  function step(now) {
    for (let n = 0; n < 12; n++) {
      if (!act) {
        if (!plan.length && !startNext()) return 500;
        act = plan.shift();
        act.begin(now);
      }
      const wait = act.run(now);
      if (wait >= 0) return wait;
      act = null;
      if (!plan.length) visit = null;
    }
    return 0;
  }

  let lastX = NaN, lastY = NaN, silkOn = false, dim = false;
  function paint(now) {           // writes two transforms; reads nothing from the layout
    const x = Math.round(clamp(sp.x + sp.ox - fx, S / 2, fw - S / 2) - S / 2);
    const y = Math.round(clamp(sp.y + sp.oy - fy, S / 2, fh - S / 2) - S / 2);
    if (x !== lastX || y !== lastY) { body.style.transform = `translate3d(${x}px,${y}px,0)`; lastX = x; lastY = y; }
    pose(sp.legs ? Math.floor(now / sp.legs) % 2 : sp.pose);
    if (sp.asleep !== dim) { dim = sp.asleep; body.classList.toggle("asleep", dim); }
    if (silk) {
      const ax = silk.x - fx, ay = silk.y - fy, dx = x + S / 2 - ax, dy = y + S * 0.17 - ay, len = Math.hypot(dx, dy);
      if (dy > 2) {
        silkEl.style.transform = `translate3d(${ax}px,${ay}px,0) rotate(${-Math.atan2(dx, dy)}rad) scaleY(${len})`;
        if (!silkOn) { silkEl.style.display = "block"; silkOn = true; }
        return;
      }
    }
    if (silkOn) { silkEl.style.display = "none"; silkOn = false; }
  }

  // ---- the loop: requestAnimationFrame while something moves, a timer while it sits, nothing when hidden or
  // asleep ---------------------------------------------------------------------------------------------
  let on = store.get() !== "off", running = false, raf = 0, timer = 0;
  function frame(now) {
    raf = 0;
    if (!running) return;
    const wait = step(now);
    paint(now);
    if (wait === Infinity) return;                         // asleep: kick() wakes it
    if (wait < 24) raf = requestAnimationFrame(frame);
    else timer = setTimeout(() => { timer = 0; if (running) raf = requestAnimationFrame(frame); }, wait - 16);
  }
  function kick() {
    if (!running) return;
    clearTimeout(timer); timer = 0;
    if (!raf) raf = requestAnimationFrame(frame);
  }
  function start() {
    if (running) return;
    running = true;
    if (keep(visit)) queue.unshift(visit);                 // pick up where it was, with fresh timing
    cut();
    kick();
  }
  function stop() {
    running = false;
    cancelAnimationFrame(raf); clearTimeout(timer); raf = timer = 0;
  }
  function rest() {               // reduced motion: sit still at home (curled up while the desk is not running)
    touch();
    const h = keepIn(home()), up = alive();
    sp.x = h.x; sp.y = h.y; sp.ox = sp.oy = sp.legs = 0; sp.pose = up ? 0 : 2; sp.asleep = !up; silk = null;
    paint(0);
  }

  const btn = $("spider-toggle");
  function apply() {
    layer.hidden = !on;
    if (on) root.dataset.spider = "on"; else delete root.dataset.spider;    // app.js leaves the web's hub empty for it
    if (btn) btn.setAttribute("aria-pressed", String(on));
    label();
    if (!on || reduced.matches || document.hidden) stop(); else start();
    if (on && reduced.matches) rest();
  }

  // ---- what the desk is doing -----------------------------------------------------------------------
  let board = {}, lastTour = 0, tickCoin = null, lastPrice = null, lastHop = Date.now();   // no hop in the first 20 s
  let switched = false;           // a tab was clicked: the next board tours, whichever it is
  function alive() {              // a board has landed and it is recent enough to say the desk is running
    if (!board.updated) return false;
    return Date.now() - board.updated < (board.static === false ? STALE_LIVE : STALE_STATIC);
  }
  function label() {              // the footer button says what the spider is doing, and why when it is still
    if (!btn) return;
    const age = Date.now() - board.updated, h = Math.floor(age / 3600000), m = Math.floor(age / 60000) % 60;
    btn.textContent = !on ? "spider: off"
      : reduced.matches ? "spider: still (reduce motion is on)"
      : alive() ? "spider: on"
      : board.updated ? `spider: asleep, no new board for ${h ? `${h}h ` : ""}${m}m` : "spider: asleep, no board yet";
  }
  const seenLines = new Map();    // coin → the event lines already on its board
  function newFills(coin) {
    const lines = [...document.querySelectorAll("#events li")].map((li) => li.textContent);
    const known = seenLines.get(coin);
    if (!known) { seenLines.set(coin, new Set(lines)); return []; }   // first look at this coin: nothing is new
    const fresh = lines.filter((t) => !known.has(t) && TRADE.test(t));
    lines.forEach((t) => known.add(t));
    return fresh;                 // newest first, as the board lists them
  }
  function blockerOf(gates) {     // the gate holding the entry back: the first DROP or WAIT, as the desk counts it
    const gate = (gates || []).find((x) => x.verdict === "DROP" || x.verdict === "WAIT");
    return gate ? gate.owner : null;
  }
  // VT: each coin's row in the basket's order, then the bank. A row's state is what it shows: ABOVE / BELOW its
  // average and its ORDER word (HOLD, FLAT, PREVIEW, or the order waiting: BUY, ADD, TRIM, SELL ALL). A coin whose
  // state none of the others share, while two of them do share one, gets the longer look
  function basketTour() {
    const word = (el) => (el ? el.textContent.trim().split(" ")[0] : "");
    const rows = sleeves(), states = rows.map((r) => `${word(r.children[AVG] && r.children[AVG].querySelector("b"))} ${word(r.lastElementChild.querySelector("b"))}`);
    const many = (st) => states.filter((x) => x === st).length;
    const shared = states.some((st) => many(st) > 1);
    const batch = { peeked: false };
    const stops = rows.map((r, k) => {
      const coin = coinOf(r), odd = shared && many(states[k]) === 1;
      return { kind: "tour", batch, at: () => sleeveSpot(coin, true), alt: () => nameSpot(coin), dwell: odd ? rand(2000, 2500) : rand(800, 1300) };
    });
    stops.push({ kind: "tour", batch, at: bankSpot, dwell: 1400 });
    return stops;
  }
  function tour(d) {              // each card the first time it owns a gate, in the board's gate order
    if (basket()) return basketTour();
    const blocker = blockerOf(d.gates), keys = [];
    (d.gates || []).forEach((x) => { if (x.owner && !keys.includes(x.owner)) keys.push(x.owner); });
    if (!keys.length) keys.push(...ORDER);
    document.querySelectorAll(".card.blocked").forEach((el) => {
      const k = el.id.replace(/^card-/, "");
      if (k !== "chief" && !keys.includes(k)) keys.push(k);
    });
    const mine = Array.isArray(d.agents) ? keys.filter((k) => d.agents.includes(k)) : keys;   // this board's cards only
    const batch = { peeked: false };
    const stops = mine.map((key) => ({ kind: "tour", batch, at: () => cardSpot(key, blocker), dwell: rand(800, 1400) }));
    stops.push({ kind: "tour", batch, at: candleSpot, dwell: 1300 }, { kind: "tour", batch, at: lineSpot, dwell: 1400 });
    return stops;
  }
  function wake() { if (visit && visit.kind === "sleep") cut(); }
  let revealed = false;
  function reveal() {             // first show: at home, where the rendered page (not the empty skeleton) puts it
    if (revealed) return;
    revealed = true;
    touch();
    if (!visit || visit.kind === "sleep" || visit.kind === "idle") {
      cut(); silk = null;
      const h = keepIn(home()); sp.x = h.x; sp.y = h.y;
      paint(performance.now());
    }
    layer.style.visibility = "";
  }
  setTimeout(() => { reveal(); kick(); }, 2500);   // no board yet (the desk may be down): show it asleep at home

  addEventListener("desk:board", (e) => {
    const d = e.detail || {};
    touch();
    candle = d.candle || null;
    const fills = newFills(d.coin), now = Date.now();
    const fresh = switched || d.coin !== board.coin || d.scanned !== board.scanned || (d.updated !== board.updated && now - lastTour > RETOUR);
    board = d; switched = false;
    label();
    reveal();
    if (!on) return;
    if (reduced.matches) { rest(); return; }
    if (!alive()) { if (!visit || visit.kind !== "sleep") cut(); kick(); return; }
    wake();
    if (fresh) {
      lastTour = now;
      queue = queue.filter((v) => v.kind !== "tour" && v.kind !== "idle");
      later = later.filter((v) => v.kind !== "tour");
      if (!visit || visit.kind === "tour" || visit.kind === "idle") cut();
      queue.push(...tour(d));
    }
    if (fills.length) {
      queue = queue.filter((v) => v.kind !== "idle");
      later = later.filter((v) => v.kind !== "event");
      queue.unshift({ kind: "event", text: fills[0], at: () => eventSpot(fills[0]), dwell: 4000, pace: 520, until: now + 10 * 60000 });
      if (!visit || visit.kind !== "event") cut();
    }
    kick();
  });

  addEventListener("desk:tick", (e) => {
    const d = e.detail || {};
    touch();
    if (d.candle) candle = d.candle;
    const vt = basket(), price = vt ? bankText() : d.price;   // VT has no price: its bank, as the ticker shows it
    const moved = d.coin === tickCoin && lastPrice != null && price != null && price !== lastPrice;
    tickCoin = d.coin; lastPrice = price;
    label();
    if (!on) return;
    if (reduced.matches) { rest(); return; }
    if (!running) return;
    if (!alive()) { if (!visit || visit.kind !== "sleep") { cut(); kick(); } return; }   // the board went stale
    if (visit && visit.kind === "sleep") { cut(); kick(); return; }
    if (!moved || Date.now() - lastHop < HOP_EVERY) return;
    if ((visit && (visit.kind === "event" || visit.kind === "hop")) || (queue[0] && queue[0].kind === "event")) return;
    const p = vt ? bankSpot() : priceSpot();
    if (!p || !seen(p.y, S / 2)) return;
    lastHop = Date.now();
    const from = { x: sp.x, y: sp.y, perch };
    const back = visit && visit.kind === "tour" ? { ...visit, hop: true } : { kind: "idle", hop: true, at: () => from, dwell: rand(800, 1500) };
    cut();
    queue.unshift({ kind: "hop", hop: true, at: () => p, dwell: 700 }, back);
    kick();
  });

  // another tab: drop the old coin's plan; the tour of whichever board loads next follows as soon as it does (the
  // clicked tab's, or the shown one's again when another tab was clicked before that one loaded)
  document.addEventListener("click", (e) => {
    const tab = e.target instanceof Element && e.target.closest(".coin[data-coin]");
    if (!tab || tab.dataset.coin === board.coin) return;  // the tab already shown: nothing changes
    switched = true;
    queue = queue.filter((v) => v.kind !== "tour" && v.kind !== "event");
    later = [];
    if (visit && (visit.kind === "tour" || visit.kind === "event")) cut();
    kick();
  });

  // once the scrolling settles: visit the stops that were waiting for it, or come back into view from a rest
  let settle = 0;
  function resume() {
    settle = 0;
    if (!running || !alive()) return;
    if (visit && visit.kind !== "idle") {                  // a tour, hop or trade is on: it takes them up after,
      if (keep(visit) && !seen(sp.y, -S / 2)) { queue.unshift(visit); cut(); kick(); }   // re-planned if scrolled away
      return;
    }
    const back = takeLater();
    if (!back.length && seen(sp.y, -S / 2)) return;
    queue = queue.filter((v) => v.kind !== "idle");
    queue.unshift(...back);
    cut(); kick();
  }
  document.addEventListener("scroll", () => {
    touch();
    if (!running) return;
    clearTimeout(settle); settle = setTimeout(resume, 300);
  }, { passive: true, capture: true });

  let width = innerWidth;
  addEventListener("resize", () => {
    touch();
    if (!on) return;
    if (reduced.matches) { rest(); return; }
    if (innerWidth === width) return;                     // a phone's address bar coming and going
    width = innerWidth;
    if (keep(visit)) queue.unshift(visit);
    cut(); kick();
  });
  if (window.ResizeObserver) {   // the page moved under it (fonts, a new board): re-aim a walk, or move its bed
    new ResizeObserver(() => {
      touch();
      if (!running || !visit) return;
      if (visit.kind === "sleep" || (keep(visit) && act && act.travel)) { if (keep(visit)) queue.unshift(visit); cut(); kick(); }
    }).observe(frameEl);
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(touch);
  document.addEventListener("visibilitychange", apply);
  if (reduced.addEventListener) reduced.addEventListener("change", apply);
  if (phone.addEventListener) phone.addEventListener("change", touch);

  if (btn) {
    btn.hidden = false;
    btn.addEventListener("click", () => {
      on = !on;
      store.set(on ? "on" : "off");
      if (on) { queue = board.coin ? tour(board) : []; later = []; cut(); }
      apply();
      dispatchEvent(new Event("resize"));                 // app.js redraws the web with or without its own hub
    });
  }

  const h = keepIn(home());
  sp.x = h.x; sp.y = h.y;
  paint(0);
  apply();
})();
