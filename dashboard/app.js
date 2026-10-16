// Perp Desk board. Polls the desk's state (live server or static boards/*.json), pulls live prices straight
// from Hyperliquid every few seconds (BTC on Coinbase: from Coinbase's own feed while it's answering), and draws it
// all. No build step, no libraries.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const COLORS = {
    chief: "--white", scan: "--green", vet: "--amber", crowd: "--blue",
    size: "--lime", fills: "--ice", risk: "--orange", crawler: "--cyan",
  };

  // ---- pixel sprites (X = body, k = eye) --------------------------------
  const SPRITES = {
    chief: ["...XXXXXX...", ".XXXXXXXXXX.", "XXXXXXXXXXXX", "XXkkXXXXkkXX", "XXkkXXXXkkXX", "XXXXXXXXXXXX",
            "XXXXXXXXXXXX", "XXXXXXXXXXXX", "XXXXXXXXXXXX", "XXXXXXXXXXXX", "XX.XXX.XXX.X", "X...X...X..."],
    scan:  ["............", "....XXXX....", "..XXXXXXXX..", ".XXXXXXXXXX.", ".XkkXXXXkkX.", "XXkkXXXXkkXX",
            "XXXXXXXXXXXX", "XXXXXXXXXXXX", "XXXXXXXXXXXX", ".XXXXXXXXXX.", "..XX.XX.XX..", "............"],
    vet:   ["............", "..X......X..", "...X....X...", "..XXXXXXXX..", ".XXkXXXXkXX.", "XXXXXXXXXXXX",
            "X.XXXXXXXX.X", "X.X......X.X", "...XX..XX...", "............", "............", "............"],
    crowd: ["............", "...XXXXXX...", ".XXXXXXXXXX.", ".XXXXXXXXXX.", "XXXkkXXkkXXX", "XXXkkXXkkXXX",
            "XXXXXXXXXXXX", "XXXXXXXXXXXX", ".XXXXXXXXXX.", "..XXXXXXXX..", "............", "............"],
    size:  [".....XX.....", ".....XX.....", "....XXXX....", "....XXXX....", "...XXXXXX...", "...XkXXkX...",
            "..XXXXXXXX..", "..XXXXXXXX..", ".XXXXXXXXXX.", ".XXXXXXXXXX.", "XXXXXXXXXXXX", "X.X.X..X.X.X"],
    fills: ["............", "..X......X..", "...X....X...", "..XXXXXXXX..", ".XX.XXXX.XX.", "XXXXXXXXXXXX",
            "X.XXXXXXXX.X", "X.X......X.X", "...XX..XX...", "............", "............", "............"],
    risk:  ["............", "....XXXX....", "..XXXXXXXX..", ".XXXXXXXXXX.", ".XXkXXXXkXX.", "XXXkXXXXkXXX",
            "XXXXXXXXXXXX", "XXXXXXXXXXXX", ".XXXXXXXXXX.", "..XXXXXXXX..", "....XXXX....", "............"],
    crawler: ["X..........X", ".X........X.", "..X.XXXX.X..", "...XXXXXX...", "XXXXkXXkXXXX", "...XXXXXX...",
              "..XXXXXXXX..", ".X.XXXXXX.X.", "X...XXXX...X", "...X....X...", "..X......X..", "............"],
  };

  function sprite(key, px = 3) {
    const map = SPRITES[key];
    const c = document.createElement("canvas");
    c.width = map[0].length * px; c.height = map.length * px;
    c.className = "sprite";
    const g = c.getContext("2d");
    const skin = key === "crawler" ? css("--spider-body") : "";   // theme hook: the CRAWLER card in the walking spider's skin
    const body = skin || css(COLORS[key]);
    const ink = (skin && css("--spider-eye")) || css("--ink") || "#070b10";
    map.forEach((row, y) => [...row].forEach((ch, x) => {
      if (ch === ".") return;
      g.fillStyle = ch === "k" ? ink : body;
      g.fillRect(x * px, y * px, px, px);
    }));
    return c;
  }

  // ---- formatting ---------------------------------------------------------
  // number formats made once, not at every call (a price can redraw the page once a second); the same text as
  // toLocaleString("en-US", ...) gives
  const NF = new Map(), nf = (dp) => NF.get(dp) || NF.set(dp, new Intl.NumberFormat("en-US", dp == null ? {} : { minimumFractionDigits: dp, maximumFractionDigits: dp })).get(dp);
  const usd = (x, dp = 2) => x == null || isNaN(x) ? "—" : (x < 0 ? "-$" : "$") + nf(dp).format(Math.abs(x));
  const usdShort = (x) => x == null ? "—" : Math.abs(x) >= 1e9 ? `$${(x / 1e9).toFixed(2)}B` : Math.abs(x) >= 1e6 ? `$${(x / 1e6).toFixed(2)}M` : Math.abs(x) >= 1e3 ? `$${(x / 1e3).toFixed(1)}K` : usd(x, 2);
  const px = (x) => x == null || isNaN(x) ? "—" : usd(x, Math.abs(x) >= 1000 ? 0 : 2);
  const pxAxis = (x) => Math.abs(x) >= 1000 ? nf(null).format(Math.round(x)) : Number(x).toFixed(2);
  const pct = (x, dp = 2) => x == null || isNaN(x) ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(dp)}%`;
  const num = (x, dp = 2) => x == null || isNaN(x) ? "—" : nf(dp).format(Number(x));
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const DAY_MS = 86400000;

  // ---- Mountain Time: every time the page shows ----------------------------------------------------------------
  // The owner reads times in Mountain Time, whatever zone the phone itself is in: America/Denver's own rules from the
  // browser's time zone data (MDT in summer, MST from the first Sunday of November: never a fixed offset), labelled
  // MT. Stored times stay epoch ms; the desk's day still turns at 00:00 UTC (6:00 pm MT in summer, 5:00 pm in winter).
  // A browser without the zone (none current) shows UTC, labelled UTC, never a wrong time called MT. activity.js uses
  // the same helper (window.deskMT), so the two never disagree.
  const deskMT = (() => {
    const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let f = null;
    try {
      f = new Intl.DateTimeFormat("en-US", { timeZone: "America/Denver", year: "numeric", month: "numeric", day: "numeric",
        weekday: "short", hour: "numeric", minute: "numeric", second: "numeric", hourCycle: "h23" });
    } catch { f = null; }
    const memo = new Map();   // a second's parts, made once: the chart's axis asks for the same candles every redraw
    const parts = (ms) => {
      const k = Math.floor(ms / 1000);
      let p = memo.get(k);
      if (p) return p;
      if (f) {
        const o = {};
        for (const x of f.formatToParts(new Date(k * 1000))) o[x.type] = x.value;
        p = { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour % 24, mi: +o.minute, s: +o.second, wd: o.weekday };
      } else {
        const d = new Date(k * 1000);
        p = { y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(),
          s: d.getUTCSeconds(), wd: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()] };
      }
      if (memo.size > 5000) memo.clear();
      memo.set(k, p);
      return p;
    };
    const two = (n) => String(n).padStart(2, "0");
    // "6:04 pm", "6:04:09 pm" (sec), "6:04 PM" (up)
    const clock = (ms, o = {}) => {
      const p = parts(ms), t = `${p.h % 12 || 12}:${two(p.mi)}${o.sec ? ":" + two(p.s) : ""} ${p.h < 12 ? "am" : "pm"}`;
      return o.up ? t.toUpperCase() : t;
    };
    const day = (ms) => { const p = parts(ms); return `${MON[p.mo - 1]} ${p.d}`; };              // "Oct 15"
    const key = (ms) => { const p = parts(ms); return p.y * 10000 + p.mo * 100 + p.d; };         // the MT date, as a number
    return {
      zone: f ? "MT" : "UTC", parts, clock, day, key,
      md: (ms) => { const p = parts(ms); return `${two(p.mo)}-${two(p.d)}`; },                    // "10-15"
      ymd: (ms) => { const p = parts(ms); return `${p.y}-${two(p.mo)}-${two(p.d)}`; },            // "2026-10-15"
      wd: (ms) => parts(ms).wd,                                                                 // "Fri"
      when: (ms) => `${day(ms)}, ${clock(ms)}`,                                                 // "Oct 15, 6:04 pm"
      // from a to b: "3:00–4:00 pm", "11:00 pm–1:00 am", a day apart "Sat 8:00 pm–Sun 2:00 am"; days: the first day too
      span: (a, b, days = false) => {
        const pa = parts(a), pb = parts(b), same = key(a) === key(b), mer = (pa.h < 12) === (pb.h < 12);
        const head = same && mer ? clock(a).slice(0, -3) : clock(a);
        return `${days || !same ? pa.wd + " " : ""}${head}–${same ? "" : pb.wd + " "}${clock(b)}`;
      },
      // the start of ms's Mountain Time day (its midnight), as epoch ms
      midnight: (ms) => { const p = parts(ms); return Math.floor(ms / 1000) * 1000 - ((p.h * 60 + p.mi) * 60 + p.s) * 1000; },
      nextUtcDay: (ms) => Math.floor(ms / DAY_MS + 1) * DAY_MS,   // the desk's day turns: VT's close, the daily loss lock
    };
  })();
  window.deskMT = deskMT;
  const MT = deskMT, TZ = deskMT.zone;
  const hhmm = (ms) => MT.clock(ms);
  // theme hook: a signed amount's classes. ok / bad exactly as before (style.css colors them: below zero is bad), plus
  // its direction for a theme: up, down, or flat when it is zero at the shown `dp` decimals (neither a gain nor a loss)
  const tone = (x, dp = 2) => `${x < 0 ? "bad" : "ok"}${x == null || isNaN(x) ? "" : Math.abs(x) * 10 ** dp < 0.5 ? " flat" : x < 0 ? " down" : " up"}`;
  const closedTag = (text) => `<span class="venue-closed">${text.replace(/ · $/, "")}</span> · `;   // theme hook: CLOSED as a chip

  // ---- canvas helpers -------------------------------------------------------
  function fit(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    }
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    return { g, w, h };
  }

  function rgba(name, a) {
    const raw = (css(name) || "#3ee08f").replace("#", "");
    const hex = raw.length === 3 ? raw.split("").map((c) => c + c).join("") : raw;
    const n = parseInt(hex, 16);
    if (!Number.isFinite(n)) return `rgba(62,224,143,${a})`;
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  function line(g, pts, color, width = 1.5, dash = []) {
    g.save(); g.strokeStyle = color; g.lineWidth = width; g.setLineDash(dash); g.beginPath();
    let started = false;
    for (const [x, y] of pts) {
      if (y == null || isNaN(y)) { started = false; continue; }
      if (!started) { g.moveTo(x, y); started = true; } else g.lineTo(x, y);
    }
    g.stroke(); g.restore();
  }

  // ---- price chart ------------------------------------------------------------
  let lastGeom = null;
  function drawChart(chart) {
    const canvas = $("chart");
    const { g, w, h } = fit(canvas);
    const cs = chart.candles;
    if (!cs.length) return;
    const padT = 8, padB = 18;
    const vals = [];
    cs.forEach((c) => vals.push(c[2], c[3]));
    [chart.ema_fast, chart.ema_slow, chart.don_hi].forEach((s) => s.forEach((v) => v != null && vals.push(v)));
    if (chart.stop) vals.push(chart.stop);
    if (chart.live) vals.push(chart.live);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
    const lastC = cs[cs.length - 1][4], padR = rightPad(g, chart, lastC, lo, hi);
    // BTC's provisional newest candle(s) (formingBars, drawn dashed): each gets a slot at least 8 px wide, so its dashes
    // read on a phone (the others' slots are about 2 px there); the rest share what's left
    const prov = new Set(chart.prov || []);
    let np = 0;
    while (np < cs.length - 1 && prov.has(cs.length - 1 - np)) np++;
    const ps = np ? Math.max((w - padR) / cs.length, 8) : 0, n0 = cs.length - np;
    const cw = (w - padR - np * ps) / n0;
    const X = (k) => k < n0 ? k * cw + cw / 2 : n0 * cw + (k - n0) * ps + ps / 2;
    const slot = (k) => k < n0 ? cw : ps;
    const Y = (p) => padT + (hi - p) / (hi - lo) * (h - padT - padB);
    const tsIndex = new Map(cs.map((c, k) => [c[0], k]));
    // BTC on Coinbase: its live price's tag at the right edge goes at its price, else at the nearest place clear of the
    // close's and the stop's tags and on the same side of each as its price (the tags read in order); none if there's
    // no such place, only its line. An axis label under it isn't drawn
    let liveY = null;
    if (chart.live && chart.live !== cs[cs.length - 1][4]) {   // the newest candle carried on to it: its close tag says it
      const y = Y(chart.live), top = padT + 8, bot = h - padB - 8, taken = [Y(cs[cs.length - 1][4]), chart.stop ? Y(chart.stop) : null].filter((o) => o != null);
      const ok = (t) => t >= top && t <= bot && taken.every((o) => Math.abs(t - o) >= 17 && (y === o || (t - o) * (y - o) > 0));
      liveY = [y, top, bot, ...taken.flatMap((o) => [o - 17, o + 17])].filter(ok).sort((a, b) => Math.abs(a - y) - Math.abs(b - y))[0] ?? null;
    }

    g.font = "10px JetBrains Mono, monospace";
    for (let k = 0; k <= 4; k++) {
      const p = lo + (hi - lo) * k / 4, y = Y(p);
      line(g, [[0, y], [w - padR, y]], css("--line"), 1);
      if (liveY != null && Math.abs(y - liveY) < 13) continue;
      g.fillStyle = css("--dim"); g.fillText(pxAxis(p), w - padR + 6, y + 3);
    }
    // the x axis in Mountain Time days: a tick where each day starts (its midnight, inside the candle it falls in: 5 am
    // MT is a 2h candle's open in winter, not in summer), its date under as many as fit 44 px apart, and "MT" under
    // the price axis to say whose days they are
    g.fillStyle = css("--dim");
    let lastDate = -1e9;
    for (let k = 1; k < cs.length; k++) {
      if (MT.key(cs[k][0]) === MT.key(cs[k - 1][0])) continue;
      const mid = MT.midnight(cs[k][0]), x = X(k) - slot(k) / 2 - Math.max(0, Math.min(1, (cs[k][0] - mid) / chart.bar_ms)) * slot(k - 1);
      line(g, [[x, h - padB], [x, h - padB + 4]], css("--dim"), 1);
      if (x - lastDate >= 44 && x + 32 <= w - padR) { g.fillText(MT.md(cs[k][0]), x + 2, h - 4); lastDate = x; }
    }
    g.fillText(TZ, w - padR + 6, h - 4);

    line(g, chart.don_hi.map((v, k) => [X(k), v == null ? null : Y(v)]), css("--channel") || rgba("--muted", .55), 1, [3, 4]);
    line(g, chart.don_lo.map((v, k) => [X(k), v == null ? null : Y(v)]), css("--channel") || rgba("--muted", .35), 1, [3, 4]);
    line(g, chart.ema_slow.map((v, k) => [X(k), v == null ? null : Y(v)]), css("--blue"), 1.6);
    line(g, chart.ema_fast.map((v, k) => [X(k), v == null ? null : Y(v)]), css("--cyan"), 1.6);

    // theme hook (--candle-up: hollow): up candles hollow and down ones filled, so direction isn't color alone. A body
    // under 3 px (a phone) is drawn hollow as wide as its slot allows, outlined in one device pixel
    const bw = Math.max(1, cw * 0.62), hollow = css("--candle-up") === "hollow";
    const hw = bw >= 3 ? bw : Math.max(bw, cw * 0.92), hl = bw >= 3 ? 1 : 1 / (window.devicePixelRatio || 1);
    cs.forEach(([, o, hgh, l, c], k) => {
      const up = c >= o, col = up ? css("--green") : css("--red");
      if (prov.has(k)) { provCandle(g, X(k), Y, o, hgh, l, c, col, Math.max(bw, slot(k) * 0.72), up && hollow); return; }
      g.strokeStyle = col; g.lineWidth = up && hollow ? hl : 1; g.beginPath(); g.moveTo(X(k), Y(hgh)); g.lineTo(X(k), Y(l)); g.stroke();
      g.fillStyle = col;
      const y1 = Y(Math.max(o, c)), y2 = Y(Math.min(o, c));
      if (up && hollow) { g.fillStyle = css("--ink"); g.fillRect(X(k) - hw / 2, y1, hw, Math.max(1, y2 - y1)); g.strokeRect(X(k) - hw / 2 + hl / 2, y1 + hl / 2, hw - hl, Math.max(hl, y2 - y1 - hl)); }
      else g.fillRect(X(k) - bw / 2, y1, bw, Math.max(1, y2 - y1));
    });
    g.lineWidth = 1;

    if (chart.stop) {
      line(g, [[0, Y(chart.stop)], [w - padR, Y(chart.stop)]], css("--orange"), 1.2, [6, 4]);
      tag(g, w - padR, Y(chart.stop), `STOP ${pxAxis(chart.stop)}`, css("--orange"));
    }
    if (chart.entry) line(g, [[0, Y(chart.entry)], [w - padR, Y(chart.entry)]], css("--entry") || "rgba(241,241,246,.6)", 1, [2, 3]);

    const pin = css("--entry-mark") === "pin";   // theme hook: an entry as an ink pin lettered L / S, not an up / down triangle
    for (const m of chart.markers) {
      const k = nearest(tsIndex, m.ts, chart.bar_ms, cs);
      if (k == null) continue;
      const x = X(k), y = Y(m.price);
      if (m.kind === "entry") {
        const short = m.side === "short";
        if (pin) { entryPin(g, x, y, short); continue; }
        g.fillStyle = css(short ? "--red" : "--green");
        g.beginPath();
        if (short) { g.moveTo(x, y - 4); g.lineTo(x - 5, y - 12); g.lineTo(x + 5, y - 12); }
        else { g.moveTo(x, y + 4); g.lineTo(x - 5, y + 12); g.lineTo(x + 5, y + 12); }
        g.fill();
      } else {
        g.fillStyle = m.pnl >= 0 ? css("--green") : css("--red");
        g.beginPath(); g.arc(x, y, 4, 0, Math.PI * 2); g.fill();
        g.strokeStyle = css("--ink") || "#070b10"; g.stroke();
      }
    }
    const last = cs[cs.length - 1];
    tag(g, w - padR, Y(lastC), pxAxis(lastC), css(lastC >= last[1] ? "--green" : "--red"), true);
    // BTC on Coinbase: its live price from Coinbase's feed, as a line and an outlined tag at the right edge (placed
    // above; none when the newest candle's close already says it). The candle still forming is formingBars'
    if (chart.live) {
      const y = Y(chart.live), cyan = css("--cyan") || "#2ee6ff";
      line(g, [[0, y], [w - padR, y]], rgba("--cyan", .7), 1, [2, 3]);
      g.save(); g.font = "bold 9px JetBrains Mono, monospace"; g.fillStyle = cyan; g.textAlign = "right";
      // its word clear above the wicks of the newest candles under it (BTC's forming candle runs up to the line), unless
      // that's far from the line: then on it, as before
      const x1 = w - padR - 4, x0 = x1 - g.measureText("LIVE").width - 2, ty0 = (liveY != null && liveY < y ? liveY : y) - 4;
      let ty = ty0;
      for (let k = cs.length - 1; k >= 0 && X(k) + slot(k) / 2 >= x0; k--) if (X(k) - slot(k) / 2 <= x1) ty = Math.min(ty, Y(cs[k][2]) - 3);
      g.fillText("LIVE", x1, ty0 - ty > 36 || ty < padT + 9 ? ty0 : ty); g.restore();
      if (liveY != null) tag(g, w - padR, liveY, pxAxis(chart.live), cyan);
    }
    lastGeom = { X, Y, cs, w, h, padR };
  }

  // The price axis's width: room for its widest tag ("STOP 77,970" is wider than a price) and label, so none is cut off
  // at the canvas's right edge on a phone. A tag is drawn from 2 px past the axis line, 8 px wider than its text (tag()),
  // and keeps 2 px clear of the edge; an axis label starts 6 px past it. At least today's 64 px
  function rightPad(g, chart, close, lo, hi) {
    g.font = "bold 10px JetBrains Mono, monospace";
    const tags = [pxAxis(close), chart.stop ? `STOP ${pxAxis(chart.stop)}` : "", chart.live ? pxAxis(chart.live) : ""];
    const tagW = Math.max(...tags.map((t) => g.measureText(t).width + 12));
    g.font = "10px JetBrains Mono, monospace";
    const axisW = Math.max(g.measureText(pxAxis(hi)).width, g.measureText(pxAxis(lo)).width) + 8;
    return Math.ceil(Math.max(64, tagW, axisW));
  }

  // A provisional candle (BTC's newest, carried on from Coinbase's feed until the next run): its wick and its body's
  // outline in dashes, up or down in its usual color, in both editions. The body is the paper (--ink) where the theme
  // draws that candle hollow (an up one, --candle-up: hollow), else a pale wash of its color: filled stays filled
  function provCandle(g, x, Y, o, h, l, c, col, pw, hollowBody) {
    const y1 = Y(Math.max(o, c)), y2 = Y(Math.min(o, c)), bh = Math.max(3, y2 - y1);
    g.save(); g.strokeStyle = col; g.lineWidth = 1; g.setLineDash([2, 2]);
    g.beginPath(); g.moveTo(x, Y(h)); g.lineTo(x, y1); g.moveTo(x, y1 + bh); g.lineTo(x, Y(l)); g.stroke();
    g.fillStyle = css("--ink") || "#070b10"; g.fillRect(x - pw / 2, y1, pw, bh);
    if (!hollowBody) { g.globalAlpha = 0.3; g.fillStyle = col; g.fillRect(x - pw / 2, y1, pw, bh); g.globalAlpha = 1; }
    g.strokeRect(x - pw / 2 + 0.5, y1 + 0.5, pw - 1, bh - 1);
    g.restore();
  }

  function nearest(index, ts, barMs, cs) {
    const open = ts - (ts - cs[0][0]) % barMs;
    if (index.has(open)) return index.get(open);
    if (ts < cs[0][0]) return null;
    return Math.min(cs.length - 1, Math.floor((ts - cs[0][0]) / barMs));
  }

  // theme hook (--entry-mark: pin): an entry at its price as a pin in the page's ink, lettered L (a long: the pin hangs
  // below the price) or S (a short: above it). Neither a gain nor a loss color, and no arrow: arrows mean money
  function entryPin(g, x, y, short) {
    const d = short ? -1 : 1, top = short ? y - 18 : y + 6;
    g.save(); g.lineWidth = 1; g.strokeStyle = g.fillStyle = css("--pen") || css("--text");
    g.beginPath(); g.arc(x, y, 1.8, 0, Math.PI * 2); g.fill();
    g.beginPath(); g.moveTo(x, y); g.lineTo(x, y + d * 6); g.stroke();
    g.fillStyle = css("--panel"); g.fillRect(x - 5.5, top, 11, 12); g.strokeRect(x - 5, top + 0.5, 10, 11);
    g.fillStyle = css("--text"); g.font = "bold 9px JetBrains Mono, monospace"; g.textAlign = "center";
    g.fillText(short ? "S" : "L", x, top + 9); g.restore();
  }

  function tag(g, x, y, text, color, filled = false) {
    g.save(); g.font = "bold 10px JetBrains Mono, monospace";
    const tw = g.measureText(text).width + 8;
    const ink = css("--ink") || "#070b10";
    g.fillStyle = filled ? color : ink; g.strokeStyle = color;
    g.fillRect(x + 2, y - 8, tw, 16); g.strokeRect(x + 2, y - 8, tw, 16);
    g.fillStyle = filled ? ink : color; g.fillText(text, x + 6, y + 4); g.restore();
  }

  // ---- the crawler's web: legs reaching out to the last candles' highs ------------
  function drawWeb() {
    const canvas = $("web");
    const { g, w, h } = fit(canvas);
    if (!lastGeom || window.innerWidth < 980) return;
    const { X, Y, cs } = lastGeom;
    const chartTop = $("chart").offsetTop;
    const cx = w * 0.56, cy = h * 0.78;
    const silk = css("--web-silk") || css("--cyan") || "#2ee6ff", dot = css("--web-dot") || "#fff";   // theme hooks
    const picks = [];
    for (let k = cs.length - 1; k >= 0 && picks.length < 9; k -= Math.max(1, Math.floor(cs.length / 18))) picks.push(k);
    g.save();
    g.strokeStyle = silk; g.lineWidth = 1.3; g.shadowColor = silk; g.shadowBlur = +(css("--web-glow") || 6);
    picks.forEach((k, n) => {
      const tx = X(k) + 6, ty = Y(n % 2 ? cs[k][3] : cs[k][2]) + chartTop;
      const kx = (cx + tx) / 2 + (n % 2 ? 18 : -18), ky = (cy + ty) / 2 + 22;
      g.beginPath(); g.moveTo(cx, cy); g.lineTo(kx, ky); g.lineTo(tx, ty); g.stroke();
      g.fillStyle = dot; g.beginPath(); g.arc(tx, ty, 2.4, 0, Math.PI * 2); g.fill();
      g.beginPath(); g.arc(kx, ky, 1.6, 0, Math.PI * 2); g.fill();
    });
    if (document.documentElement.dataset.spider === "on") return g.restore();   // spider.js walks the real one
    g.shadowBlur = 0;
    g.fillStyle = css("--web-hub") || "#1a1d33"; g.strokeStyle = dot; g.lineWidth = 2;
    g.beginPath(); g.ellipse(cx, cy, 13, 10, -0.4, 0, Math.PI * 2); g.fill(); g.stroke();
    g.fillStyle = silk; g.beginPath(); g.ellipse(cx + 12, cy + 6, 8, 6, -0.4, 0, Math.PI * 2); g.fill();
    g.restore();
  }

  // ---- equity + pnl bars ----------------------------------------------------
  function drawEquity(eq, marks = []) {
    const { g, w, h } = fit($("equity"));
    if (eq.length < 2) {
      const y = h / 2;
      line(g, [[0, y], [w, y]], css("--green"), 1.5, [5, 5]);
      g.font = "10px JetBrains Mono, monospace"; g.fillStyle = css("--muted");
      g.fillText(`${eq.length ? usd(eq[0][1]) + " · " : ""}the curve starts drawing at the first bar close`, 4, y - 8);
      return;
    }
    const ys = eq.map((p) => p[1]);
    let lo = Math.min(...ys), hi = Math.max(...ys);
    if (hi === lo) { hi += 1; lo -= 1; }
    const padL = 46;
    // deposits and withdrawals in the curve's window (older ones are off it): the plan's as ticks on the bottom axis,
    // extras and withdrawals as a dashed line with their amount, in a band kept clear above the curve
    const shown = marks.filter(([ts]) => ts >= eq[0][0] - (eq[1][0] - eq[0][0]));
    const big = shown.filter(([, amt, kind]) => kind !== "plan" || amt < 0), top = big.length ? 30 : 6;
    const X = (k) => padL + k / (eq.length - 1) * (w - padL - 8);
    const Y = (v) => top + (hi - v) / (hi - lo) * (h - top - 8);
    g.font = "10px JetBrains Mono, monospace"; g.fillStyle = css("--dim");
    [hi, (hi + lo) / 2, lo].forEach((v) => { g.fillText(usdShort(v), 0, Y(v) + 3); line(g, [[padL, Y(v)], [w, Y(v)]], css("--line"), 1); });
    const up = ys[ys.length - 1] >= ys[0];
    const tone = up ? "--green" : "--red";
    const grad = g.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, rgba(tone, .28)); grad.addColorStop(1, rgba(tone, 0));
    g.beginPath(); g.moveTo(X(0), h); eq.forEach((p, k) => g.lineTo(X(k), Y(p[1]))); g.lineTo(X(eq.length - 1), h); g.closePath();
    g.fillStyle = grad; g.fill();
    line(g, eq.map((p, k) => [X(k), Y(p[1])]), css(tone), 2);
    // a deposit or withdrawal is a step in the bank, not profit or loss (a label that would overlap another is left
    // off, the line stays; one after the newest point is drawn at it)
    const at = (ts) => { const k = eq.findIndex((p) => p[0] > ts); return X(k < 0 ? eq.length - 1 : k); };
    shown.forEach(([ts, amt, kind]) => { if (kind === "plan" && amt > 0) line(g, [[at(ts), h - 7], [at(ts), h]], css("--blue"), 1.5); });
    const ends = [0, 0];
    g.fillStyle = css("--blue");
    big.forEach(([ts, amt]) => {
      const x = at(ts), up = amt > 0, text = `${up ? "+" : "−"}$${Math.round(Math.abs(amt)).toLocaleString("en-US")} ${up ? "in" : "out"}`;
      line(g, [[x, 2], [x, h - 4]], css("--blue"), 1, [2, 3]);
      const tw = g.measureText(text).width, tx = Math.min(x + 3, w - tw - 2), row = ends.findIndex((e) => tx >= e + 6);
      if (row < 0) return;
      g.fillText(text, tx, 12 + row * 12);
      ends[row] = tx + tw;
    });
    const lx = X(eq.length - 1), ly = Y(ys[ys.length - 1]);
    g.fillStyle = css(tone); g.beginPath(); g.arc(lx, ly, 4, 0, Math.PI * 2); g.fill();
    g.strokeStyle = rgba(tone, .4); g.lineWidth = 4; g.beginPath(); g.arc(lx, ly, 8, 0, Math.PI * 2); g.stroke();
  }

  function drawPnlBars(pnls) {
    const { g, w, h } = fit($("pnlbars"));
    if (!pnls.length) return;
    const m = Math.max(...pnls.map(Math.abs)) || 1;
    const bw = (w - 46) / Math.max(pnls.length, 40);
    pnls.forEach((p, k) => {
      const bh = Math.max(3, Math.abs(p) / m * (h - 4));
      g.fillStyle = p >= 0 ? css("--green") : css("--red");
      g.fillRect(46 + k * bw, h - bh, Math.max(1, bw * 0.7), bh);
    });
  }

  // ---- DOM sections -------------------------------------------------------------
  function renderAgents(agents) {
    const root = $("agents");
    if (!root.children.length) {
      agents.forEach((a) => {
        const el = document.createElement("article");
        el.className = "card"; el.id = `card-${a.key}`;
        el.append(sprite(a.key));
        el.insertAdjacentHTML("beforeend", `<div class="name" style="color:var(${COLORS[a.key]})">${esc(a.name)}</div>
          <div class="role">${esc(a.role)}</div><div class="detail"></div><div class="value"></div>
          <div class="bar"><i></i></div><div class="quote"></div>`);
        if (a.asks_jev) el.insertAdjacentHTML("beforeend", `<span class="asks">ASKS JEV</span>`);
        root.append(el);
      });
    }
    agents.forEach((a) => {
      const el = $(`card-${a.key}`);
      el.className = `card ${a.state || ""}`;
      el.style.setProperty("--card", `var(${COLORS[a.key]})`);
      el.querySelector(".detail").textContent = a.detail || "";
      el.querySelector(".value").textContent = a.value ?? "";
      const bar = el.querySelector(".bar i");
      bar.style.width = `${Math.round((a.score ?? (a.state === "active" ? 1 : 0.25)) * 100)}%`;
      bar.style.background = `var(${COLORS[a.key]})`;
      el.querySelector(".quote").textContent = `"${a.quote}"`;
      el.querySelector(".quote").title = a.quote;
    });
  }

  const BUBBLE_SPOTS = { scan: [6, 6], vet: [52, 8], size: [8, 62], fills: [30, 80], risk: [58, 50], crowd: [62, 24] };
  function renderBubbles(agents) {
    const root = $("bubbles");
    root.innerHTML = "";
    agents.filter((a) => BUBBLE_SPOTS[a.key]).forEach((a) => {
      const [x, y] = BUBBLE_SPOTS[a.key];
      const b = document.createElement("div");
      b.className = "bubble";
      b.style.left = `${x}%`; b.style.top = `${y}%`;
      b.style.borderColor = `var(${COLORS[a.key]})`;
      b.innerHTML = `<b style="color:var(${COLORS[a.key]})">${esc(a.name)}</b>${esc(a.quote)}`;
      root.append(b);
    });
    const chief = agents.find((a) => a.key === "chief");
    $("chief-bubble").textContent = chief ? chief.quote : "";
  }

  // With deposits or withdrawals (the board only has "flows" once there's one): the P&L leaves them out, and its
  // percentage is the time-weighted performance, not the P&L over the money put in, so it's named
  const pnlTag = (s) => s.flows ? `P&amp;L ${usd(s.ticker.pnl)} · PERF ${pct(s.ticker.pnl_pct)} TIME-WEIGHTED · ` +
    `${s.flows.net >= 0 ? "DEPOSITED" : "WITHDRAWN"} ${usd(Math.abs(s.flows.net))}, NOT P&amp;L` : `P&amp;L ${usd(s.ticker.pnl)} (${pct(s.ticker.pnl_pct)})`;
  const pnlHtml = (s) => pnlTag(s).replace(`P&amp;L ${usd(s.ticker.pnl)}`, `<span class="${tone(s.ticker.pnl)}">P&amp;L ${usd(s.ticker.pnl)}</span>`);

  // A part of the page rewritten only when its HTML changes, so an animation in it (the live dot's pulse) runs on
  // through redraws that come once a second with Coinbase's feed
  const put = (el, html) => { if (el._html !== html) { el._html = html; el.innerHTML = html; } };
  const txt = (el, text) => { if (el.textContent !== text) el.textContent = text; };   // the same, for plain text

  function renderStage(s) {
    const t = s.ticker, k = s.kills, coin = s.header.coin || "BTC";
    // BTC on Coinbase: between runs the live price is Coinbase's own (its feed), else Hyperliquid's (a proxy, named
    // as one), and nothing moves while Coinbase is closed
    const v = s.venue, lp = s.livePx || {}, cbLive = v && lp.source === "coinbase";
    const proxy = v && !lp.closed && !cbLive && live.at;
    const dot = (word) => `<span class="live-dot" aria-hidden="true"></span><span class="live-word">${word}</span> · `;
    // the proxy's price stops being called live once it's older than the desk monitor allows (Hyperliquid not answering)
    const liveTag = v && lp.closed ? closedTag("CLOSED (COINBASE) · ") : cbLive ? dot("LIVE (COINBASE)") : proxy && lp.stale ? "STALE (HYPERLIQUID PROXY) · "
      : live.at ? dot(`LIVE${v ? " (HYPERLIQUID PROXY)" : ""}`) : "";
    const pos = String(t.position || "");
    const posCls = pos.startsWith("SHORT") ? "short" : pos.startsWith("LONG") ? "long" : "flat";
    const chgCls = t.change_24h == null || isNaN(t.change_24h) ? "" : t.change_24h >= 0 ? "ok" : "bad";
    const tk = $("ticker");
    if (!tk.querySelector(".tk-tag")) tk.innerHTML = `‹ <span class="tk-tag"></span><span class="tk-body"></span>`;
    put(tk.querySelector(".tk-tag"), liveTag);
    put(tk.querySelector(".tk-body"), `TRADES · ${esc(coin)} ${px(t.price)} · BANK ${usd(t.bank)} · ` +
      `<span class="hl ${posCls}">${esc(t.position)}${t.position_detail ? " " + esc(t.position_detail) : ""}</span>` +
      ` · ${esc(t.record)} · HOLD <span class="${tone(t.hold_pct, 4)}">${pct(t.hold_pct)}</span>` +
      ` · ${pnlHtml(s)} · FEES ${usd(t.fees)}${v ? " (EST.)" : ""} · FUNDING ${v ? "(EST.) " : ""}${usd(t.funding)}`);
    txt($("chart-title"), `${v ? v.product : coin + "-PERP"} · ${s.header.interval} · ${px(t.price)}${proxy ? " (HL proxy)" : ""}`);
    put($("chart-sub"), `<span class="${chgCls}">24h ${pct(t.change_24h)}</span>` +
      ` · fast <i class="swatch fast"></i> · slow <i class="swatch slow"></i> · channel <i class="swatch channel"></i> · stop <i class="swatch stop"></i>` +
      (s.chart.prov && s.chart.prov.length ? ` · newest candle${s.chart.prov.length > 1 ? "s" : ""} <i class="swatch prov"></i> provisional` : ""));
    const chips = $("chips");
    put(chips, s.agents.map((a) => `<span class="chip ${a.state === "idle" ? "off" : ""}" title="${esc(a.name)}: ${esc(a.state)}"
      style="background:var(${COLORS[a.key]})">${esc(a.name[0])}</span>`).join(""));
    if (!$("chief-sprite").children.length) $("chief-sprite").append(sprite("chief", 4));
    txt($("mini-time"), `${MT.clock(s.updated)} ${TZ}`);
    put($("kill-state"), [
      ["flat_kill", k.flat_kill], ["entry_kill", k.entry_kill], ["jev_kill", k.jev_kill],
      ["drawdown halt", k.halted], ["daily loss lock", k.daily_locked], ["cooldown", k.cooldown],
    ].map(([n, on]) => `<div class="${on ? "on" : ""}"><span>${n}</span><span>${on ? "ON" : "off"}</span></div>`).join(""));
    const j = s.jev, ans = j.answers;
    const row = (label, v) => `<div class="q"><span>${label}</span><div class="bar"><i style="width:${Math.round((v ?? 0) * 100)}%;background:var(--white)"></i></div></div>`;
    put($("jevbox"), `<div class="q"><span>MODE</span>${esc(j.mode)}${j.enabled ? "" : " · no key"} · ${j.calls} calls</div>` +
      (ans ? row(`spent ${num(ans.momentum_already_spent?.noul)}`, ans.momentum_already_spent?.noul) +
             row(`follow-through ${num(ans.follow_through?.noul)}`, ans.follow_through?.noul) +
             row(`trend ${num(ans.regime?.probabilities?.trend)}`, ans.regime?.probabilities?.trend)
           : `<div class="q"><span>LAST ANSWER</span>asked only when every other gate passes</div>`));
  }

  function renderBalance(s) {
    const t = s.ticker, goal = s.goal;
    txt($("bal-day"), `DAY ${s.header.day}`);
    txt($("bal-mode"), s.header.mode);
    txt($("bal-record"), t.record);
    txt($("bal-hold"), pct(t.hold_pct));
    $("bal-hold").className = t.hold_pct < 0 ? "bad" : "ok";
    txt($("balance"), usd(t.bank));
    $("balance").style.color = t.pnl >= 0 ? "var(--green)" : "var(--red)";
    if (goal) {
      txt($("goal-from"), usd(goal.start, 0));
      txt($("goal-to"), usd(goal.target, 0));
      $("goal-bar").style.width = `${(goal.progress * 100).toFixed(1)}%`;
      const yrs = (y) => y == null ? "—" : y > 200 ? "200+ years" : `${y.toFixed(y < 10 ? 1 : 0)} years`;
      // At the backtest's out-of-sample pace: its whole period includes the years the settings were picked on, so it
      // flatters. A board from before that change has only backtest_cagr (the whole period): it keeps its old label.
      // "backtest", not "test": the account itself is the paper test, and ETH/SOL's 2024–26 isn't fully unseen.
      const yr = (d) => typeof d === "string" && /^\d{4}/.test(d) ? d.slice(0, 4) : null, y0 = yr(goal.test_from), y1 = yr(goal.test_to);
      const span = y0 && y1 ? (y0 === y1 ? y0 : `${y0}–${y1.slice(2)}`) + " " : "";
      const at = (rate, y) => `<span>(${pct(rate, 1)}/yr): ${yrs(y)}</span>`;   // wraps before the numbers, not inside them
      const pace = goal.test_cagr === undefined && goal.backtest_cagr !== undefined
        ? `at backtest pace ${at(goal.backtest_cagr, goal.years_at_backtest_rate)}`
        : `at ${span}backtest pace${goal.test_label ? ` of ${esc(goal.test_label)}` : ""}${goal.plan_included ? " + planned deposits" : ""} ${at(goal.test_cagr, goal.years_at_test_rate)}`;
      // with deposits: the paces are the NAV's (deposits aren't growth), the years count the plan still to come
      const din = goal.deposits_in || 0, dout = goal.withdrawn || 0, nx = goal.next_extra;
      const money = goal.deposited == null ? "" : `<br>${[din && `deposited ${usd(din, 0)}`, dout && `withdrawn ${usd(dout, 0)}`].filter(Boolean).join(" · ")}` +
        ` so far (money ${din && dout ? "in and out, not profit or loss" : din ? "in, not profit" : "out, not a loss"})` +
        (goal.planned_left ? ` · ${goal.planned_left} more planned, ${usd(goal.planned_left_usd, 0)}` : "") +
        (nx ? ` · next extra ${nx.amount > 0 ? "+" : "-"}${usd(Math.abs(nx.amount), 0)} on ${esc(nx.date)}` : "");
      put($("goal-note"), `${(goal.progress * 100).toFixed(1)}% of the way (log scale) · ${num(goal.multiple_left, 0)}x to go<br>` + pace +
        (goal.realised_cagr != null ? `<br>at this account's own pace ${at(goal.realised_cagr, goal.years_at_realised_rate)}` : "") + money);
    }

    // theme hook: HOLD and the big balance as a gain, a loss or flat (tone(): the same ok / bad as above, plus up / down / flat)
    $("bal-hold").className = tone(t.hold_pct, 4); $("balance").className = `big ${tone(t.pnl)}`;
  }

  const GATE_FMT = (g) => {
    if (g.value == null) return g.note || "";
    const v = Math.abs(g.value) >= 1000 ? Math.round(g.value).toLocaleString("en-US") : num(g.value, 2);
    const t = g.threshold == null ? "" : ` ${g.op === "min" ? "≥" : g.op === "max" ? "≤" : "vs"} ${Math.abs(g.threshold) >= 1000 ? Math.round(g.threshold).toLocaleString("en-US") : num(g.threshold, 2)}`;
    return `${v}${t}`;
  };

  // BTC on Coinbase: its session in the venue line, as the price on screen has it (lp: livePrice at this redraw), not
  // only as the run found it. A scheduled closure that began since the run is CLOSED NOW (the board's closed_now is
  // still empty), one the run found is CLOSED AT THE LAST RUN once Coinbase's feed shows it trading, and the next
  // closure is named only while it is still ahead: one that is under way or over isn't "next"
  function closureNote(v, lp, now) {
    const nc = v.session && v.session.next_closure, cn = v.closed_now, shut = lp && lp.closed;
    const closed = shut && shut.by === "schedule" ? ` · <b>CLOSED NOW: ${esc(shut.why)} until ${hhmm(shut.until)} ${TZ}</b>`
      : cn ? ` · <b>${shut || !lp ? "CLOSED NOW" : "CLOSED AT THE LAST RUN"}: ${esc(cn.why)}${cn.until ? ` until ${hhmm(cn.until)} ${TZ}` : ""}</b>` : "";
    const next = nc && now < nc.start && !(cn && cn.scheduled) ? ` · next ${cn ? "scheduled " : ""}closure ${esc(nc.why)} ${MT.span(nc.start, nc.end, true)} ${TZ}` : "";
    return closed + next;
  }

  // light: a redraw for a new price only (the gates and events are the board's)
  function renderAnalysis(s, light = false) {
    const st = s.analysis.stats, basket = s.header.kind === "basket", t = s.ticker;
    txt($("analysis-title"), basket ? "BASKET · ANALYSIS" : "DESK · ANALYSIS");
    txt($("scanned"), `${s.analysis.scanned.toLocaleString("en-US")} ${basket ? "DAILY CLOSES DECIDED" : "BARS SCANNED"}`);
    const v = s.venue, lp = s.livePx || {};   // BTC on Coinbase: whole contracts, its margin, funding estimated from logged rates
    const cells = basket ? [
      ["$" + s.header.coin, "basket", "sym"], ["Cash", usd(t.cash)], ["Held", usd(t.notional)],
      ["Exposure", `${num(t.exposure)}x`], ["Unrealised", usd(t.unrealised), `h-pnl ${tone(t.unrealised)}`], ["Fees", usd(t.fees)], ["Funding", usd(t.funding)],
    ] : v ? [
      ["$" + (s.header.coin || "BTC"), "perp · " + s.header.venue, "sym"],
      [lp.closed ? "Price" : lp.source === "coinbase" ? "Price (Coinbase live)" : live.at ? "Price (HL proxy)" : "Price", px(st.price)],
      [`Contracts · ${v.contract_size} ${s.header.coin || "BTC"}`, String(v.contracts)],
      [`Margin · ${(v.margin_rate * 100).toFixed(1)}% o/n`, v.contracts ? usd(v.margin_in_use, 0) : "none"],
      ["Funding APR (est.)", `${num(st.funding_apr, 1)}%`], ["ATR", `${num(st.atr_pct)}%`],
      ["Spread", st.spread_bps == null ? "—" : `${num(st.spread_bps, 2)} bps`],
    ] : [
      ["$" + (s.header.coin || "BTC"), "perp · " + s.header.venue, "sym"], ["Price", px(st.price)],
      ["24h", pct(st.change_24h), st.change_24h == null || isNaN(st.change_24h) ? "" : st.change_24h >= 0 ? "ok" : "bad"],
      ["ATR", `${num(st.atr_pct)}%`], ["Funding APR", `${num(st.funding_apr, 1)}%`],
      ["Open interest", usdShort(st.open_interest_usd)], ["Spread", st.spread_bps == null ? "—" : `${num(st.spread_bps, 2)} bps`],
    ];
    put($("stats"), cells.map(([k, v, cls]) => `<div class="${cls || ""}"><span>${k}</span><b>${esc(v)}</b></div>`).join(""));
    const ol = s.analysis.outlook || [];
    const day = (ms) => `${MT.wd(ms)} ${MT.clock(ms)}`;   // "Fri 6:00 pm"
    const olHead = s.analysis.outlook_side === "short"
      ? `SHORT LINE · a 2h close below it shorts; steps up as old lows age out (if ${esc(s.header.coin || "BTC")} makes no new low)`
      : `BUY LINE · steps down as old highs age out (if ${esc(s.header.coin || "BTC")} makes no new high)`;
    const sk = v && v.skipping, fs = v && v.funding.stale_since;
    const venueLine = v ? `<div class="ol-exp">${esc(v.display)} RULES · whole ${v.contract_size} BTC contracts (one is ${usd(v.one_contract_usd, 0)}), ` +
      `≤${v.max_leverage}x, overnight margin ${(v.margin_rate * 100).toFixed(1)}% · fees est. ${esc(v.fee_estimate)} · funding est. from ${v.funding.logged} logged hourly rates` +
      (fs ? ` <b>(none new since ${MT.when(fs)} ${TZ})</b>` : "") + (v.stop_fill ? ` · ${esc(v.stop_fill)}` : "") +
      closureNote(v, s.livePx, Date.now()) +
      (v.waiting_for ? ` · <b>WAITING for Coinbase's ${MT.when(v.waiting_for)} ${TZ} candle</b>` : "") +
      (sk ? ` · <b>SKIPPING: ${esc(sk.note)}</b>` : "") + `</div>` : "";
    put($("outlook"), venueLine + (s.header.experiment ? `<div class="ol-exp">EXPERIMENT · ${esc(s.header.experiment)}</div>` : "") +
      (ol.length ? `<div class="ol-head">${olHead}</div>` +
      `<div class="ol-rows">` + ol.map((r, k) => `<div class="${k === 0 ? "now" : ""}"><span>${k === 0 ? "next close" : day(r.ts) + " " + TZ}</span>` +
      `<b>${px(r.level)}</b><i>${pct(r.level / st.price - 1, 1)}</i></div>`).join("") + `</div>` : ""));
    if (light) return;
    put($("gates"), s.analysis.gates.map((g) =>
      `<li><span>${esc(g.name.replace(/_/g, " "))} <i class="na">${esc(GATE_FMT(g))}</i></span><span class="v ${g.verdict}">${g.verdict}</span></li>`).join(""));
    const when = basket ? MT.when : hhmm;   // the basket trades once a day: its events need the date
    put($("events"), s.events.slice().reverse().map((e) =>
      `<li><b style="color:var(${COLORS[e.agent] || "--muted"})">${esc(e.agent.toUpperCase())}</b>${when(e.ts)} ${TZ} ${esc(e.text)}</li>`).join("") ||
      `<li>no events yet — the desk speaks when something happens.</li>`);
  }

  function renderThresholds(rows) {
    const groups = { KILL: [], HARD: [], SOFT: [] };
    rows.forEach((r) => (groups[r.section] || groups.HARD).push(r));
    const desc = { KILL: "desk-level brakes", HARD: "arithmetic, costs nothing", SOFT: "applied to the agents' scores and Jev's answers" };
    const fmt = (x) => x == null ? "—" : Math.abs(x) >= 1000 ? Math.round(x).toLocaleString("en-US") : num(x, 2);
    $("thr").innerHTML = Object.entries(groups).filter(([, v]) => v.length).map(([sec, v]) =>
      `<div class="thr-sec">${sec} — ${v.length} · ${desc[sec]}</div>` + v.map((r) => {
        const mark = r.verdict === "PASS" || r.verdict === "MARK" ? `<span class="ok">✓</span>` :
          r.verdict === "DROP" ? `<span class="bad">✗</span>` : r.verdict === "WAIT" ? `<span class="na">…</span>` : `<span class="na">–</span>`;
        const thr = r.threshold == null ? (r.verdict === "SKIP" && r.value != null ? "info" : "") : `${r.op || "vs"} ${fmt(r.threshold)}`;
        return `<div class="thr-row"><span class="n">${esc(r.name)}</span><span class="t">${esc(thr)}</span><span class="c">${fmt(r.value)}</span>${mark}</div>`;
      }).join("")).join("");
  }

  // ---- the VT basket's tab ---------------------------------------------------------
  function renderBasket(s) {
    const t = s.ticker, b = s.basket, r = b.rules, now = Date.now();
    const liveTag = live.at ? `<span class="live-dot" aria-hidden="true"></span><span class="live-word">LIVE</span> · ` : "";
    const pos = String(t.position || "");
    const posCls = pos.startsWith("SHORT") ? "short" : pos.startsWith("LONG") ? "long" : "flat";
    $("vt-ticker").innerHTML = `‹ ${liveTag}BASKET · ${esc(s.header.coins.join(" · "))} · BANK ${usd(t.bank)} · ` +
      `<span class="hl ${posCls}">${esc(t.position)}${t.position_detail ? " " + esc(t.position_detail) : ""}</span>` +
      ` · ${esc(t.record)} · HOLD <span class="${tone(t.hold_pct, 4)}">${pct(t.hold_pct)}</span>` +
      ` · ${pnlHtml(s)} · FEES ${usd(t.fees)} · FUNDING ${usd(t.funding)}`;
    const next = MT.nextUtcDay(now), left = next - now;   // every 00:00 UTC: 6:00 pm MT in summer, 5:00 pm in winter
    txt($("vt-head"), `HOLDINGS · AT THE ${MT.clock(next, { up: true })} ${TZ} DAILY CLOSE`);
    $("vt-next").textContent = `NEXT DECISION ${MT.clock(next, { up: true })} ${TZ} · IN ${Math.floor(left / 3600000)}H ${Math.floor(left % 3600000 / 60000)}M`;
    $("vt-exp").textContent = s.header.experiment ? `EXPERIMENT · ${s.header.experiment}` : "";
    $("vt-rule").textContent = `A ${r.coins.length === 3 ? "third" : "share"} of the book per coin. A coin is held while its daily close ` +
      `is above its ${r.sma_bars}-day average, sized ${r.target_vol.toFixed(2)} ÷ its ${r.vol_lookback}-day vol (at most ${r.max_units} units; ` +
      `1 unit = ${r.book_leverage} ÷ ${r.coins.length} of the bank), and resized only when the new size is more than ` +
      `${Math.round(r.band * 100)}% away. Orders fill at the next day's open; under $${r.min_order_usd} they're skipped (exits always go through).`;
    const head = ["COIN", `VS ${r.sma_bars}-DAY AVG`, `${r.vol_lookback}-DAY VOL`, "UNITS", "HELD", "UNREALISED", "ORDER"];
    const cell = (k, v, sub, cls = "") => `<div class="${cls}"><span>${head[k]}</span><b>${v}</b>${sub ? `<i>${sub}</i>` : ""}</div>`;
    const rows = b.holdings.map((h) => {
      const gap = h.sma_gap == null ? "—" : `<span class="${h.above_sma ? "ok" : "bad"}">${h.above_sma ? "ABOVE" : "BELOW"} ${pct(h.sma_gap, 1)}</span>`;
      const band = h.held_units > 0 && h.target_units > 0 && num(h.target_units) !== num(h.held_units) ? ` · within the ${Math.round(r.band * 100)}% band` : "";
      const o = h.pending;
      const verb = o && (o.kind === "exit" ? "SELL ALL" : o.kind === "entry" ? "BUY" : o.usd > 0 ? "ADD" : "TRIM");
      const order = o ? [o.kind === "exit" ? verb : `${verb} ${usd(Math.abs(o.usd))}`, "fills at the next open"]
        : h.decided_ts == null ? ["PREVIEW", "the first decision is at the next close"]
        : h.qty > 0 ? ["HOLD", h.entry_ts ? `since ${MT.day(h.entry_ts)}` : ""]
        : h.target_units > 0 ? ["FLAT", `entry under the $${r.min_order_usd} minimum; checked again at the next close`]
        : ["FLAT", h.warm ? `waits for a close above the ${r.sma_bars}-day average` : "warming up the averages"];
      return `<div class="h-row">` +
        `<div class="h-coin coin-${["BTC", "ETH", "SOL"].includes(h.coin) ? h.coin.toLowerCase() : "x"}"><span>COIN</span><b>${esc(h.coin)}</b><i>${px(h.mark)}</i></div>` +
        cell(1, gap, h.sma50 ? `avg ${px(h.sma50)}` : "warming up") +
        cell(2, h.vol10 == null ? "—" : `${(h.vol10 * 100).toFixed(0)}%`, "annualised") +
        cell(3, `${num(h.held_units)} held`, `target ${num(h.target_units ?? 0)}${band}`) +
        cell(4, usd(h.notional), h.qty > 0 ? `${(h.weight * 100).toFixed(1)}% of the bank · ${num(h.qty, 5)} ${esc(h.coin)}` : "") +
        cell(5, h.qty > 0 ? `<span class="h-pnl ${tone(h.unrealised)}">${usd(h.unrealised)}</span>` : "—", h.qty > 0 ? `avg entry ${px(h.avg_entry)}` : "") +
        cell(6, order[0], order[1], o ? "pend" : "") + `</div>`;
    });
    $("holdings").innerHTML = `<div class="h-row h-head">${head.map((x) => `<div>${x}</div>`).join("")}</div>` + rows.join("");
    const caught = b.last_close_ts && b.last_close_ts < Math.floor(now / DAY_MS) * DAY_MS
      ? ` The latest daily close (${MT.when(Math.floor(now / DAY_MS) * DAY_MS)} ${TZ}) is picked up on the desk's next run.` : "";
    $("vt-note").textContent = `Prices live from Hyperliquid; averages, vol and units are from the last close` +
      (b.last_close_ts ? ` (${MT.when(b.last_close_ts)} ${TZ})` : "") + `. ` +
      `${b.orders.filled} fills, ${b.orders.blocked} orders skipped by the $${r.min_order_usd} minimum.${caught}`;
  }

  const RULES = {
    coins: "one sleeve each, an equal share of the book", interval: "decided at the daily close ({close}), filled at the next open",
    vol_lookback: "days of returns in the realised vol (x √365)", target_vol: "units = this ÷ the coin's realised vol",
    max_units: "no coin gets more units than this", sma_bars: "held only while the close is above this many days' average",
    band: "held units change only when the new size is further away than this", book_leverage: "a coin's notional = bank x this ÷ 3 x units",
    min_order_usd: "Hyperliquid's minimum order: smaller entries and resizes are skipped", starting_equity: "the paper account's start",
    goal_equity: "the goal the balance panel tracks",
  };
  function renderRules(rules) {
    const fmt = (k, v) => Array.isArray(v) ? v.join(" · ") : /usd|equity/.test(k) ? usd(v, 0) : String(v);
    const close = `${MT.clock(MT.nextUtcDay(Date.now()))} ${TZ}`;   // the next daily close, in MT (an hour earlier in winter)
    $("thr").innerHTML = `<div class="thr-sec">VolTrend — frozen: these exact numbers passed the edge hunt's pre-set bar; not tuned</div>` +
      Object.entries(rules).map(([k, v]) => `<div class="rule"><span class="n">${esc(k)}</span><b>${esc(fmt(k, v))}</b><i>${esc((RULES[k] || "").replace("{close}", close))}</i></div>`).join("");
  }

  function renderKills(k) {
    document.querySelectorAll("[data-kill]").forEach((b) => {
      const on = !!k[b.dataset.kill];
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
  }

  // ---- kill switch POSTs ---------------------------------------------------------
  function token() { try { return localStorage.getItem("desk-token") || ""; } catch { return ""; } }
  document.querySelectorAll("[data-kill]").forEach((b) => b.addEventListener("click", async () => {
    const on = !b.classList.contains("on");
    const res = await fetch("api/kill", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": token() },
      body: JSON.stringify({ switch: b.dataset.kill, on }),
    }).catch(() => null);
    if (res && res.status === 403) {
      const t = prompt("Dashboard token (DASHBOARD_TOKEN):");
      if (t) { try { localStorage.setItem("desk-token", t); } catch { /* private mode */ } }
    }
    $("kill-hint").textContent = res && res.ok ? `${b.dataset.kill} ${on ? "ON" : "off"} — applied on the next poll` : "couldn't reach the desk";
    tick();
  }));

  // ---- coins, live prices, main loop ----------------------------------------------
  const ACCOUNTS = ["BTC", "ETH", "SOL", "VT"];   // VT = the vol-trend basket (BTC + ETH + SOL), paper only
  const HL = "https://api.hyperliquid.xyz/info";
  const storeGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
  const storeSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } };
  let coin = (location.hash.slice(1) || storeGet("desk-coin") || "BTC").toUpperCase();
  if (!ACCOUNTS.includes(coin)) coin = "BTC";

  let state = null, base = null, index = null;
  const live = { mids: {}, at: 0, candle: null };   // filled from Hyperliquid by the viewer's own browser
  // spider.js listens: "desk:board" after a board loads, "desk:tick" after a live price. candle() = the newest
  // candle's high in #chart px as last drawn, so it stays right after a resize
  const newest = () => lastGeom && { x: lastGeom.X(lastGeom.cs.length - 1), y: lastGeom.Y(lastGeom.cs[lastGeom.cs.length - 1][2]) };
  const emit = (name, detail) => window.dispatchEvent(new CustomEvent(name, { detail: { coin: state && state.header.coin, candle: newest, ...detail } }));
  // desk:tick: the open tab's price as just drawn and where it came from, source "coinbase" or "hyperliquid" (null
  // while Coinbase is closed: then `closed` says why); priceAt is that price's time. mids and at stay Hyperliquid's.
  // desk:board carries the same as `live`, so a listener knows it from the board's first draw. A tick comes with each
  // new price (an allMids answer, a Coinbase message), and at once when where the price comes from changes, whatever
  // redrew the page (the feed going quiet, a closure starting, the page shown again)
  const liveNow = () => {
    const p = state && state.livePx;
    return { price: state && state.ticker.price, source: p ? p.source : "hyperliquid", priceAt: p && p.source ? p.at : live.at, closed: (p && p.closed) || null };
  };
  let told = null;   // what the last desk:tick or desk:board said
  const tell = (n) => { told = { coin: state && state.header.coin, source: n.source, by: n.closed ? n.closed.by : null, priceAt: n.priceAt, at: live.at }; return n; };
  const ticked = () => emit("desk:tick", { ...tell(liveNow()), mids: live.mids, at: live.at });

  async function hl(body) {
    const res = await fetch(HL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(res.status);
    return res.json();
  }

  // ---- BTC on Coinbase: its live price from Coinbase's own market-data feed ---------------------------------------
  // Coinbase's REST API sends no CORS header, so a page can't read it; a WebSocket isn't bound by CORS, and the public
  // market-data feed needs no key (a page can't keep one safe). One socket, only while the BTC account needs a price
  // (its tab, or its card in the tab row) and the page is visible; the product is the board's venue.product. It
  // subscribes to the ticker and to heartbeats (one a second; Coinbase closes a connection that goes 60-90 s with
  // nothing to send). Coinbase documents a ticker message for each trade; measured, one also comes when the best bid or
  // ask moves, about two a second. Its price is the last trade: still Coinbase's price in a minute with no trade.
  // Hyperliquid's BTC stays the proxy whenever this feed isn't fresh, and VT's BTC is always Hyperliquid's: live.mids
  // is never written from here.
  const CB_WS = "wss://advanced-trade-ws.coinbase.com";
  const CB_BEAT = 5000;     // fresh while a heartbeat came within 5 s (one a second: four missed in a row is a dead line)
  const CB_QUIET = 60000;   // ... and a ticker message on this connection within a minute; longer, outside a closure the
                            // board knows of, is a halt or a lost subscription: the proxy, and a new connection asks again
  const CB_DEAD = 10000;    // no heartbeat for 10 s, or still connecting: this connection is closed and retried
  const CB_PAINT = 1000;    // at most one redraw a second from the ticker, always with its newest price
  const CB_BASE = 2000, CB_MAX = 60000, CB_GAP = 1000;   // retries: 2 s doubling to 60 s, jittered; never two a second
  const STALE = 20000;      // the proxy's price older than this (Hyperliquid not answering) isn't called live, as the
                            // desk monitor says "no live price" after as long
  const CB_SKEW = 300000;   // a ticker stamp further than 5 min from its arrival: the arrival time places its price
  // how long ago the latest of these times was, measured either way: a wall clock set back (the phone's own clock
  // corrected) makes a time ahead of now, which is no fresher for it, so nothing stays "fresh" or waits until the clock
  // catches up; the next message, about a second away, is
  const near = (now, ...ts) => Math.min(...ts.map((t) => Math.abs(now - t)));
  // quiet: the ticker went quiet or Coinbase was closed, so a snapshot (the price from before) doesn't count until an
  // update does. calm: when a quiet ticker was last expected (a closure the board knows of). mode: where the price on
  // screen comes from, as last drawn (cbMode)
  const cb = { ws: null, sub: null, product: null, venue: null, price: NaN, at: 0, beat: 0, opened: 0, last: 0, tries: 0,
    retry: 0, dog: 0, due: 0, painted: 0, mode: "", quiet: false, calm: 0, bars: new Map(), barMs: 7200000 };
  // Coinbase's RFC 3339 times carry nanoseconds: cut to milliseconds first (Safari's Date.parse takes no more)
  const cbTime = (s) => {
    const m = typeof s === "string" && /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d{1,3})?\d*Z$/.exec(s);
    const t = m ? Date.parse(m[1] + (m[2] || "") + "Z") : NaN;
    return isFinite(t) ? t : 0;
  };
  const cbFresh = (product, now = Date.now()) => !!cb.ws && cb.ws.readyState === 1 && cb.sub === product && cb.at > 0 &&
    near(now, cb.beat) < CB_BEAT && near(now, cb.at) < CB_QUIET;
  // Coinbase closed: as the last run found it (the board's own price until a run finds it open, or, for a scheduled
  // closure, until its end), or inside a closure the board's schedule lists (its weekly break or a maintenance window)
  // that began since the run
  function cbClosed(v, now = Date.now()) {
    if (!v) return null;
    const cn = v.closed_now;
    if (cn && !(cn.scheduled && cn.until && now >= cn.until)) return { ...cn, by: "run" };
    const nc = v.session && v.session.next_closure;
    return nc && now >= nc.start && now < nc.end ? { why: nc.why, until: nc.end, scheduled: true, by: "schedule" } : null;
  }
  // Where a board's live price comes from right now, decided once per render so a number and its label agree:
  // {source: "coinbase" | "hyperliquid" | null, price, at, closed, stale}
  function livePrice(s, now = Date.now()) {
    const v = s.venue, hlMid = parseFloat(live.mids[s.header.coin || "BTC"]);
    if (!v) return { source: "hyperliquid", price: hlMid, at: live.at };
    const closed = cbClosed(v, now);
    if (closed) return { source: null, price: NaN, at: 0, closed };
    if (v.name === "coinbase" && cbFresh(v.product, now)) return { source: "coinbase", price: cb.price, at: cb.at };
    // past the end of a scheduled closure the run found: closed, as it found it, until Coinbase's feed shows it trading
    if (v.closed_now) return { source: null, price: NaN, at: 0, closed: { ...v.closed_now, by: "run" } };
    return { source: "hyperliquid", price: hlMid, at: live.at, stale: !!live.at && near(now, live.at) > STALE };
  }
  const cbMode = (lp) => JSON.stringify(lp, ["source", "closed", "by", "stale"]);
  const cbWanted = () => !document.hidden && !!cb.product && (!!(base && base.venue && base.venue.product === cb.product) ||
    !!(index && index.coins && index.coins.some((c) => c.venue === "coinbase" && !c.error)));
  function cbSync() {
    if (!cbWanted()) { cbClose(); return; }
    if (cb.ws && cb.sub !== cb.product) cbClose();   // the board names another product: start again with it
    if (!cb.ws && !cb.retry) cbConnect();
    if (!cb.dog) cb.dog = setInterval(cbWatch, 1000);
  }
  function cbConnect() {
    cb.retry = 0;
    if (!cbWanted() || cb.ws || !("WebSocket" in window)) return;
    const wait = Math.min(CB_GAP, cb.last + CB_GAP - Date.now());   // (a clock set back: still at most a second)
    if (wait > 0) { cb.retry = setTimeout(cbConnect, wait); return; }
    cb.last = Date.now();
    let ws;
    try { ws = new WebSocket(CB_WS); } catch { cbRetry(); return; }
    Object.assign(cb, { ws, sub: cb.product, at: 0, beat: 0, opened: 0 });
    ws.onopen = () => {
      if (cb.ws !== ws) return;
      cb.opened = Date.now();
      // one channel per subscribe message, as Coinbase asks, sent at once (it drops a connection with none in 5 s)
      ws.send(JSON.stringify({ type: "subscribe", channel: "heartbeats" }));
      ws.send(JSON.stringify({ type: "subscribe", channel: "ticker", product_ids: [cb.sub] }));
    };
    ws.onmessage = (e) => { if (cb.ws === ws) cbMessage(e.data); };
    ws.onclose = () => { if (cb.ws === ws) cbDrop(); };
    ws.onerror = () => { /* a close follows */ };
  }
  // this connection is over (closed by Coinbase or the network, or gone quiet): the proxy until a retry gets through
  function cbDrop(quiet = false) {
    const ws = cb.ws, up = cb.opened ? Date.now() - cb.opened : 0;
    cb.ws = null; cb.at = 0;
    if (ws) { ws.onopen = ws.onmessage = ws.onclose = null; try { ws.close(1000); } catch { /* already closed */ } }
    // it held for a while: the next retry starts short again (not after a quiet spell: a halted market is asked again
    // less and less often)
    if (up > 60000 && !quiet) cb.tries = 0;
    cbRetry();
    cbWatch();
  }
  function cbRetry() {
    clearTimeout(cb.retry); cb.retry = 0;
    if (!cbWanted()) return;
    const d = Math.min(CB_MAX, CB_BASE * 2 ** Math.min(cb.tries++, 10));
    cb.retry = setTimeout(cbConnect, d / 2 + Math.random() * d / 2);
  }
  function cbClose() {           // not wanted (the page hidden, no Coinbase account shown): no socket, no retry
    clearTimeout(cb.retry); cb.retry = 0;
    clearInterval(cb.dog); cb.dog = 0;
    const ws = cb.ws;
    cb.ws = null; cb.at = 0;
    if (ws) { ws.onopen = ws.onmessage = ws.onclose = null; try { ws.close(1000); } catch { /* already closed */ } }
  }
  // once a second while wanted: a connection stuck connecting or without heartbeats is dropped, and so is one whose
  // ticker has gone quiet outside a closure the board knows of; a change in where the shown price comes from (the feed
  // going quiet, a closure starting, the proxy going stale) is redrawn at once, and the BTC tab's footer counts its age
  function cbWatch() {
    const now = Date.now();
    if (cb.ws && near(now, cb.opened || cb.last, cb.beat) > CB_DEAD) { cbDrop(); return; }
    if (cbClosed(cb.venue, now)) { cb.at = 0; cb.quiet = true; cb.calm = now; }   // nothing from before it is live after it
    else if (cb.ws && cb.opened && near(now, cb.at, cb.opened, cb.calm) > CB_QUIET) { cb.quiet = true; cbDrop(true); return; }
    const on = base && base.venue ? base : { venue: cb.venue, header: { coin: "BTC" } };   // another tab: the BTC card
    if (cbMode(livePrice(on, now)) !== cb.mode) cbPaint(true);
    else if (on === base && state && state.venue) txt($("foot"), foot(state));
  }
  // Parsed defensively: text only, JSON only; heartbeats and the ticker for this product only (acks, errors and
  // any new kind of message are ignored, as Coinbase asks); a price is a positive decimal. Newest is the last to
  // arrive (one socket delivers in order) and how fresh it is is when it arrived here (cb.at): Coinbase's own stamp
  // never decides either, so a message stamped in the future (a clock off at either end) can't hold back the ones
  // after it. The stamp only places a price in its 2h window for the forming candle, when it's near the arrival time
  function cbMessage(raw) {
    if (typeof raw !== "string" || raw.length > 65536) return;
    let m;
    try { m = JSON.parse(raw); } catch { return; }
    if (!m || typeof m !== "object" || !Array.isArray(m.events)) return;
    const now = Date.now();
    if (m.channel === "heartbeats") { cb.beat = now; return; }
    if (m.channel !== "ticker") return;
    const ts = cbTime(m.timestamp);
    let p = NaN, upd = false;
    m.events.forEach((e) => (e && Array.isArray(e.tickers) ? e.tickers : []).forEach((k) => {
      const x = !k || k.product_id !== cb.sub ? NaN : typeof k.price === "number" ? k.price   // Coinbase sends "80835": plain decimals only
        : typeof k.price === "string" && /^\d+(\.\d+)?$/.test(k.price) ? Number(k.price) : NaN;
      if (x > 0 && isFinite(x)) { p = x; upd = upd || e.type === "update"; }
    }));
    if (!(p > 0)) return;
    if (cbClosed(cb.venue, now)) { cb.quiet = true; return; }   // closed: nothing on screen uses it
    // after a quiet spell or a closure (or past the end of one the run found), a snapshot is only the price from before
    // it: it takes an update, a trade or the book moving, to count as live again
    if (!upd && (cb.quiet || (cb.venue && cb.venue.closed_now))) return;
    cb.quiet = false; cb.price = p; cb.at = now;
    cbSeen(p, ts && Math.abs(ts - now) <= CB_SKEW ? ts : now);
    cbPaint();
  }
  // Coinbase's prices this page has seen, by 2h window (its open -> {o, h, l, c, at}, at = the newest one's time): what
  // carries BTC's forming candle on between runs (formingBars). Only the ticker's prices as they come, never Coinbase's
  // candles channel (measured incomplete); the newest three windows are kept
  function cbSeen(p, t) {
    const w = Math.floor(t / cb.barMs) * cb.barMs, x = cb.bars.get(w);
    if (x) { x.h = Math.max(x.h, p); x.l = Math.min(x.l, p); x.c = p; x.at = t; return; }
    cb.bars.set(w, { o: p, h: p, l: p, c: p, at: t });
    [...cb.bars.keys()].sort((a, b) => b - a).slice(3).forEach((k) => cb.bars.delete(k));
  }
  function cbPaint(now = false) {   // now: where the price comes from changed, drawn at once rather than within a second
    if (cb.due && !now) return;
    clearTimeout(cb.due);
    cb.due = setTimeout(() => {
      cb.due = 0; cb.painted = Date.now();
      if (!base) return;
      if (!base.venue) { renderCoins(); return; }   // another tab: only the BTC card's price moves
      // the same Coinbase price as drawn (the book moved, no trade) and no new allMids: only the price's time is new
      if (state && drawn.source === "coinbase" && livePrice(base).source === "coinbase" && cb.price === drawn.price && live.at === drawn.mids) {
        state.livePx.at = cb.at; txt($("foot"), foot(state));
      } else render(true);                          // only what the price moves; render says when its source changed
      if (state.livePx.source === "coinbase" && (!told || told.priceAt !== cb.at)) ticked();   // a new Coinbase message
    }, now ? 0 : Math.min(CB_PAINT, Math.max(0, cb.painted + CB_PAINT - Date.now())));   // (a clock set back: not later)
  }
  document.addEventListener("visibilitychange", () => { if (document.hidden) cbClose(); else if (base) render(); });

  // A bank re-marked at live mids: its P&L, the goal, and a point on the equity curve.
  function rebank(s, bank) {
    const t = s.ticker, g = s.goal, fl = s.flows;
    t.bank = bank; t.pnl = bank - g.start; t.pnl_pct = bank / g.start - 1;
    if (fl) { t.pnl = bank - fl.start - fl.net; t.pnl_pct = bank / fl.units - 1; }   // deposits aren't profit: the NAV's
    g.equity = bank;
    g.progress = Math.max(0, Math.min(1, Math.log(bank / g.start) / Math.log(g.target / g.start)));
    g.multiple_left = g.target / bank;
    if (s.equity && s.equity.length) s.equity.push([Date.now(), bank]);
  }

  // The basket's bank at live mids, perp-style: cash (the realised balance) plus each coin's quantity x (mid - average
  // entry). A coin with no mid keeps the board's mark.
  function basketLive(cash, holdings) {
    let u = 0, cost = 0, held = 0;
    holdings.forEach((h) => {
      const mid = parseFloat(live.mids[h.coin]);
      if (mid) { h.mark = mid; h.notional = h.qty * mid; h.unrealised = h.qty * (mid - h.avg_entry); }
      u += h.qty * (h.mark - h.avg_entry); cost += h.qty * h.avg_entry; held += h.qty * h.mark;
    });
    return { bank: cash + u, u, cost, held };
  }

  function withLiveBasket(s) {
    if (!live.at || s.ticker.cash == null) return s;
    const t = s.ticker, m = basketLive(t.cash, s.basket.holdings);
    if (!(m.bank > 0)) return s;
    s.basket.holdings.forEach((h) => { h.weight = h.qty * h.mark / m.bank; });
    t.unrealised = m.u; t.notional = m.held; t.exposure = m.held / m.bank; t.hold_pct = m.cost ? m.u / m.cost : 0;
    rebank(s, m.bank);
    return s;
  }

  // Overlay live prices on the last published snapshot: price, unrealised P&L, balance, forming candle (BTC on
  // Coinbase: the run's own forming candle carried on with Coinbase's feed, formingBars, and a live price marker).
  function withLive(snap) {
    if (!snap) return snap;
    const s = JSON.parse(JSON.stringify(snap));
    if (s.header.kind === "basket") return withLiveBasket(s);
    const lp = s.livePx = livePrice(s);   // Coinbase closed: no price, so the board's own, nothing re-marked
    if (s.venue) formingBars(s, Date.now());
    const mid = lp.price;
    if (!mid) return s;
    if (lp.source === "coinbase") s.chart.live = mid;
    const t = s.ticker;
    t.price = mid; s.analysis.stats.price = mid;
    const p = t.pos, m = remark(t.cash, p, mid);
    if (m) {
      t.hold_pct = p ? m.u / (p.qty * p.entry) : 0;
      rebank(s, m.bank);
    }
    const c = live.candle, ch = s.chart;   // never Hyperliquid's forming candle on a Coinbase chart
    if (c && !s.venue && live.candleCoin === (s.header.coin || "BTC") && ch.candles.length && c[0] > ch.candles[ch.candles.length - 1][0]) {
      c[4] = mid; c[2] = Math.max(c[2], mid); c[3] = Math.min(c[3], mid);
      ch.candles.push(c);
      const line = (s.analysis.outlook && s.analysis.outlook[0]) ? s.analysis.outlook[0].level : null;
      const short = s.analysis.outlook_side === "short";
      ch.ema_fast.push(null); ch.ema_slow.push(null);
      ch.don_lo.push(short ? line : null); ch.don_hi.push(short ? null : line);
    }
    return s;
  }

  // A desk account's bank re-marked at `price`: its cash (the realised balance) plus the open position's move. The tab
  // (withLive) and its card in the tab row both use this, so they agree. null: nothing to re-mark with
  function remark(cash, pos, price) {
    if (cash == null || !(price > 0)) return null;
    const u = pos ? (pos.side === "long" ? 1 : -1) * pos.qty * (price - pos.entry) : 0;
    return cash + u > 0 ? { bank: cash + u, u } : null;
  }
  // index.json's BTC row as a venue livePrice reads: Coinbase's product, closed now (and until when), its next closure
  const rowVenue = (c) => c.product ? { name: c.venue, product: c.product, closed_now: c.closed_now || null,
    session: { next_closure: c.next_closure || null } } : null;
  // A desk account's card, priced as its own tab would be right now, so it reads the same whichever tab is open: ETH and
  // SOL at Hyperliquid's mid; BTC on Coinbase by livePrice on its venue (Coinbase's own price while the feed is fresh,
  // else Hyperliquid's as the proxy, the board's price while Coinbase is closed). The re-mark is the tab's (remark).
  // {bank, price, lp}: the board's bank and price when there's no live price (or an index from before cash was in it)
  function cardMark(c) {
    const lp = c.venue === "coinbase" ? livePrice({ venue: rowVenue(c) || cb.venue, header: { coin: c.coin } })
      : { source: "hyperliquid", price: live.at ? parseFloat(live.mids[c.coin]) : NaN };
    const price = lp.source && lp.price > 0 ? lp.price : NaN, m = remark(c.cash, c.pos, price);
    return { bank: m ? m.bank : c.bank, price: price > 0 ? price : c.price, lp };
  }
  // The BTC card's mark for whose price it shows (its tab's ticker and footer say it in full), from the same livePrice
  // call as the price: [word, the long form for its title, class]. Short enough for a 320 px phone's card
  function srcMark(lp) {
    if (!lp) return null;
    if (lp.closed) return ["closed", `Coinbase is closed (${lp.closed.why}): the last run's price`, "closed"];
    if (lp.source === "coinbase") return ["cb", "Coinbase's own price, from its live feed", "cb"];
    if (lp.source === "hyperliquid" && lp.price > 0) {
      return lp.stale ? ["hl stale", "Hyperliquid's BTC, a proxy for Coinbase's, and stale: no new answer for over 20 s", "stale"]
        : ["hl proxy", "Hyperliquid's BTC, a proxy for Coinbase's: no price from Coinbase's own feed just now", "proxy"];
    }
    return ["last run", "the last run's price: no live price yet", "run"];
  }

  // BTC on Coinbase: the candle still forming, drawn as the chart's newest. The run's own (board chart.forming, read
  // from Coinbase at chart.forming_as_of; the desk never decides on it), carried on with Coinbase's prices this page has
  // seen in its window since the run: the close the newest of them, the high and low widened by each. The window after
  // it, once it has begun, is drawn from those prices alone; a window after one the page saw nothing of isn't drawn
  // (the chart's slots are candles in a row). Each is provisional (chart.prov, drawn dashed): the page sees no price
  // from before it opened or while it was hidden, so a high or a low since the run can be missing. So is the run's own
  // once its window has closed (the run saw only part of it). The next run's board brings the desk's own candle. Never
  // Coinbase's candles channel, never Hyperliquid's candle on this chart. chart.provFeed: some of it is the feed's;
  // chart.provRun: the run's own is provisional only because its window has closed
  function formingBars(s, now) {
    const ch = s.chart, bar = ch.bar_ms, cs = ch.candles;
    ch.prov = []; ch.provFeed = false; ch.provRun = false;
    if (!bar || !cs.length) return;
    const line = (s.analysis.outlook && s.analysis.outlook[0]) ? s.analysis.outlook[0].level : null, short = s.analysis.outlook_side === "short";
    const add = (c, prov) => {
      cs.push(c); ch.ema_fast.push(null); ch.ema_slow.push(null);
      ch.don_lo.push(short ? line : null); ch.don_hi.push(short ? null : line);
      if (prov) ch.prov.push(cs.length - 1);
    };
    let next = cs[cs.length - 1][0] + bar;   // the window the chart's next slot is for (a closure has no candles)
    const f = Array.isArray(ch.forming) && ch.forming.length === 5 && ch.forming[0] >= next ? ch.forming.slice() : null;
    if (f) {
      const x = cb.bars.get(f[0]), after = !!x && x.at > (ch.forming_as_of || 0);
      if (after) { f[2] = Math.max(f[2], x.h); f[3] = Math.min(f[3], x.l); f[4] = x.c; ch.provFeed = true; }
      ch.provRun = !after && f[0] + bar <= now;
      add(f, after || ch.provRun);
      next = f[0] + bar;
    }
    for (let x = cb.bars.get(next); x; x = cb.bars.get(next += bar)) { add([next, x.o, x.h, x.l, x.c], true); ch.provFeed = true; }
  }

  function renderCoins() {
    const nav = $("coins");
    if (!index || !index.coins) { nav.hidden = true; return; }
    nav.hidden = false;
    let start = 0, bank = 0, net = 0;
    const cards = index.coins.map((c) => {
      if (c.error) return `<button class="coin err" data-coin="${esc(c.coin)}"><span class="c-name">${esc(c.coin)}</span><span class="c-sub">error: ${esc(c.error)}</span></button>`;
      const basket = c.kind === "basket";
      const open = c.coin === (state && state.header.coin) && state.ticker.bank != null;
      // its tab open: what the tab shows; otherwise re-marked here by the tab's own rules (cardMark), every card and
      // so the ALL total moving with the live prices whichever tab is open
      const mk = basket ? null : cardMark(c);
      let b = open ? state.ticker.bank : basket ? c.bank : mk.bank;
      if (basket && !open && live.at && c.holdings) {   // the basket's tab isn't open: re-mark it from the same mids
        const m = basketLive(c.cash, JSON.parse(JSON.stringify(c.holdings)));
        if (m.bank > 0) b = m.bank;
      }
      start += c.start; bank += b; net += c.net_flows || 0;
      if (c.venue === "coinbase" && !basket && !open) cb.mode = cbMode(mk.lp);   // cbWatch redraws the card when it changes
      const label = basket ? (c.holdings || []).map((h) => h.coin).join("·") : open ? px(state.ticker.price) : px(mk.price);
      const src = c.venue === "coinbase" && !basket ? srcMark(open ? state.livePx : mk.lp) : null;   // BTC: whose price it is
      const sub = basket ? " · basket" : ` · risk ${(c.risk_per_trade * 100).toFixed(0)}%${c.shorts ? " · +shorts" : ""}${c.venue ? ` · ${esc(c.venue)}` : ""}`;
      const made = c.units != null ? b - c.start - (c.net_flows || 0) : b - c.start;
      const pnlCls = b == null || c.start == null ? "" : tone(made);
      return `<button class="coin ${c.coin === coin ? "on" : ""}" data-coin="${esc(c.coin)}" aria-pressed="${c.coin === coin}">` +
        `<span class="c-name">${esc(c.coin)}<i${src ? ` class="c-px" title="${esc(src[1])}"` : ""}>` +
        `${src ? `<b class="c-src ${src[2]}">${src[0]}</b>` : ""}${esc(label)}</i></span><span class="c-bank ${pnlCls}">${usd(b)}</span>` +
        `<span class="c-sub">${c.units ? `${pct(b / c.units - 1)} perf · P&amp;L ${usd(b - c.start - c.net_flows, 0)}` : pct(b / c.start - 1)} · ${esc(c.position)} · ${esc(c.record)}${sub}</span></button>`;
    });
    const left = index.coins.filter((c) => c.error).map((c) => c.coin);   // no bank this run: not in the total
    const putIn = start + net;   // with deposits: the P&L on everything put in, never counting the deposits as profit
    const allCls = (net ? putIn > 0 : !!start) ? tone(net ? bank - putIn : bank - start) : "";
    cards.push(`<div class="coin total"><span class="c-name">ALL</span><span class="c-bank ${allCls}">${usd(bank)}</span>` +
      (net ? `<span class="c-sub">${pct(putIn > 0 ? (bank - putIn) / putIn : 0)} · P&amp;L ${usd(bank - putIn)} on ${usd(putIn, 0)} put in` :
       `<span class="c-sub">${pct(start ? bank / start - 1 : 0)} on ${usd(start, 0)} paper`) +
      `${left.length ? ` · without ${esc(left.join(", "))}` : ""}</span></div>`);
    nav.style.setProperty("--tabs", cards.length);
    // the same cards as drawn: only what changed, in place, so a tap isn't lost to a redraw (once a second with
    // Coinbase's feed). Taps are handled once, on the row
    const html = cards.join("");
    if (nav._html === html) return;
    nav._html = html;
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const fresh = [...tpl.content.children], old = [...nav.children];
    if (fresh.length !== old.length || fresh.some((el, k) => el.tagName !== old[k].tagName || el.dataset.coin !== old[k].dataset.coin)) {
      nav.innerHTML = html; return;
    }
    fresh.forEach((el, k) => {
      const o = old[k];
      [...o.attributes].forEach((a) => { if (!el.hasAttribute(a.name)) o.removeAttribute(a.name); });
      [...el.attributes].forEach((a) => { if (o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value); });
      if (o.innerHTML !== el.innerHTML) o.innerHTML = el.innerHTML;
    });
  }
  $("coins").addEventListener("click", (e) => {   // a card only: not the ALL total, a gap, or the page around the row
    const b = e.target instanceof Element && e.target.closest(".coin[data-coin]");
    if (b && $("coins").contains(b)) show(b.dataset.coin);
  });

  // An account with no board this run: why, in full and wrapped (its tab's line is cut short), above the board shown
  function notice() {
    let el = $("acct-err");
    if (!el) {
      el = document.createElement("div"); el.id = "acct-err"; el.className = "panel acct-err"; el.setAttribute("role", "status");
      $("coins").after(el);
    }
    const row = missing && index && index.coins ? index.coins.find((c) => c.coin === missing) : null;
    el.hidden = !(row && row.error);
    if (row && row.error) el.textContent = `${missing}: no board this run, its step failed. ${row.error}`;
  }

  function show(c) {
    coin = c; storeSet("desk-coin", coin); history.replaceState(null, "", "#" + coin); missing = null;
    live.candle = null; src = `boards/${coin}.json`; tick().then(pollCandle);   // the candle for the new tab's board
  }
  // A new #COIN typed or linked in an open tab (same page, so no reload) switches to that account too.
  window.addEventListener("hashchange", () => {
    const c = location.hash.slice(1).toUpperCase();
    if (ACCOUNTS.includes(c) && c !== coin) show(c);
  });

  // light: a redraw for a new price only (Coinbase's, once a second): what a price moves, not the agents, their
  // bubbles, the P&L bars, the rules or the switches, which only a new board changes. drawn: what the last one showed
  const drawn = { bank: null, price: null, source: null, mids: 0 };
  function render(light = false) {
    state = withLive(base);
    if (!state) return;
    const s = state, hdr = s.header, lp = s.livePx || {};
    // Coinbase's product and closures for the socket and the BTC card: the BTC board's while it's open, else the
    // index's row for it (the same run), so a page opened straight on another tab prices the BTC card by the same rules
    const cbRow = !s.venue && index && index.coins ? index.coins.find((c) => c.venue === "coinbase" && !c.error && c.product) : null;
    const was = cb.product;
    if (s.venue && s.venue.name === "coinbase" && s.venue.product) { cb.product = s.venue.product; cb.venue = s.venue; cb.barMs = s.chart.bar_ms || cb.barMs; }
    else if (cbRow) { cb.product = cbRow.product; cb.venue = rowVenue(cbRow); }
    if (was && was !== cb.product) cb.bars.clear();   // another product's prices aren't this one's candle
    cbSync();
    txt($("title"), hdr.title);
    txt($("tagline"), hdr.tagline);
    txt($("mode"), hdr.mode); $("mode").className = `mode ${hdr.mode.toLowerCase()}`;
    txt($("daydate"), `DAY ${hdr.day} · ${MT.day(s.updated).toUpperCase()}`);   // the run's time and date, in MT
    txt($("time"), MT.clock(s.updated));
    txt($("tz"), TZ);
    const basket = hdr.kind === "basket";
    document.body.classList.toggle("kind-basket", basket);
    document.body.dataset.coin = String(hdr.coin || "BTC").toUpperCase();
    document.body.classList.toggle("has-live", s.venue ? lp.source === "coinbase" || (lp.source === "hyperliquid" && !!live.at && !lp.stale)
      : !!live.at);   // Coinbase closed, or the proxy stale: nothing live
    renderCoins(); notice();
    if (s.venue) cb.mode = cbMode(lp);
    if (basket) renderBasket(s);
    else { if (!light) renderAgents(s.agents); renderStage(s); if (!light) renderBubbles(s.agents); drawChart(s.chart); drawWeb(); }
    renderBalance(s);
    if (!light || s.ticker.bank !== drawn.bank) { drawn.bank = s.ticker.bank; drawEquity(s.equity, s.flows ? s.flows.marks : []); }   // flat: the same curve
    if (!light) drawPnlBars(s.pnl_bars);
    renderAnalysis(s, light);
    if (!light) { if (basket) renderRules(s.basket.rules); else renderThresholds(s.thresholds); renderKills(s.kills); }
    txt($("foot"), foot(s));
    drawn.price = s.ticker.price; drawn.source = lp.source; drawn.mids = live.at;
    if (staticMode) {
      document.querySelectorAll("[data-kill]").forEach((b) => { b.disabled = true; });
      txt($("kill-hint"), "read-only board: flip switches in ledger/<COIN>/kills.json on GitHub");
    }
    const n = liveNow();   // where the price comes from changed: the listeners hear it with this redraw, not later
    if (told && told.coin === hdr.coin && (told.source !== n.source || told.by !== (n.closed ? n.closed.by : null))) ticked();
  }
  function foot(s) {
    const hdr = s.header, lp = s.livePx || {};
    const age = Math.round((Date.now() - s.updated) / 60000), secs = (t) => Math.max(0, Math.round((Date.now() - t) / 1000));
    const cn = lp.closed;   // BTC on Coinbase: its own feed, else Hyperliquid's as a proxy, else closed (and why)
    return `desk updated ${MT.when(s.updated)} ${TZ} (${age} min ago)` +
      (s.venue && cn ? (cn.by === "schedule" ? ` · Coinbase is closed now by its schedule (${cn.why}, until ${hhmm(cn.until)} ${TZ}): no live price`
        : ` · Coinbase was closed at the last run (${cn.why}): no live price until a run finds it open` +
          (cn.scheduled && cn.until ? ` or, after ${hhmm(cn.until)} ${TZ}, its own feed does` : ""))
      : s.venue && lp.source === "coinbase" ? ` · live price ${secs(lp.at)}s ago from Coinbase`
      : s.venue && lp.stale ? ` · stale price: Hyperliquid's (a proxy for Coinbase's) last came ${secs(live.at)}s ago, and nothing from Coinbase's own feed just now`
      : live.at ? ` · live price ${secs(live.at)}s ago from Hyperliquid` +
        (s.venue ? ` (a proxy: no price from Coinbase's own feed just now; the desk marks at Coinbase's price every run)` : "") : "") +
      provNote(s.chart) +
      (missing ? ` · no ${missing} board this run (its step failed), showing ${hdr.coin || "BTC"}` : "") +
      ` · not financial advice; the desk can and will lose money`;
  }

  // BTC's provisional newest candle(s) (formingBars), as the footer says it: from Coinbase's live feed, or the run's own
  // whose window has closed since; either way the next run's board brings the desk's own
  function provNote(ch) {
    const n = ch.prov ? ch.prov.length : 0, run = ch.provRun ? `the desk's as of ${hhmm(ch.forming_as_of)} ${TZ}, before its window closed` : "";
    if (!n) return "";
    const from = [ch.provFeed ? "from Coinbase's live feed" : "", run].filter(Boolean).join(" and ");
    return n > 1 ? ` · newest ${n} candles: provisional, ${from}; the desk's own candles replace them at the next run`
      : ` · newest candle: provisional, ${from}; the desk's own candle replaces it at the next run`;
  }

  // Live server: /api/state every 2s. Static hosting (GitHub Pages): boards/<COIN>.json every 60s.
  const params = new URLSearchParams(location.search);
  let src = params.get("state") || "api/state";
  let staticMode = !!params.get("state");
  let missing = null;   // an account with no board this run: its step failed, and the workflow publishes no stale one
  let asked = 0;        // a tab clicked while another board is still loading: only the newest request is shown
  async function tick() {
    const mine = ++asked;
    try {
      const url = staticMode && src !== "api/state" ? src.split("?")[0] + `?t=${Date.now()}` : src;
      let res = await fetch(url, { cache: "no-store" });
      if (!res.ok && src === "api/state" && res.status !== 503) {
        staticMode = true; src = `boards/${coin}.json`;
        res = await fetch(`${src}?t=${Date.now()}`, { cache: "no-store" });
      }
      if (mine !== asked) return;
      if (res.status === 404 && staticMode && src === `boards/${coin}.json`) {
        // no board for this account this run (its step failed: a rate limit, a deposits.json mistake): show another
        // account's (BTC's if it has one) and the tab row, with why, never a blank page; the remembered tab stays
        const ir = await fetch(`boards/index.json?t=${Date.now()}`, { cache: "no-store" }).catch(() => null);
        const idx0 = ir && ir.ok ? await ir.json().catch(() => null) : null;
        if (mine !== asked) return;
        const ok = ((idx0 && idx0.coins) || []).filter((c) => !c.error && c.coin !== coin).map((c) => c.coin);
        const alt = ok.includes("BTC") ? "BTC" : ok[0] || (!idx0 && coin !== "BTC" ? "BTC" : null);
        if (alt) {
          missing = coin; coin = alt; src = `boards/${coin}.json`;
          res = await fetch(`${src}?t=${Date.now()}`, { cache: "no-store" });
        } else if (idx0) {      // no account has a board this run (deposits.json isn't valid JSON, say): the tabs, and why
          missing = coin; index = idx0; renderCoins(); notice();
          $("foot").textContent = "no account has a board this run: every step failed (why: above). the next good run brings them back";
          return;
        }
      }
      if (!res.ok && src === "boards/BTC.json") {            // an older deploy with a single BTC board
        src = "board.json"; res = await fetch(`${src}?t=${Date.now()}`, { cache: "no-store" });
      }
      if (!res.ok) throw new Error(res.status);
      const board = await res.json();
      let idx = index;
      if (staticMode) {
        const ir = await fetch(`boards/index.json?t=${Date.now()}`, { cache: "no-store" }).catch(() => null);
        idx = ir && ir.ok ? await ir.json() : null;
      }
      if (mine !== asked) return;
      base = board; index = idx;
      render();
      emit("desk:board", { updated: base.updated, scanned: base.analysis?.scanned, gates: base.analysis?.gates || [],
        agents: (base.agents || []).map((a) => a.key), static: staticMode, board: base, index: idx, live: tell(liveNow()) });
    } catch (err) {
      if (mine === asked) $("foot").textContent = `can't reach the desk (${err.message}). is \`python -m desk paper\` running?`;
    }
  }

  async function pollMids() {
    try {
      live.mids = await hl({ type: "allMids" }); live.at = Date.now();
      if (base && base.venue && livePrice(base).source === "coinbase") cbPaint();   // on Coinbase's feed: its next redraw takes these
      else { render(); if (!told || told.at !== live.at) ticked(); }   // (render has told a change of source already)
    } catch { /* keep the last price */ }
  }
  async function pollCandle() {
    const c = (base && base.header.coin) || coin, iv = (base && base.header.interval) || "2h";
    const ms = (base && base.chart.bar_ms) || 7200000, now = Date.now();
    if ((base && base.header.kind === "basket") || c === "VT") { live.candle = null; return; }   // no one candle to draw
    try {
      const rows = await hl({ type: "candleSnapshot", req: { coin: c, interval: iv, startTime: now - 2 * ms, endTime: now } });
      const f = rows.find((r) => r.T >= now);
      live.candle = f ? [f.t, +f.o, +f.h, +f.l, +f.c] : null; live.candleCoin = c;   // whose candle it is
      render();
    } catch { /* the chart just shows closed candles */ }
  }

  // a resize (a phone turned on its side) redraws everything at the new size, the equity curve and the P&L bars too:
  // a full render, not the light one a price takes (the event isn't passed on as `light`), once a frame at most
  let resized = 0;
  window.addEventListener("resize", () => { if (!resized) resized = requestAnimationFrame(() => { resized = 0; render(); }); });
  window.addEventListener("desk:theme", () => { $("agents").textContent = ""; $("chief-sprite").textContent = ""; render(); });
  (async function loop() {
    await tick();
    setTimeout(loop, staticMode ? 60000 : 2000);
  })();
  pollMids(); pollCandle();
  setInterval(pollMids, 5000);
  setInterval(pollCandle, 30000);
})();
