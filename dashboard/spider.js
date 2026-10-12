// The walking crawler. A pixel spider that goes to whatever the desk just did, so you can watch the desk
// work through its checks. Every move comes from the board's real state:
//   a new board (a run landed, or another tab)   the agent cards in gate order (SCAN, VET, CROWD, SIZE, FILLS,
//                                                RISK), staying longest on the one holding the entry back, then
//                                                the newest candle, then the BUY / SHORT LINE
//   a live price tick (at most one per 20 s)     a quick hop to the price in the ticker, then back
//   a new fill (BOUGHT, SOLD, SHORTED, ...)      it runs to that event line and stays a few seconds
//   nothing new                                  a slow walk along the panel edges, now and then a drop on silk
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

  // The CRAWLER card's sprite (X = body, k = eye) and a second leg pose for walking.
  const POSES = [
    ["X..........X", ".X........X.", "..X.XXXX.X..", "...XXXXXX...", "XXXXkXXkXXXX", "...XXXXXX...",
     "..XXXXXXXX..", ".X.XXXXXX.X.", "X...XXXX...X", "...X....X...", "..X......X..", "............"],
    [".X........X.", ".X........X.", "..X.XXXX.X..", "X..XXXXXX..X", ".XXXkXXkXXX.", "...XXXXXX...",
     "X.XXXXXXXX.X", ".X.XXXXXX.X.", "....XXXX....", "..XX....XX..", ".X........X.", "............"],
  ];
  const TOUR = ["scan", "vet", "crowd", "size", "fills", "risk"];      // the order an entry meets the agents
  const TRADE = /\b(BOUGHT|SOLD|SHORTED|COVERED|ADDED|TRIMMED|STOP|HALT)\b/;
  const HOP_EVERY = 20000;              // at most one hop to the ticker per 20 s
  const RETOUR = 5 * 60000;             // same coin, same bar: a fresh `updated` tours again after 5 min (live server)

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
  const inView = (y) => y > scrollY && y < scrollY + innerHeight;
  // where it stands on an element's top (or bottom) edge, a fraction f of the way along; `inset` > 0 is inside
  const edge = (b, top, f, inset = -0.22) => ({ x: b.x + b.w * f, y: top ? b.y + S * inset : b.y + b.h - S * inset });

  // ---- the spots it visits ------------------------------------------------------------------------
  function home() {               // desktop: the hub app.js spins the chart web from. narrower: the stage's top edge
    const wrap = box(document.querySelector(".chart-wrap"));
    if (wrap && innerWidth >= 980) return { x: wrap.x + wrap.w * 0.56, y: wrap.y + wrap.h * 0.78 };
    const st = box($("stage"));
    return st ? { x: st.x + st.w - S * 1.6, y: st.y - S * 0.22 } : { x: fx + S, y: fy + S };
  }
  function cardSpot(key, blocker) {
    const el = $(`card-${key}`), b = box(el);
    if (!b) return null;
    return { ...edge(b, true, 0.62, 0.12), found: key === blocker || el.classList.contains("blocked"), perch: { el, top: true } };
  }
  let candle = null;              // the newest candle's high, in #chart's own pixels (from app.js)
  function candleSpot() {
    const b = candle && box($("chart"));
    return b ? { x: b.x + candle.x, y: b.y + candle.y - S * 0.4 } : null;
  }
  function lineSpot() {           // the next close on the BUY LINE / SHORT LINE, when the board shows one
    const el = document.querySelector("#outlook .ol-rows .now") || document.querySelector("#outlook .ol-head");
    const b = box(el);
    return b ? { ...edge(b, true, 0.5), perch: { el, top: true } } : null;
  }
  function priceSpot() {          // the coin's price in the ticker (the first $ amount in it)
    const t = $("ticker");
    if (!t) return null;
    const walk = document.createTreeWalker(t, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const m = /\$[\d,.]+/.exec(n.data);
      if (!m) continue;
      const range = document.createRange();
      range.setStart(n, m.index); range.setEnd(n, m.index + m[0].length);
      const r = range.getBoundingClientRect();
      if (r.width) return { x: r.left + scrollX + r.width / 2, y: r.top + scrollY - S * 0.22 };
    }
    return null;
  }
  const eventLine = (text) => [...document.querySelectorAll("#events li")].find((li) => li.textContent === text);
  function eventSpot(text) {
    const b = box(eventLine(text));
    return b ? { x: b.x + S * 0.35, y: b.y + S * 0.1 } : null;     // on the agent's name; the trade text stays clear
  }

  // ---- motion: a visit becomes a plan of small actions; each returns ms until it next needs a frame, -1 = done
  const sp = { x: 0, y: 0, ox: 0, oy: 0, legs: 0, pose: 0 };     // legs: ms per step while walking, 0 when still
  let silk = null;                // {x, y}: where the thread is tied while it hangs from one
  let perch = null;               // {el, top}: the edge it is sitting on
  let queue = [], visit = null, plan = [], act = null;

  const legMs = (pace) => (pace >= 400 ? 60 : pace >= 150 ? 100 : 170);
  function walk(x, y, ms, legs, arc = 0) {
    let x0, y0, t0;
    return {
      travel: true,
      begin(now) { x0 = sp.x; y0 = sp.y; t0 = now; },
      run(now) {
        const k = Math.min(1, (now - t0) / ms), e = ease(k);
        sp.x = x0 + (x - x0) * e;
        sp.y = y0 + (y - y0) * e - Math.sin(Math.PI * k) * arc;
        sp.legs = arc ? 0 : legs;
        return k < 1 ? (legs >= 170 ? 30 : 0) : -1;           // a slow walk is smooth enough at ~30 frames a second
      },
    };
  }
  function lower(y, ms) {         // tie a thread here and drop straight down
    let y0, t0;
    return {
      travel: true,
      begin(now) { y0 = sp.y; t0 = now; silk = { x: sp.x, y: sp.y - S * 0.1 }; },
      run(now) {
        const k = Math.min(1, (now - t0) / ms);
        sp.y = y0 + (y - y0) * ease(k); sp.legs = 0;
        return k < 1 ? 0 : -1;
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
        if (k < 1) return 0;
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
  function sit(ms, how) {         // "read": still, a slow shuffle; "found": a twitch first; "hang": sway on the thread
    let t0;
    return {
      begin(now) { t0 = now; },
      run(now) {
        const t = now - t0;
        sp.legs = 0;
        if (t >= ms) { sp.ox = sp.oy = 0; sp.pose = 0; return -1; }
        if (how === "found" && t < 680) {
          sp.ox = Math.floor(t / 45) % 2 ? 2 : -2; sp.oy = t % 340 < 120 ? -4 : 0; sp.pose = Math.floor(t / 85) % 2;
          return 0;
        }
        sp.oy = 0;
        if (how === "hang") { sp.ox = Math.sin(t / 520) * 2.5; return 0; }
        sp.ox = 0; sp.pose = Math.floor(t / 700) % 2;
        return Math.min(ms - t, 700 - (t % 700));
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
    if (hop) out.push(walk(to.x, to.y, clamp(d * 0.9, 300, 650), 0, Math.min(56, 14 + d * 0.12)));
    else if (dy > 90 && dy < 600 && Math.abs(dx) < dy * 0.4 && pace < 400) {   // mostly straight down: drop on silk
      if (Math.abs(dx) > 2) out.push(walk(to.x, from.y, clamp(Math.abs(dx) / pace * 1000, 150, 2000), legMs(pace)));
      out.push(lower(to.y, clamp(dy / 0.35, 350, 1600)));
    } else out.push(walk(to.x, to.y, clamp(d / pace * 1000, 250, pace < 100 ? 9000 : 2400), legMs(pace)));
    return out;
  }
  function keepIn(p) {
    if (dirty) refresh();
    return { ...p, x: clamp(p.x, fx + S / 2, fx + fw - S / 2), y: clamp(p.y, fy + S / 2, fy + fh - S / 2) };
  }

  function wander() {             // nothing new: a calm walk along the panel edges in view
    const top = scrollY, bottom = scrollY + innerHeight, r = Math.random();
    const here = perch && box(perch.el);
    if (here && !silk && r < 0.2) return { kind: "idle", drop: rand(45, 110) };
    if (r < 0.32 && innerWidth >= 980) {
      const h = home();
      if (inView(h.y)) return { kind: "idle", at: () => h, dwell: rand(2500, 5000), pace: 70 };
    }
    if (here && r < 0.72) {
      const lo = here.x + S, hi = here.x + here.w - S, y = perch.top ? here.y - S * 0.22 : here.y + here.h + S * 0.22;
      if (hi - lo > 110 && inView(y)) {
        const x = sp.x - lo > hi - sp.x ? rand(lo, Math.max(lo, sp.x - 50)) : rand(Math.min(hi, sp.x + 50), hi);
        const p = perch;
        return { kind: "idle", at: () => ({ x, y, perch: p }), dwell: rand(900, 3200), pace: phone.matches ? 32 : 45 };
      }
    }
    const edges = [];
    document.querySelectorAll(".panel, .card").forEach((el) => {
      const b = box(el);
      if (!b || b.w < S * 3) return;
      if (b.y > top + S && b.y < bottom - S) edges.push({ el, b, top: true });
      if (b.y + b.h > top + S && b.y + b.h < bottom - S) edges.push({ el, b, top: false });
    });
    if (!edges.length) return { kind: "idle", at: () => ({ x: sp.x, y: sp.y }), dwell: 3000 };
    const e = edges[Math.floor(Math.random() * edges.length)];
    const spot = { ...edge(e.b, e.top, rand(0, 1)), perch: { el: e.el, top: e.top } };
    spot.x = clamp(spot.x, e.b.x + S, e.b.x + e.b.w - S);
    return { kind: "idle", at: () => spot, dwell: rand(1200, 3600), pace: phone.matches ? 60 : 80 };
  }

  function startNext() {
    for (let tries = 0; tries < 12; tries++) {
      if (!queue.length) queue.push(wander());
      visit = queue.shift();
      if (visit.drop) {
        if (dirty) refresh();
        const room = Math.min(visit.drop, scrollY + innerHeight - sp.y - S, fy + fh - sp.y - S);
        if (room < 30 || silk) continue;
        plan = [lower(sp.y + room, room / 0.09), sit(rand(1400, 2400), "hang"), climb(room / 0.07), sit(rand(600, 1200), "read")];
        return true;
      }
      const spot = visit.at();
      if (!spot) continue;
      const to = keepIn(spot);
      let pace = visit.pace || (phone.matches ? 190 : 260);
      if (visit.kind === "idle" && !inView(sp.y)) pace = Math.max(pace, 240);     // out of sight: come back quicker
      plan = route(to, pace, visit.hop);
      plan.push(arrive(spot.perch || null), sit(spot.found ? 2600 : visit.dwell, spot.found ? "found" : "read"));
      return true;
    }
    visit = null;
    return false;
  }
  function cut() { visit = null; plan = []; act = null; sp.ox = sp.oy = 0; sp.legs = 0; }

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

  let lastX = NaN, lastY = NaN, silkOn = false;
  function paint(now) {           // writes two transforms; reads nothing from the layout
    const x = Math.round(clamp(sp.x + sp.ox - fx, S / 2, fw - S / 2) - S / 2);
    const y = Math.round(clamp(sp.y + sp.oy - fy, S / 2, fh - S / 2) - S / 2);
    if (x !== lastX || y !== lastY) { body.style.transform = `translate3d(${x}px,${y}px,0)`; lastX = x; lastY = y; }
    pose(sp.legs ? Math.floor(now / sp.legs) % 2 : sp.pose);
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

  // ---- the loop: requestAnimationFrame while something moves, a timer while it sits, nothing when hidden --
  let on = store.get() !== "off", running = false, raf = 0, timer = 0;
  function frame(now) {
    raf = 0;
    if (!running) return;
    const wait = step(now);
    paint(now);
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
    if (visit && visit.kind !== "idle") queue.unshift(visit);    // pick up where it was, with fresh timing
    cut();
    kick();
  }
  function stop() {
    running = false;
    cancelAnimationFrame(raf); clearTimeout(timer); raf = timer = 0;
  }
  function rest() {               // reduced motion: sit still at home
    touch();
    const h = keepIn(home());
    sp.x = h.x; sp.y = h.y; sp.ox = sp.oy = sp.legs = sp.pose = 0; silk = null;
    paint(0);
  }

  const btn = $("spider-toggle");
  function apply() {
    layer.hidden = !on;
    if (on) root.dataset.spider = "on"; else delete root.dataset.spider;    // app.js leaves the web's hub empty for it
    if (btn) { btn.textContent = `spider: ${on ? "on" : "off"}`; btn.setAttribute("aria-pressed", String(on)); }
    if (!on || reduced.matches || document.hidden) stop(); else start();
    if (on && reduced.matches) rest();
  }

  // ---- what the desk is doing -----------------------------------------------------------------------
  let board = {}, lastTour = 0, tickCoin = null, lastPrice = null, lastHop = Date.now();   // no hop in the first 20 s
  const seen = new Map();         // coin → the event lines already on its board
  function newFills(coin) {
    const lines = [...document.querySelectorAll("#events li")].map((li) => li.textContent);
    const known = seen.get(coin);
    if (!known) { seen.set(coin, new Set(lines)); return []; }   // first look at this coin: nothing is new
    const fresh = lines.filter((t) => !known.has(t) && TRADE.test(t));
    lines.forEach((t) => known.add(t));
    return fresh;                 // newest first, as the board lists them
  }
  function blockerOf(gates) {     // the gate holding the entry back: the first DROP or WAIT, as the desk counts it
    const gate = (gates || []).find((x) => x.verdict === "DROP" || x.verdict === "WAIT");
    return gate ? gate.owner : null;
  }
  function tour(blocker) {
    const stops = TOUR.map((key) => ({ kind: "tour", at: () => cardSpot(key, blocker), dwell: rand(800, 1400) }));
    stops.push({ kind: "tour", at: candleSpot, dwell: 1300 }, { kind: "tour", at: lineSpot, dwell: 1400 });
    return stops;
  }

  addEventListener("desk:board", (e) => {
    const d = e.detail || {};
    touch();
    candle = d.candle || null;
    const fills = newFills(d.coin), now = Date.now();
    const fresh = d.coin !== board.coin || d.scanned !== board.scanned || (d.updated !== board.updated && now - lastTour > RETOUR);
    board = d;
    if (!on) return;
    if (reduced.matches) { rest(); return; }
    if (fresh) {
      lastTour = now;
      queue = queue.filter((v) => v.kind !== "tour" && v.kind !== "idle");
      if (!visit || visit.kind === "tour" || visit.kind === "idle") cut();
      queue.push(...tour(blockerOf(d.gates)));
    }
    if (fills.length) {
      queue = queue.filter((v) => v.kind !== "idle");
      queue.unshift({ kind: "event", at: () => eventSpot(fills[0]), dwell: 4000, pace: 520 });
      if (!visit || visit.kind !== "event") cut();
    }
    kick();
  });

  addEventListener("desk:tick", (e) => {
    const d = e.detail || {};
    touch();
    if (d.candle) candle = d.candle;
    const moved = d.coin === tickCoin && lastPrice != null && d.price != null && d.price !== lastPrice;
    tickCoin = d.coin; lastPrice = d.price;
    if (!on) return;
    if (reduced.matches) { rest(); return; }
    if (!moved || !running || Date.now() - lastHop < HOP_EVERY) return;
    if ((visit && (visit.kind === "event" || visit.kind === "hop")) || (queue[0] && queue[0].kind === "event")) return;
    const p = priceSpot();
    if (!p || !inView(p.y)) return;
    lastHop = Date.now();
    const from = { x: sp.x, y: sp.y, perch };
    const back = visit && visit.kind === "tour" ? { ...visit, hop: true } : { kind: "idle", hop: true, at: () => from, dwell: rand(800, 1500) };
    cut();
    queue.unshift({ kind: "hop", hop: true, at: () => p, dwell: 700 }, back);
    kick();
  });

  // another tab: drop the old coin's plan; the new board's tour follows as soon as it loads
  document.addEventListener("click", (e) => {
    if (!(e.target instanceof Element) || !e.target.closest(".coin[data-coin]")) return;
    queue = queue.filter((v) => v.kind !== "tour" && v.kind !== "event");
    if (visit && (visit.kind === "tour" || visit.kind === "event")) cut();
    kick();
  });

  let width = innerWidth;
  addEventListener("resize", () => {
    touch();
    if (!on) return;
    if (reduced.matches) { rest(); return; }
    if (innerWidth === width) return;                     // a phone's address bar coming and going
    width = innerWidth;
    if (visit && visit.kind !== "idle") queue.unshift(visit);
    cut(); kick();
  });
  document.addEventListener("scroll", touch, { passive: true, capture: true });
  if (window.ResizeObserver) {   // the page moved under it (fonts, a new board): re-aim a walk that is under way
    new ResizeObserver(() => {
      touch();
      if (running && visit && visit.kind !== "idle" && act && act.travel) { queue.unshift(visit); cut(); kick(); }
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
      if (on) { queue = board.coin ? tour(blockerOf(board.gates)) : []; cut(); }
      apply();
      dispatchEvent(new Event("resize"));                 // app.js redraws the web with or without its own hub
    });
  }

  const h = keepIn(home());
  sp.x = h.x; sp.y = h.y;
  paint(0);
  apply();
})();
