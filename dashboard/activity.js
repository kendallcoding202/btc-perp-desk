// The desk monitor (under the account tabs) and the desk log (after the chart, or the VT basket).
//
// Everything that moves here is tied to one of four real things. Nothing moves on a timer of its own:
//   (a) the last desk run: its board (boards/<ACC>.json, built by desk/board.py), replayed in the order the desk
//       ran its steps, always labelled "REPLAY · LAST RUN hh:mm UTC";
//   (b) a live price, fetched in this browser by app.js ("desk:tick", which names its source): Hyperliquid's allMids
//       every 5 s; on BTC's Coinbase tab, Coinbase's own feed while it's fresh (about once a second), else
//       Hyperliquid's BTC as a proxy, named as one, and none while Coinbase is closed (as the last run found it, or
//       by the schedule on the board);
//   (c) app.js's re-read of the board every 60 s ("desk:board");
//   (d) the clock: the countdown to the next close, and how old the board is.
// The desk's rules are fixed in desk/thresholds.py. This page shows what the last run did; it changes nothing.
// Computed here, and only here: the live distance to the line or the stop, the live P&L, the EMAs if the bar
// closed at the live price, and the re-audit's dates (research/edge-hunt-2026-10/REAUDIT.md's schedule).
// It reads app.js's two events, the page, and the browser's own timing entries for requests app.js already made.
// Its one request is research.json, from this same site. It never writes to the board it is handed (app.js's
// own copy). No animation-frame loop: CSS opacity and transform, a short setTimeout chain per replay, and a 1 Hz
// clock that runs only while a panel is on screen and the page is visible. Reduced motion: no motion at all.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const nav = $("coins"), basketEl = $("basket");
  if (!nav || !basketEl) return;

  // ---- facts copied from the repo (tests/test_activity_copy.py pins each one) ------------------------------
  const STALE_STATIC = 150 * 60000, STALE_LIVE = 5 * 60000;   // spider.js: past this, the desk is not running
  const CRON = "4,19,34,49 * * * *";                            // .github/workflows/paper.yml's schedule
  const HL_INFO = "https://api.hyperliquid.xyz/info";           // app.js: allMids every 5 s, candles every 30 s
  // desk/filter.py's ORDER, gate by gate. A gate it doesn't list goes by its owner
  const PHASES = ["KILLS", "DATA", "HARD", "SCAN", "SOFT", "SIZE", "JEV"];
  const PHASE = {
    drawdown_halt: "KILLS", daily_loss: "KILLS", cooldown: "KILLS", manual_kill: "KILLS", flat: "KILLS",
    history_bars: "DATA", data_age_bars: "DATA",
    atr_pct_min: "HARD", atr_pct_max: "HARD", abs_funding_apr: "HARD", spread_bps: "HARD", depth_usd: "HARD",
    setup: "SCAN",
    trend_strength: "SOFT", efficiency: "SOFT", momentum_already_spent: "SOFT", crowding: "SOFT",
    ticket_usd: "SIZE",
    // BTC on Coinbase (desk/venue.py): the session and a known funding rate with HARD, whole contracts and margin with SIZE
    session: "HARD", funding_known: "HARD", desk_size_usd: "SIZE", contracts: "SIZE", margin: "SIZE",
  };
  const OWNER_PHASE = { risk: "KILLS", chief: "KILLS", crawler: "DATA", jev: "JEV" };
  const phaseOf = (g) => PHASE[g.name] || OWNER_PHASE[g.owner] || "HARD";
  const TRADE = /\b(BOUGHT|SOLD|SHORTED|COVERED|ADDED|TRIMMED|HALT|flattened)\b/;
  const NO_TICK = 20000;          // no live price for this long: say so
  const NO_READ_STATIC = 130000, NO_READ_LIVE = 15000;   // app.js re-reads every 60 s (a live server: 2 s)
  const NO_FIRST = 15000;         // no board this long after load: say the page can't load one
  const DAY = 86400000;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const wide = matchMedia("(min-width: 980px)");
  const narrow = matchMedia("(max-width: 420px)");
  // filter.py's KILLS: a brake that holds every new entry, whatever the price does (desk/engine.py kill_gates)
  const BRAKE = {
    drawdown_halt: ["drawdown halt", "until a human resets it"],
    daily_loss: ["daily loss lock", "until the next UTC day"],
    cooldown: ["losing-streak cooldown", "until it ends"],
    flat_kill: ["flat kill switch on", "until it is switched off"],
    entry_kill: ["entry kill switch on", "until it is switched off"],
  };

  // ---- formatting ----------------------------------------------------------------------------------------
  const fix = (x, dp) => Number(x).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  // whole dollars at or above $1,000, cents below
  const usd = (x, dp) => x == null || !isFinite(x) ? "—" : (x < 0 ? "−$" : "$") + fix(Math.abs(x), dp ?? (Math.abs(x) >= 1000 ? 0 : 2));
  const mid$ = (x) => x == null || !isFinite(x) ? "—" : "$" + Number(x).toLocaleString("en-US", { maximumFractionDigits: 6 });
  const pct = (x, dp = 2) => x == null || !isFinite(x) ? "—" : `${x < 0 ? "−" : "+"}${Math.abs(x * 100).toFixed(dp)}%`;
  const n2 = (x) => x == null || !isFinite(x) ? "—" : Math.abs(x) >= 1000 ? Math.round(x).toLocaleString("en-US") : fix(x, 2);
  const iso = (ms) => new Date(ms).toISOString();
  const hhmm = (ms) => iso(ms).slice(11, 16), hms = (ms) => iso(ms).slice(11, 19), ymd = (ms) => iso(ms).slice(0, 10);
  const clock = (ms) => {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };
  const ago = (ms) => { const m = Math.max(0, Math.floor(ms / 60000)); return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m} min`; };
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const last = (xs) => (xs && xs.length ? xs[xs.length - 1] : null);
  const set = (el, text) => { if (el && el.textContent !== text) el.textContent = text; };
  const word = (v, g) => (v === "SKIP" ? (g && g.value != null ? "info" : "skip") : v);

  // ---- the two panels, with room kept for them before any data, so nothing below shifts -------------------
  // index.html holds their empty shells, so their room is there from the first paint; made here if it doesn't
  const mon = $("monitor") || document.createElement("section");
  mon.className = "panel act-monitor act-wait";      // act-wait: no board yet, nothing to show but that
  mon.id = "monitor";
  mon.setAttribute("aria-label", "desk monitor: the last desk run, replayed, and live prices");
  mon.innerHTML = `
    <div class="act-status"><span class="act-pill" data-k="mode">—</span><span class="act-dot act-stale" data-k="dot"></span><span class="act-st"><span data-k="st">waiting for the first board…</span><span class="act-xp" data-k="exp"></span></span><button type="button" class="act-jump" data-k="jump" aria-label="jump to the desk log"><span class="act-jw">DESK </span>LOG ↓</button></div>
    <div class="act-err" data-k="err" hidden></div>
    <div class="act-meter desk-only" data-k="meter">
      <div class="act-big"><b data-k="pct">—</b> <span data-k="to"></span></div>
      <div class="act-scale"><span class="act-end" data-k="end0">LINE</span><div class="act-track"><i class="act-mk" data-k="mk"></i></div><span class="act-end">5+ ATR</span>
        <div class="act-ticks" data-k="ticks"><span>LINE</span><span>1</span><span>2</span><span>3</span><span>4</span><span>5+ ATR</span></div></div>
      <div class="act-cap" data-k="cap"></div>
    </div>
    <div class="act-vtl basket-only" data-k="vtl"><div></div><div></div><div></div><div class="act-cap"></div></div>
    <div class="act-strip">
      <div class="act-sh"><span data-k="rl">REPLAY · LAST RUN</span><span class="act-cnt" data-k="cnt"></span></div>
      <div class="act-sb"><div class="act-sbody"><div class="act-nodes" data-k="nodes"></div><div class="act-read" data-k="read"></div></div>
        <button type="button" class="act-play" data-k="play" aria-label="replay the last run's checks" title="replay the last run's checks">▶</button></div>
    </div>
    <div class="act-next"><span data-k="next1"></span> <span data-k="next2"></span></div>`;
  if (!mon.isConnected) nav.after(mon);

  const log = $("desklog") || document.createElement("section");
  log.className = "panel act-log act-wait";
  log.id = "desklog";
  log.setAttribute("aria-label", "desk log: the last desk run, step by step, and what is live");
  log.innerHTML = `
    <div class="panel-head"><span>DESK LOG <span class="act-pill" data-k="lmode">—</span></span><span class="act-lrun" data-k="lrun">—</span></div>
    <div class="act-lbody">
      <div class="act-replay">
        <div class="act-rh"><span data-k="lrl">REPLAY · LAST RUN · steps in the order the desk ran them</span>
          <button type="button" class="act-play" data-k="lplay" aria-label="replay the last run's steps" title="replay the last run's steps">▶</button></div>
        <button type="button" class="act-more" data-k="more" hidden aria-expanded="false"></button>
        <ol class="act-lines act-short" data-k="lines"></ol>
      </div>
      <div class="act-side">
        <div class="act-live" data-k="live"></div>
        <div class="act-tape" data-k="tape" aria-live="off"><b>TAPE · this page's own requests, as they land</b><div></div><div></div><div></div><div></div><div></div><div></div></div>
        <div class="act-beat" data-k="beat"></div>
        <div class="act-rsch" data-k="rsch"></div>
      </div>
    </div>
    <div class="act-foot" data-k="foot">PAPER · no real money · the rules are fixed in desk/thresholds.py and the desk does not change them · backtests are not promises</div>`;
  if (!log.isConnected) basketEl.after(log);

  const M = {}, L = {};
  mon.querySelectorAll("[data-k]").forEach((e) => { M[e.dataset.k] = e; });
  log.querySelectorAll("[data-k]").forEach((e) => { L[e.dataset.k] = e; });

  // ---- what this page knows ---------------------------------------------------------------------------------
  let B = null, IDX = null, staticBoard = true;   // detail.board (app.js's base: read only), detail.index, detail.static
  let shown = null, shownUpdated = null;     // the account on screen, and its board's run time
  let mids = null, midsAt = 0;               // the newest allMids answer, and when it came
  let tk = null;                             // the open tab's price as app.js last drew it: {coin, price, source, at, closed}
  const modes = {};                           // each account's header.mode, as seen
  let research = null;
  let lastRead = 0, visibleSince = Date.now(), unreadSaid = false;   // when a board last came in; the tab last shown
  const basket = () => !!(B && B.header && B.header.kind === "basket");
  const agentOf = (k) => (B.agents || []).find((a) => a.key === k) || null;
  const stale = (now) => !!B && now - B.updated > (staticBoard ? STALE_STATIC : STALE_LIVE);
  // this page has read no board for a while (a re-read failing, or the site down): it can't say the desk is idle
  const unread = (now) => !!B && now - Math.max(lastRead, visibleSince) > (staticBoard ? NO_READ_STATIC : NO_READ_LIVE);
  const modeWord = () => (B.header.mode || "paper").toLowerCase();
  // the brake holding new entries as of the last run, if any: [what, until]
  function brake() {
    const g = (B.analysis.gates || []).find((x) => phaseOf(x) === "KILLS" && x.verdict === "DROP");
    if (g) return BRAKE[g.name === "manual_kill" ? (g.note === "flat_kill" ? "flat_kill" : "entry_kill") : g.name] || [g.name.replace(/_/g, " "), "for now"];
    const k = B.kills || {};
    const key = k.flat_kill ? "flat_kill" : k.entry_kill ? "entry_kill" : k.halted ? "drawdown_halt" : k.daily_locked ? "daily_loss" : k.cooldown ? "cooldown" : null;
    return key ? BRAKE[key] : null;
  }
  const newestClose = () => basket() ? B.basket.last_close_ts
    : B.chart.candles.length ? last(B.chart.candles)[0] + B.chart.bar_ms : null;
  // the closes since the board's newest one (the next desk run decides each, in order), and the next to come
  function closes(now) {
    const bar = B.chart.bar_ms, c0 = newestClose();
    if (c0 == null) return { missed: 0, list: [], next: Math.ceil(now / bar) * bar };
    const k = Math.max(0, Math.floor((now - c0) / bar)), list = [];
    for (let j = 1; j <= Math.min(k, 3); j++) list.push(c0 + j * bar);
    return { missed: k, list, next: c0 + (k + 1) * bar };
  }
  // The board's breakout line for the close at ts. trigger_outlook lists the level for each close after the run,
  // stepping as old highs (lows) age out of the window, from the bars the run had. Exact for the first close after
  // the run; for a later one it is a bound, because a bar since then can only raise a high (lower a low). Past its
  // last entry the list says nothing for sure unless the window (rules.breakout_bars) still holds the run's bars
  // and the list wasn't cut at its 10 entries.
  function lineAt(ts) {
    const ol = B.analysis.outlook || [];
    if (!ol.length || !B.chart.candles.length) return null;
    let j = 0;
    ol.forEach((r, k) => { if (r.ts <= ts) j = k; });
    const n = B.rules && B.rules.breakout_bars, lastBar = last(B.chart.candles)[0], end = last(ol).ts;
    const out = { side: B.analysis.outlook_side === "short" ? "short" : "long", level: ol[j].level, bound: ts > ol[0].ts };
    const known = ts <= end || (n && ts <= lastBar + (n + 1) * B.chart.bar_ms && ol.length < 10);
    if (!known) out.level = null;
    return out;
  }

  // Live numbers for the open desk tab: the only arithmetic this page does on the board
  // BTC on Coinbase (board.venue): the live price is the one app.js shows, Coinbase's own (its feed) when the tick
  // says so, else Hyperliquid's BTC, a proxy; while Coinbase is closed (as the last run found it, or by the board's
  // schedule since) nothing is re-marked, as app.js does
  const venue = () => (B && B.venue) || null;
  const vName = () => { const v = venue(); return v ? v.name.charAt(0).toUpperCase() + v.name.slice(1) : "Hyperliquid"; };
  const feedTick = () => !!(tk && B && B.venue && tk.coin === B.header.coin && tk.source === "coinbase");
  function calc(now) {
    const b = B, coin = b.header.coin, ch = b.chart, t = b.ticker, st = b.analysis.stats;
    const mine = tk && tk.coin === coin ? tk : null;
    const shut = b.venue ? (b.venue.closed_now ? { ...b.venue.closed_now, by: "run" } : mine && mine.closed) || null : null;
    const feed = !shut && feedTick();
    const lv = shut ? NaN : feed ? mine.price : mids ? parseFloat(mids[coin]) : NaN, isLive = isFinite(lv) && lv > 0;
    const mid = isLive ? lv : t.price, cl = last(ch.candles), c = closes(now);
    const atrUsd = st.atr_pct != null && cl ? st.atr_pct / 100 * cl[4] : null;   // the ATR the run used, in $
    const o = { coin, mid, isLive, shut, cl, atrUsd, c, pos: t.pos, rules: b.rules || null, feed, at: feed ? mine.at : midsAt };
    o.brake = t.pos ? null : brake();     // flat and braked: no line is in play, whatever the price does
    if (t.pos) {
      const p = t.pos, sg = p.side === "long" ? 1 : -1;
      o.stopPct = p.stop / mid - 1;
      o.stopAtr = atrUsd ? Math.abs(mid - p.stop) / atrUsd : null;
      o.pnl = sg * p.qty * (mid - p.entry);
      o.through = sg * (mid - p.stop) <= 0;
    } else if (!o.brake) {
      const ln = lineAt(c.next);
      if (ln) {
        Object.assign(o, { side: ln.side, level: ln.level, bound: ln.bound });
        if (ln.level != null) {
          o.pct = ln.level / mid - 1;
          o.gap = Math.abs(ln.level - mid);
          o.atr = atrUsd ? o.gap / atrUsd : null;
          o.past = ln.side === "short" ? mid < ln.level : mid > ln.level;
        }
      }
    }
    // the EMAs if the forming bar closed at the live price: emaNext = prev + 2/(n+1) x (price - prev), as
    // desk/indicators.py ema() steps. Only for the close right after the run's newest one
    const ef = last(ch.ema_fast), es = last(ch.ema_slow), r = o.rules;
    if (r && isLive && !c.missed && !o.brake && ef != null && es != null) {
      o.ef = ef + 2 / (r.ema_fast + 1) * (mid - ef);
      o.es = es + 2 / (r.ema_slow + 1) * (mid - es);
    }
    return o;
  }

  // ---- timers: replays, flashes, the 1 Hz clock --------------------------------------------------------------
  function chain(steps, done) {       // [[ms, fn], ...]: each fn, then wait its ms
    let k = 0, t = 0, dead = false;
    const next = () => {
      if (dead) return;
      if (k >= steps.length) { t = 0; if (done) done(); return; }
      const [ms, fn] = steps[k++];
      fn();
      t = setTimeout(next, ms);
    };
    next();
    return { cancel() { dead = true; clearTimeout(t); } };
  }
  const runs = { strip: null, log: null };
  function stopRun(k) { if (runs[k]) { runs[k].cancel(); runs[k] = null; } }
  // a one-shot flash on a real change: a value that changed, a board read, a fill. The Web Animations API runs it
  // on its own, so no class juggling (a later className write can't cut it off) and no forced layout
  const FLASH = {
    value: [[{ filter: "brightness(1.9)" }, { filter: "none" }], 600],
    read: [[{ transform: "scale(1.7)", filter: "brightness(1.5)" }, { transform: "none", filter: "none" }], 600],
    fill: () => { const y = getComputedStyle(document.documentElement).getPropertyValue("--yellow").trim();
      return [[{ backgroundColor: y, transform: "scale(1.9)" }, { backgroundColor: y, transform: "none" }], 900]; },
  };
  const flashed = new WeakMap();      // with Coinbase's feed a value can change every second: it flashes at most every 3 s
  function flash(el, kind = "value") {
    if (!el || !el.animate || reduced.matches || document.hidden) return;
    if (kind === "value" && feedTick()) {
      const t = performance.now();
      if (t - (flashed.get(el) ?? -1e9) < 3000) return;
      flashed.set(el, t);
    }
    const [frames, ms] = typeof FLASH[kind] === "function" ? FLASH[kind]() : FLASH[kind];
    try { el.animate(frames, { duration: ms, easing: "ease-out" }); } catch { /* no flash: the value is still right */ }
  }

  let monOn = false, logOn = false, logPending = false, stripPending = false, sec = 0, secN = 0;
  const visible = () => !document.hidden;
  function clockRun() {
    const want = !!B && (monOn || logOn) && visible();
    if (want && !sec) { sec = setInterval(secondly, 1000); secondly(); }
    else if (!want && sec) { clearInterval(sec); sec = 0; }
  }
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((es) => {
      es.forEach((e) => {
        if (e.target === mon) { monOn = e.isIntersecting; mon.classList.toggle("act-off", !monOn); }
        else { logOn = e.isIntersecting; log.classList.toggle("act-off", !logOn); }
      });
      if (logOn) loadResearch();          // the research lines sit at the log's end: read them when it's in view
      if (monOn && stripPending) replayStrip();
      if (logOn && logPending) playLog();
      if (!monOn && runs.strip) finishStrip();
      clockRun();
    });
    io.observe(mon); io.observe(log);
  } else { monOn = logOn = true; setTimeout(loadResearch, 0); }

  // ---- monitor row 1: status ------------------------------------------------------------------------------
  // What the page knows: when the board it shows was built, and whether it can still read one. It can't know whether
  // the desk ran (a failed deploy or fetch looks the same), so a stale board says "no new board", not "no desk run".
  function status(now) {
    const b = B, old = stale(now), lost = unread(now), age = ago(now - b.updated).toUpperCase();
    const mode = b.header.mode || "PAPER";
    set(M.mode, mode); M.mode.classList.toggle("act-livemode", mode !== "PAPER");
    set(L.lmode, mode); L.lmode.classList.toggle("act-livemode", mode !== "PAPER");
    M.dot.classList.toggle("act-stale", old || lost);
    set(M.st, lost ? (narrow.matches ? `CAN'T READ THE BOARD SINCE ${hhmm(lastRead)}` : `CAN'T READ THE BOARD SINCE ${hhmm(lastRead)} UTC · SHOWING THE ONE BUILT ${hhmm(b.updated)} UTC`)
      : old ? `STALE · NO NEW BOARD FOR ${age}`      // the run's time is in the REPLAY label
      : `LAST RUN ${hhmm(b.updated)} UTC · ${age} AGO`);
    M.st.classList.toggle("act-warn", old || lost);
    set(M.exp, b.header.shorts && b.header.experiment ? "SHORTS: PAPER EXPERIMENT" : "");
    set(L.lrun, `${lost ? "CAN'T READ · " : old ? "STALE · " : ""}LAST RUN ${hhmm(b.updated)} UTC`);
    L.lrun.classList.toggle("act-warn", old || lost);
    if (lost && !unreadSaid) { unreadSaid = true; tapeAdd(`${hms(now)} no board read since ${hms(lastRead)} · showing the one built ${hms(b.updated)} UTC`, "act-warn"); beat(now); }
    // a step that failed this run: the asked-for tab (app.js shows BTC's board when one is missing), or this one
    const asked = location.hash.slice(1).toUpperCase();
    const bad = IDX && IDX.coins ? IDX.coins.find((c) => c.error && (c.coin === asked || c.coin === shown)) : null;
    const notice = $("acct-err");       // app.js's own notice for an account with no board says it in full, just above
    M.err.hidden = !bad || !!(notice && !notice.hidden && notice.textContent.startsWith(bad.coin + ":"));
    if (bad) set(M.err, `${bad.coin}: its step failed this run (${bad.error.slice(0, 60)})${bad.coin !== shown ? ` · showing ${shown}` : ""}`);
  }
  // no board at all yet, a while after load: say the page can't load one, rather than wait forever
  setTimeout(() => {
    if (B) return;
    set(M.st, "CAN'T LOAD THE BOARD · NOTHING TO SHOW YET");
    M.st.classList.add("act-warn");
  }, NO_FIRST);

  // ---- monitor row 2: the trigger meter (desk tabs) --------------------------------------------------------
  function priceCap(o, now) {
    if (o.shut && o.shut.by === "schedule") return `${vName()} closed now by its schedule (${o.shut.why}, until ${hhmm(o.shut.until)} UTC) · price ${mid$(o.mid)} from the last run, ${hhmm(B.updated)} UTC`;
    if (o.shut) return `${vName()} closed at the last run, ${hhmm(B.updated)} UTC (${o.shut.why}) · price ${mid$(o.mid)} from that run`;
    if (!o.isLive) return `price ${mid$(o.mid)} at the last run, ${hhmm(B.updated)} UTC · no live price yet`;
    const age = now - o.at;
    return age > NO_TICK ? `no live price for ${Math.round(age / 1000)} s · showing the last one, ${mid$(o.mid)}`
      : `price ${mid$(o.mid)} from ${o.feed ? "Coinbase" : `Hyperliquid${venue() ? ", a proxy for Coinbase's" : ""}`} · ${Math.max(0, Math.round(age / 1000))}s ago`;
  }
  let lastPct = null;
  function meter(now) {
    if (!B || basket()) return;
    const o = calc(now), r = o.rules, word2 = o.side === "short" ? "short line" : "buy line";
    const trend = (o.side || "").toUpperCase(), at = `at the ${hhmm(o.c.next)} close`;
    let big = "—", to = "", cap = priceCap(o, now), f = null, ticks = "LINE", hot = false;
    if (o.pos) {
      ticks = "STOP";
      big = pct(o.stopPct);
      f = o.stopAtr;
      to = o.through ? `price is through the run's stop ${usd(o.pos.stop)}` : `to the run's stop ${usd(o.pos.stop)}${o.stopAtr != null ? ` · ${o.stopAtr.toFixed(1)} ATR` : ""}`;
      // the position's price move only: the entry fee and funding are already out of the cash, the exit fee to come
      // (the mode is the pill beside it; the closes since the run are in the countdown)
      cap = `move since entry ${o.pnl >= 0 ? "+" : ""}${usd(o.pnl)}, before fees and funding · ` + priceCap(o, now);
      hot = o.through;
    } else if (o.brake) {
      // a brake holds every entry: no line is in play, so no distance to one, and the price doesn't matter to it
      big = "NO NEW ENTRIES";
      to = o.brake[0];
      cap = `${o.brake[1]} · as of the last run, ${hhmm(B.updated)} UTC`;
    } else if (o.side && o.level != null && o.bound && o.past) {
      // closes since the run can only move the line away (up for a buy line): the board's level is a bound the price
      // is already past, so where the line is now is not known here (the next desk run knows)
      big = (o.side === "short" ? "≤" : "≥") + usd(o.level);
      to = `${word2} ${at}, if still ${trend}: the price is past this bound; the real line may be ${o.side === "short" ? "lower" : "higher"}`;
    } else if (o.side && o.level != null) {
      big = (o.bound ? (o.side === "short" ? "≤" : "≥") : "") + pct(o.pct);   // a bound: at least this far
      f = o.past ? 0 : o.atr;
      // the strip's readout shows the line at the run's close; when this one (the next close's) differs, name its close
      const sg = (B.analysis.gates || []).find((g) => g.name === "setup");
      const w = !o.bound && sg && sg.threshold != null && usd(sg.threshold) !== usd(o.level) ? `${hhmm(o.c.next)} ${word2}` : word2;
      to = o.past ? `past the ${w} ${usd(o.level)}` : `to the ${w}${o.bound ? ", at " + (o.side === "short" ? "most" : "least") : ""} ${usd(o.level)}` +
        (o.atr != null && !o.bound ? ` · ${o.atr.toFixed(1)} ATR` : "");
      if (o.past) cap = `past the line now · only a ${(B.header.interval || "").toLowerCase()} close decides, at the next desk run · ` + cap;
      if (o.bound) cap = `if still ${trend}, later closes can only ${o.side === "short" ? "lower" : "raise"} it · ` + cap;
      hot = o.past;
    } else if (o.side) {
      to = `the ${word2} is unknown until the next desk run: the run's bars have aged out of the window`;
    } else {
      const dir = (B.analysis.direction || "").toUpperCase();
      to = dir ? `no buy line: the trend says ${dir}` : "no line on this board";
      if (dir === "SHORT") cap = `long only: it waits for the ${r ? r.ema_fast + "-EMA" : "fast EMA"} to be back above the ${r ? r.ema_slow + "-EMA" : "slow EMA"} · ` + cap;
    }
    const changed = lastPct !== null && big !== lastPct;
    lastPct = big;
    set(M.pct, big); set(M.to, to); set(M.cap, cap);
    M.pct.classList.toggle("act-hot", hot);
    M.pct.classList.toggle("act-word", /[A-Z]{3}/.test(big));
    if (changed) flash(M.pct);
    set(M.ticks.firstElementChild, ticks); set(M.end0, ticks);
    M.mk.hidden = f == null;
    M.meter.classList.toggle("act-noscale", f == null && !o.pos);   // no distance to show: no scale
    if (f != null) M.mk.style.transform = `translateX(${(Math.min(5, Math.max(0, f)) / 5 * 100).toFixed(2)}%)`;
    M.mk.classList.toggle("act-past", !!o.past || !!o.through);
  }

  // ---- monitor row 2, VT: each coin against its 50-day average ---------------------------------------------
  function vtLive(now) {
    if (!B || !basket()) return;
    const rows = M.vtl.children, hs = B.basket.holdings, n = B.basket.rules.sma_bars;
    let any = false;
    hs.slice(0, 3).forEach((h, k) => {
      const lv = mids ? parseFloat(mids[h.coin]) : NaN, isLive = isFinite(lv) && lv > 0;
      any = any || isLive;
      const px = isLive ? lv : h.mark, gap = h.sma50 ? px / h.sma50 - 1 : null;
      // the live price against the average as of the last close; ABOVE / BELOW is what that close said
      const html = `<b>${esc(h.coin)}</b> ${usd(px)} · ${gap == null ? "warming up" : `<i>${pct(gap, 1)}</i> vs ${n}-day avg`} · ` +
        (h.sma50 == null ? "no average yet" : `<span class="${h.above_sma ? "act-up" : "act-dn"}">${h.above_sma ? "ABOVE" : "BELOW"}</span> at 00:00`);
      if (rows[k].innerHTML !== html) {
        const was = rows[k].querySelector("i"), now2 = gap == null ? null : pct(gap, 1);
        rows[k].innerHTML = html;
        if (was && now2 && was.textContent !== now2) flash(rows[k].querySelector("i"));
      }
    });
    const age = now - midsAt;
    set(rows[3], !any ? `prices at the last run, ${hhmm(B.updated)} UTC · no live price yet`
      : age > NO_TICK ? `no live price for ${Math.round(age / 1000)} s · showing the last ones`
      : `Hyperliquid · ${Math.round(age / 1000)}s ago · averages as of the ${ymd(B.basket.last_close_ts || B.updated).slice(5)} close`);
  }

  // ---- monitor row 3: the gate strip, a labelled replay of the last run's checks ---------------------------
  let gates = [], blocker = -1;
  function gateLine(g) {
    const t = g.threshold == null ? "" : ` (${g.op === "min" ? "min" : g.op === "max" ? "max" : "vs"} ${n2(g.threshold)})`;
    return `${(g.owner || "").toUpperCase()} ${g.name.replace(/_/g, " ")} ${g.value == null ? g.note || "" : n2(g.value) + t}`.trim();
  }
  function restText() {
    const chief = agentOf("chief"), cl = last(B.chart.candles);
    if (blocker < 0) return `every check passed at the ${cl ? hhmm(cl[0] + B.chart.bar_ms) + " " : ""}close · CHIEF ${chief ? chief.value : "—"}`;
    const g = gates[blocker], when = cl ? ` ${hhmm(cl[0] + B.chart.bar_ms)}` : "";
    if (g.name === "flat") return `${g.note}: no new entries while it is open`;
    if (g.verdict === "DROP" && phaseOf(g) === "KILLS") {
      const k = BRAKE[g.name === "manual_kill" ? (g.note === "flat_kill" ? "flat_kill" : "entry_kill") : g.name];
      if (k) return g.name === "manual_kill" ? `DROP at manual kill: ${k[0]}` : `DROP at ${k[0]} · no new entries`;
    }
    if (g.name === "setup" && g.value != null && g.threshold != null)
      return `setup WAIT ·${when} close ${usd(g.value)} ${g.op === "min" ? "<" : ">"} line ${usd(g.threshold)}`;
    return `${g.verdict} at ${g.name.replace(/_/g, " ")}: ${g.value == null ? g.note || "" : n2(g.value)}${g.threshold == null ? "" : ` (${g.op} ${n2(g.threshold)})`}`;
  }
  function buildStrip() {
    gates = (B.analysis.gates || []).slice();
    blocker = gates.findIndex((g) => g.verdict === "DROP" || g.verdict === "WAIT");   // EntryDecision.blocked_by
    const tally = {};
    gates.forEach((g) => { const w = word(g.verdict, g).toLowerCase(); tally[w] = (tally[w] || 0) + 1; });
    const jev = B.jev || {}, asked = gates.some((g) => g.owner === "jev");
    const jevNote = asked ? "" : !jev.enabled || jev.mode === "off" ? "JEV off this run"
      : blocker >= 0 ? "JEV not asked: a check before it held the entry" : "JEV not asked this run";
    const groups = PHASES.map((ph) => ({ ph, idx: gates.map((g, k) => (phaseOf(g) === ph ? k : -1)).filter((k) => k >= 0) }))
      .filter((x) => x.idx.length || x.ph === "JEV");
    M.nodes.innerHTML = groups.map(({ ph, idx }) => `<span class="act-grp" data-g="${ph}${idx.length > 1 ? " " + idx.length : ""}" style="flex-grow:${Math.max(1, idx.length)}">` +
      (idx.length ? idx.map((k) => `<i class="act-node v-${gates[k].verdict}${k === blocker ? " act-block" : ""}${blocker >= 0 && k > blocker ? " act-after" : ""}" data-n="${k}" title="${esc(gateLine(gates[k]) + " · " + word(gates[k].verdict, gates[k]))}"></i>`).join("")
        : `<i class="act-node act-jev" title="${esc(jevNote)}"></i>`) + `</span>`).join("");
    M.nodes.dataset.jev = jevNote;
    set(M.rl, `REPLAY · LAST RUN ${hhmm(B.updated)} UTC`);
    M.cnt.innerHTML = `<span class="act-wd">${gates.length} checks: </span>` + ["pass", "mark", "wait", "drop", "info", "skip"].filter((w) => tally[w])
      .map((w, k) => `<span class="c-${w}">${k ? " · " : ""}${tally[w]} ${w}</span>`).join("");
  }
  function restStrip() {
    M.nodes.classList.remove("act-rp");
    M.nodes.querySelectorAll(".act-node").forEach((n) => n.classList.remove("on", "act-hold"));
    set(M.read, gates.length ? restText() : "no checks on this board yet");
  }
  function playStrip() {
    stopRun("strip");
    stripPending = false;
    if (!B || basket()) return;
    const nodes = [...M.nodes.querySelectorAll(".act-node")];
    if (reduced.matches || !visible() || !monOn || !nodes.length) { restStrip(); return; }
    M.nodes.classList.add("act-rp");
    nodes.forEach((n) => n.classList.remove("on", "act-hold"));
    const steps = nodes.map((n) => {
      const k = n.dataset.n == null ? -1 : +n.dataset.n, g = gates[k], hold = k === blocker;
      return [hold ? 600 : 120, () => {
        n.classList.add("on");
        if (hold) n.classList.add("act-hold");
        set(M.read, g ? `${gateLine(g)} · ${word(g.verdict, g)}${hold ? " · this one holds the entry" : k > blocker && blocker >= 0 ? " · still checked, for the board" : ""}`
          : M.nodes.dataset.jev || "JEV");
      }];
    });
    runs.strip = chain(steps, () => { runs.strip = null; restStrip(); });
  }

  // ---- monitor row 3, VT: what the last run's close says per coin ------------------------------------------
  function vtStrip(play) {
    const hs = B.basket.holdings, decided = hs.some((h) => h.decided_ts != null);
    set(M.rl, `REPLAY · LAST RUN ${hhmm(B.updated)} UTC · the ${ymd(B.basket.last_close_ts || B.updated).slice(5)} close`);
    M.cnt.innerHTML = "";
    M.nodes.innerHTML = hs.map((h) => {
      const t = h.sma50 == null ? `${h.coin} warming up` : h.above_sma ? `${h.coin} → ${fix(h.target_units || 0, 2)} units` : `${h.coin} → 0 units`;
      return `<span class="act-chip ${h.sma50 == null ? "" : h.above_sma ? "c-pass" : "c-drop"}" title="${esc(`${h.coin} ${h.above_sma ? "above" : "below"} its average at that close: target ${fix(h.target_units || 0, 2)} units`)}">${esc(t)}</span>`;
    }).join("");
    const orders = hs.filter((h) => h.pending).map((h) => `${h.coin} ${h.pending.kind === "exit" ? "close all" : `${h.pending.usd >= 0 ? "+" : "−"}$${fix(Math.abs(h.pending.usd), 2)}`} ${modeWord()} order, fills at the next open`);
    const dts = hs.map((h) => h.decided_ts).filter((x) => x != null);
    set(M.read, !decided ? "PREVIEW · first decision at the next 00:00 close"
      : `decided at the ${ymd(Math.max(...dts)).slice(5)} close · ${orders.length ? orders.join(" · ") : "no order waiting"}`);
    stopRun("strip");
    const chips = [...M.nodes.children];
    if (!play || reduced.matches || !visible() || !monOn) { M.nodes.classList.remove("act-rp"); return; }
    M.nodes.classList.add("act-rp");
    chips.forEach((c) => c.classList.remove("on"));
    runs.strip = chain(chips.map((c) => [150, () => c.classList.add("on")]), () => { runs.strip = null; M.nodes.classList.remove("act-rp"); });
  }

  // ---- monitor row 4: the countdown to the next close -------------------------------------------------------
  function countdown(now) {
    const c = closes(now), vt = basket();
    set(M.next1, `NEXT ${vt ? "DECISION" : (B.header.interval || "").toUpperCase() + " CLOSE"} ${hhmm(c.next)} UTC IN ${clock(c.next - now)}`);
    const braked = !vt && !B.ticker.pos && brake();
    if (!c.missed) { if (M.next2.title) M.next2.title = ""; set(M.next2, vt ?"the close re-computes the averages and vol" : braked ? "the first desk run after it checks again" : "the first desk run after it decides it"); return; }
    const when = c.list.map((t) => (vt ? ymd(t).slice(5) + " " : "") + hhmm(t)).join(", ");
    const verb = braked ? "checks" : "decides";
    // a narrow phone: one line (the times are in the title); otherwise the closes by name
    const tip = `closed since the run: ${when}${c.missed > c.list.length ? ", …" : ""}`;
    if (M.next2.title !== tip) M.next2.title = tip;
    set(M.next2, narrow.matches && c.missed > 1 ? `${c.missed} closes since the run wait for the next desk run`
      : c.missed <= c.list.length ? `${when} closed since the run · the next run ${verb} ${c.missed > 1 ? "each" : "it"}`
      : `${c.missed} closes since the run (${when}, …) · the next desk run ${verb} each`);
  }

  // ---- the desk log: the last run's steps, as numbered lines ----------------------------------------------
  function deskLines() {
    const b = B, h = b.header, ch = b.chart, st = b.analysis.stats, pp = b.paper || {}, r = b.rules;
    const cr = (agentOf("crawler") || {}).stats || {}, cl = last(ch.candles), bar = ch.bar_ms;
    const iv = h.interval || "", out = [];
    const L1 = (tag, text, v, cls, w) => out.push({ tag, text, v, cls, w });
    // this run's events, in the order the desk ran them (desk/paper.py step): everything before the newest close's
    // checks (fills at earlier bars' opens, earlier closes' decisions, the brakes and stop changes at the newest close),
    // then that close's checks, then its decision and what filled after it (the forming bar)
    const ev = pp.new_events || [], closeAt = cl ? cl[0] + bar : null;
    const before = (e) => closeAt != null && (e.ts < closeAt || (e.ts === closeAt && e.agent !== "chief"));
    const evLine = (e) => L1((e.agent || "").toUpperCase(), `${hhmm(e.ts)} ${e.text}`, null, "act-ev");
    L1("RUN", `#${pp.runs ?? "—"} on this ledger · ${h.coin} · ${(h.mode || "").toLowerCase()} · board built ${hms(b.updated)} UTC`);
    L1("CRAWL", `${(cr.bars ?? ch.candles.length).toLocaleString("en-US")} ${iv} candles + ${cr.funding ?? 0} funding prints · ${cr.requests ?? "—"} requests to ${vName()}`);
    if (cl) {
      const closeTs = cl[0] + bar;
      L1("CRAWL", `newest close ${hhmm(closeTs)} UTC at ${usd(cl[4])}${cr.age_s != null ? ` · ${ago(cr.age_s * 1000)} old at the run` : ""}`);
      const n = pp.bars_processed_this_run || 0;
      if (pp.runs === 1) L1("CRAWL", `first run: the account opened here; its first decision is at the next ${iv} close`);
      else if (!n) L1("CRAWL", `0 new closes this run: ${hhmm(closeTs)} was decided by an earlier run; the gates were re-checked against it`);
      else {
        const ts = []; for (let k = n - 1; k >= 0 && ts.length < 4; k--) ts.push(hhmm(closeTs - k * bar));
        L1("CRAWL", n > 1 ? `${n} new closes this run (${n > 4 ? "…, " : ""}${ts.join(", ")}), each decided in turn` : `1 new close this run (${ts[0]}), decided by this run`);
      }
      const early = ev.filter(before);
      if (early.length) {
        L1("", `── THIS RUN, BEFORE THE ${hhmm(closeAt)} CLOSE'S CHECKS`, null, "act-ph"); early.forEach(evLine);
        L1("", `── THE ${hhmm(closeAt)} CLOSE`, null, "act-ph");
      }
      const ef = last(ch.ema_fast), es = last(ch.ema_slow);
      const fe = r ? `${r.ema_fast}-EMA` : "fast EMA", se = r ? `${r.ema_slow}-EMA` : "slow EMA";
      L1("CALC", ef == null || es == null ? `${fe} / ${se}: warming up` :
        `${fe} ${usd(ef)} · ${se} ${usd(es)} → looking ${(b.analysis.direction || "").toUpperCase()}`);
      const hi = last(ch.don_hi), lo = last(ch.don_lo);
      L1("CALC", `${r ? r.breakout_bars + "-bar" : "channel"} high ${usd(hi)} · low ${usd(lo)}, from the bars before that close`);
      L1("CALC", `ATR ${st.atr_pct == null ? "—" : fix(st.atr_pct, 2) + "%"} of the price · funding ${st.funding_apr == null ? "—" : fix(st.funding_apr, 2) + "% apr"} (3-day average)`);
      L1("CALC", `book at the run: spread ${st.spread_bps == null ? "—" : fix(st.spread_bps, 2) + " bps"} · depth ${usd(st.depth_usd, 0)}`);
    }
    let ph = null;
    const gs = b.analysis.gates || [], bi = gs.findIndex((g) => g.verdict === "DROP" || g.verdict === "WAIT");
    gs.forEach((g, k) => {
      if (phaseOf(g) !== ph) { ph = phaseOf(g); L1("", `── ${ph}`, null, "act-ph"); }
      const t = g.threshold == null ? "" : ` (${g.op === "min" ? "min" : g.op === "max" ? "max" : "vs"} ${n2(g.threshold)})`;
      L1((g.owner || "").toUpperCase(), `${g.name.replace(/_/g, " ")} ${g.value == null ? "" : n2(g.value) + t}`.trim(), g.verdict, bi >= 0 && k > bi ? "act-after" : "", word(g.verdict, g));
      if ((g.verdict === "WAIT" || g.verdict === "DROP") && g.note) L1("", g.note, null, "act-note");
      else if (g.verdict === "SKIP" && g.note) L1("", g.note, null, "act-note act-dim");
    });
    if (!gs.some((g) => g.owner === "jev") && gs.length) {
      L1("", "── JEV", null, "act-ph");
      const j = b.jev || {};
      L1("JEV", `not asked: ${!j.enabled || j.mode === "off" ? "JEV off this run" : bi >= 0 ? "a check before it held the entry" : "asked only when every other gate passes"}`);
    }
    const chief = agentOf("chief"), size = agentOf("size"), pos = b.ticker.pos;
    if (chief) L1("CHIEF", `${chief.value}${bi < 0 ? "" : gs[bi].name === "flat" ? " · in a position: no new entries while it is open"
      : ` · ${gs[bi].verdict === "DROP" ? "dropped" : "held"} at ${gs[bi].name.replace(/_/g, " ")}`}`);
    if (size && size.quote && size.quote !== "nothing to size.") {
      const q = size.quote.replace(/\.$/, "");
      L1("SIZE", pos ? `on now: ${q}` : /^GO/.test(chief && chief.value) ? `${q} · order placed, fills at the next open`
        : `had it entered at that close: ${q} · not traded`);
    }
    const late = ev.filter((e) => !before(e));
    if (late.length) late.forEach(evLine);
    else if (!ev.length) L1("FILLS", "no new orders or fills this run");
    return out;
  }
  function vtLines() {
    const b = B, bk = b.basket, r = bk.rules, cr = bk.crawler || {}, pp = b.paper || {}, out = [];
    const L1 = (tag, text, v, cls) => out.push({ tag, text, v, cls });
    L1("RUN", `#${pp.runs ?? "—"} on this ledger · VT basket · ${(b.header.mode || "").toLowerCase()} · board built ${hms(b.updated)} UTC`);
    L1("CRAWL", `${cr.bars ?? "—"} daily candles (${r.coins.join(", ")}) · ${cr.requests ?? "—"} requests to Hyperliquid`);
    if (bk.last_close_ts) L1("CRAWL", `newest close ${ymd(bk.last_close_ts)} 00:00 UTC${cr.age_s != null ? ` · ${ago(cr.age_s * 1000)} old at the run` : ""}`);
    const n = pp.bars_processed_this_run || 0, decided = bk.holdings.some((h) => h.decided_ts != null);
    L1("CRAWL", pp.runs === 1 ? "first run: the account opened flat; its first decision is at the next 00:00 UTC close"
      : n ? `${n} new daily close${n > 1 ? "s" : ""} this run, each decided in turn`
      : decided ? "0 new daily closes this run: the last close was decided by an earlier run"
      : "0 new daily closes this run · PREVIEW: what the rule sees at the last close; the first decision is at the next 00:00 close");
    // this run's events in the desk's order (desk/paper.py step_basket): fills at earlier opens and earlier closes'
    // decisions first, then the newest close's decision, then the fills at the forming bar's open
    const ev = pp.new_events || [], T = bk.last_close_ts;
    const evLine = (e) => L1((e.agent || "").toUpperCase(), `${ymd(e.ts).slice(5)} ${hhmm(e.ts)} ${e.text}`, null, "act-ev");
    const early = T ? ev.filter((e) => e.ts < T) : [];
    if (early.length) {
      L1("", `── THIS RUN, BEFORE THE ${ymd(T).slice(5)} CLOSE`, null, "act-ph"); early.forEach(evLine);
      L1("", `── THE ${ymd(T).slice(5)} CLOSE`, null, "act-ph");
    }
    bk.holdings.forEach((h) => {
      L1("CALC", h.sma50 == null ? `${h.coin} warming up the ${r.sma_bars}-day average` :
        `${h.coin} close ${usd(h.close)} vs ${r.sma_bars}-day avg ${usd(h.sma50)}`, h.sma50 == null ? null : h.above_sma ? "ABOVE" : "BELOW");
      if (h.vol10 != null) L1("CALC", `${h.coin} ${r.vol_lookback}-day vol ${(h.vol10 * 100).toFixed(1)}% → ` +
        (h.above_sma ? `target ${fix(h.target_units || 0, 2)} units (cap ${r.max_units})` : "0 units: below its average"));
      const o = h.pending, verb = o && (o.kind === "exit" ? "SELL ALL" : o.kind === "entry" ? "BUY" : o.usd > 0 ? "ADD" : "TRIM");
      L1("ORDER", `${h.coin} ` + (o ? `${o.kind === "exit" ? verb : `${verb} ${usd(Math.abs(o.usd))}`} · fills at the next open`
        : h.decided_ts == null ? "PREVIEW: the first decision is at the next close"
        : h.qty > 0 ? `HOLD ${fix(h.held_units, 2)} units` : "FLAT"), null, o ? "act-ev" : "");
    });
    L1("CHIEF", `${bk.orders.filled} fills so far · ${bk.orders.blocked} orders skipped by the $${r.min_order_usd} minimum`);
    const late = ev.filter((e) => !early.includes(e));
    if (late.length) late.forEach(evLine);
    else if (!ev.length) L1("FILLS", "no new orders or fills this run");
    return out;
  }
  const SHORT_SHOW = 12;
  let expanded = false;
  function buildLog() {
    const lines = basket() ? vtLines() : deskLines();
    const n = lines.length, pad = String(n).length;
    L.lines.style.setProperty("--n", `"${String(n).padStart(pad, "0")}"`);
    L.lines.style.setProperty("--pad", pad);
    L.lines.innerHTML = lines.map((x) => `<li class="${x.cls || ""}"><span class="act-t">${esc(x.tag)}</span>${esc(x.text)}` +
      (x.v ? ` <span class="act-v v-${x.v}">${esc(x.w || x.v)}</span>` : "") + `</li>`).join("");
    set(L.lrl, `REPLAY · LAST RUN ${hhmm(B.updated)} UTC · steps in the order the desk ran them`);
    const hidden = Math.max(0, n - SHORT_SHOW);
    L.more.hidden = !hidden;
    shortLog(hidden);
  }
  function shortLog(hidden) {
    hidden = hidden ?? Math.max(0, L.lines.children.length - SHORT_SHOW);
    L.lines.classList.toggle("act-short", !expanded);
    L.more.setAttribute("aria-expanded", String(expanded));
    set(L.more, expanded ? "▾ show only the last lines" : `▸ ${hidden} earlier lines of this run`);
  }
  L.more.addEventListener("click", () => { expanded = !expanded; shortLog(); });
  function playLog() {
    stopRun("log");
    logPending = false;
    const box = L.lines, all = [...box.children];
    const shownLines = wide.matches || expanded ? all : all.slice(-SHORT_SHOW);
    const end = () => { runs.log = null; box.classList.remove("act-rp"); all.forEach((li) => li.classList.remove("on", "cur")); if (wide.matches) box.scrollTop = box.scrollHeight; };
    if (!shownLines.length || reduced.matches || !visible() || !logOn) { end(); return; }
    const step = Math.min(wide.matches ? 100 : 80, 3000 / shownLines.length);
    box.classList.add("act-rp");
    all.forEach((li) => li.classList.remove("on", "cur"));
    if (wide.matches) box.scrollTop = 0;
    let prev = null;
    runs.log = chain(shownLines.map((li) => [step, () => {
      if (prev) prev.classList.remove("cur");
      li.classList.add("on", "cur"); prev = li;
      if (wide.matches && li.offsetTop + li.offsetHeight > box.scrollTop + box.clientHeight) box.scrollTop = li.offsetTop + li.offsetHeight - box.clientHeight;
    }]), end);
  }
  function replayStrip() { stripPending = false; if (!B) return; if (basket()) vtStrip(true); else playStrip(); }
  function finishStrip() { stopRun("strip"); if (B && !basket()) restStrip(); else M.nodes.classList.remove("act-rp"); }
  M.play.addEventListener("click", replayStrip);
  L.lplay.addEventListener("click", () => playLog());
  M.jump.addEventListener("click", () => log.scrollIntoView({ behavior: reduced.matches ? "auto" : "smooth", block: "start" }));

  // ---- the log's LIVE rows: computed in this browser from live prices (BTC on Coinbase: its feed, or the proxy) ---
  const LIVE_HEAD = (iv, o) => `<div class="act-lh">LIVE · COMPUTED IN YOUR BROWSER ${o && o.shut ? `AT THE LAST RUN'S PRICE (${esc(vName().toUpperCase())} CLOSED)`
    : o && o.feed ? "FROM COINBASE'S OWN PRICES" : `FROM HYPERLIQUID PRICES${venue() ? " (A PROXY FOR COINBASE'S)" : ""}`} · the desk itself decides only at the ${esc(iv)} close</div>`;
  let liveShape = "";
  function liveRows(now) {
    if (!B) return;
    const rows = [];
    let o = null;
    if (basket()) {
      const n = B.basket.rules.sma_bars, age = Math.round((now - midsAt) / 1000);
      rows.push(["PRICES", mids ? `allMids · ${age > NO_TICK / 1000 ? `no live price for ${age} s` : `${age}s ago`}` : `at the last run, ${hhmm(B.updated)} UTC · no live price yet`, ""]);
      B.basket.holdings.forEach((h) => {
        const lv = mids ? parseFloat(mids[h.coin]) : NaN, isLive = isFinite(lv) && lv > 0, px = isLive ? lv : h.mark;
        rows.push([h.coin, `${mid$(px)} · ${h.sma50 ? `${pct(px / h.sma50 - 1, 2)} vs the ${n}-day avg ${usd(h.sma50)}, as of the ${ymd(B.basket.last_close_ts || B.updated)} close` : "no average yet"}`,
          ""]);
      });
    } else {
      o = calc(now);
      const iv = (B.header.interval || "").toLowerCase(), age = Math.round((now - o.at) / 1000);
      rows.push(["PRICE", o.shut ? `${mid$(o.mid)} at the last run · ${vName()} ${o.shut.by === "schedule" ? `is closed now by its schedule (${o.shut.why}, until ${hhmm(o.shut.until)} UTC)` : `was closed then (${o.shut.why})`}`
        : o.isLive ? `${mid$(o.mid)} · ${o.feed ? "Coinbase feed" : "allMids"} · ${age > NO_TICK / 1000 ? `no live price for ${age} s` : `${Math.max(0, age)}s ago`}` : `${mid$(o.mid)} at the last run · no live price yet`, ""]);
      const exp = B.header.shorts && B.header.experiment && o.side === "short" ? " · shorts: paper experiment" : "";
      if (o.pos) {
        rows.push(["STOP", `${usd(o.pos.stop)} from the last run · ${pct(o.stopPct)} away${o.stopAtr != null ? ` · ${o.stopAtr.toFixed(1)} ATR` : ""}`, o.through ? "act-hot" : ""]);
        // the price move only: the entry fee and funding are already out of the account's cash, the exit fee to come
        rows.push(["POSITION", `${o.pos.side} ${o.pos.qty} from ${usd(o.pos.entry)} · price move ${o.pnl >= 0 ? "+" : ""}${usd(o.pnl)} at ${mid$(o.mid)}, before fees and funding · ${modeWord()}`, ""]);
      } else if (o.brake) {
        rows.push(["ENTRIES", `none: ${o.brake[0]}, ${o.brake[1]} · as of the last run, ${hhmm(B.updated)} UTC`, ""]);
      } else if (o.side) {
        const head = o.side === "short" ? "SHORT LINE" : "BUY LINE", way = o.side === "short" ? "lower" : "higher";
        const cond = o.bound ? `if the trend still says ${o.side.toUpperCase()} · ` : "";
        if (o.level == null) rows.push([head, `unknown until the next desk run: the run's bars have aged out of the window`, ""]);
        else if (o.bound && o.past) rows.push([head, `${cond}at ${o.side === "short" ? "most" : "least"} ${usd(o.level)} · the price is past that bound; the real line could be ${way} · known at the next desk run${exp}`, ""]);
        else if (o.bound) rows.push([head, `${cond}at ${o.side === "short" ? "most" : "least"} ${usd(o.level)} · ${pct(o.pct)} or more away · the closes since the last run can only move it ${way}${exp}`, ""]);
        else if (o.past) rows.push([head, `${usd(o.level)} · a ${iv} close past it would clear the setup check; every check re-runs at the close${exp}`, "act-hot"]);
        else rows.push([head, `${usd(o.level)} · ${pct(o.pct)} away (${usd(o.gap, 2)}) · a close here: setup WAIT${exp}`, ""]);
      } else rows.push(["LINE", `none: the trend says ${(B.analysis.direction || "—").toUpperCase()} and this account takes longs only`, ""]);
      if (o.ef != null) {
        // the board's EMAs are rounded to cents: "≈" only when the gap is inside that rounding, or too small to show
        const side = o.ef >= o.es ? "LONG" : "SHORT", near = Math.abs(o.ef - o.es) < 0.02 || usd(o.ef) === usd(o.es);
        const was = (B.analysis.direction || "").toUpperCase(), r = o.rules;
        rows.push(["TREND IF IT CLOSED HERE", `${side}${near ? " ≈" : ""} · ${r.ema_fast}-EMA ${usd(o.ef)} ${near ? "≈" : o.ef >= o.es ? "≥" : "<"} ${r.ema_slow}-EMA ${usd(o.es)}${was && was !== side ? ` · flips from ${was}` : ""}`, ""]);
      }
    }
    const head = LIVE_HEAD(basket() ? "00:00 UTC" : (B.header.interval || ""), o);   // the heading names the source
    const shape = rows.map((r) => r[0]).join("|") + "|" + head;
    if (shape !== liveShape) {
      liveShape = shape;
      L.live.innerHTML = head + rows.map(() => `<div class="act-lr"><b></b><span></span></div>`).join("");
    }
    const els = L.live.querySelectorAll(".act-lr");
    rows.forEach(([k, v, cls], j) => {
      const e = els[j], s = e.lastElementChild;
      const changed = !!s.textContent && s.textContent !== v && k !== "PRICE" && k !== "PRICES";
      set(e.firstElementChild, k);
      set(s, v);
      if (s.className !== cls) s.className = cls;
      if (changed) flash(s);
    });
  }

  // ---- the live tape: one line per real event, timed by the browser's own entries for app.js's requests -----
  const tape = [];
  const hlEnds = [];                   // responseEnd and duration of each Hyperliquid /info request app.js made
  let boardEntry = null, po = null, prevTick = 0;
  function take(e) {
    if (e.name === HL_INFO) { hlEnds.push({ end: e.responseEnd, ms: e.duration }); if (hlEnds.length > 30) hlEnds.shift(); }
    else if (/\/boards\/[A-Z]+\.json(\?|$)/.test(e.name)) boardEntry = e;
  }
  if ("PerformanceObserver" in window) {
    try { po = new PerformanceObserver((list) => list.getEntries().forEach(take)); po.observe({ type: "resource", buffered: true }); } catch { po = null; }
  }
  function tapeAdd(text, cls = "") {
    if (!logOn || !visible()) return;      // a line only for what happens while you can see it
    tape.push({ text, cls });
    if (tape.length > 6) tape.shift();
    drawTape();
  }
  function drawTape() {
    const slots = L.tape.querySelectorAll("div"), off = 6 - tape.length;
    for (let k = 0; k < 6; k++) {
      const x = tape[k - off];
      set(slots[k], x ? x.text : "");
      slots[k].className = x ? `${x.cls}${k === 5 ? " act-new" : ""}` : "";
    }
  }
  let prevMid = null;                  // {src, m}: the last price on the tape, and whose (a move is only within one source)
  function moved(src, m) {
    const d = prevMid && prevMid.src === src && isFinite(m) ? m - prevMid.m : null;
    if (isFinite(m)) prevMid = { src, m };
    return d == null ? "" : d === 0 ? " ·0" : ` ${d > 0 ? "▲" : "▼"}${Number(Math.abs(d).toPrecision(6))}`;
  }
  function tapeTick(now) {
    if (po) po.takeRecords().forEach(take);
    const t = performance.now(), ended = hlEnds.filter((x) => x.end > prevTick && x.end <= t);
    prevTick = t;
    const ms = ended.length === 1 ? ` · ${Math.round(ended[0].ms)} ms` : "";   // two ended together: whose time is it? left out
    let what;
    if (basket()) what = B.basket.holdings.map((h) => `${h.coin} ${mid$(parseFloat(mids[h.coin]))}`).join(" ");
    else { const m = parseFloat(mids[B.header.coin]); what = `${B.header.coin} ${mid$(m)}${moved("hl", m)}`; }
    tapeAdd(`${hms(now)} price check · allMids${ms} · ${what}`, "act-tk");
  }
  // Coinbase's feed (BTC's tab): no request to time; a line at most every 5 s, its newest price, as its prices come
  // about once a second and the tape holds six lines
  let feedLine = 0;
  function tapeFeed(now) {
    if (now - feedLine < 5000) return;
    feedLine = now;
    tapeAdd(`${hms(now)} price · Coinbase feed · ${B.header.coin} ${mid$(tk.price)}${moved("cb", tk.price)}`, "act-tk");
  }
  function tapeBoard(now, kind) {
    if (po) po.takeRecords().forEach(take);
    const e = boardEntry && boardEntry.name.includes(`/boards/${shown}.json`) ? boardEntry : null;
    const kb = e && e.decodedBodySize ? ` · ${(e.decodedBodySize / 1024).toFixed(1)} KB` : "";
    if (kind === "new") tapeAdd(`${hms(now)} NEW RUN · board built ${hms(B.updated)} UTC${kb}`, "act-ev");
    else if (kind === "first") tapeAdd(`${hms(now)} board read${kb} · last run ${hms(B.updated)} UTC`, "act-bd");
    else tapeAdd(`${hms(now)} board re-read${kb} · no new board since ${hms(B.updated)} UTC`, "act-bd");
  }

  // ---- the desk runs heartbeat: the last run's boards, account by account -------------------------------------
  function beat(now) {
    const old = stale(now), lost = unread(now), mins = CRON.split(" ")[0].split(",").map((m) => ":" + m.padStart(2, "0")).join(" ");
    const pp = B.paper || {};
    let chips = "", built = "";
    if (IDX && IDX.coins) {
      chips = IDX.coins.map((c) => `<span class="act-hb${c.coin === shown ? " me" : ""}${c.error ? " bad" : ""}"${c.error ? ` title="${esc(c.error)}"` : ""}>${esc(c.coin)} ${c.updated ? hms(c.updated) : "failed"}</span>`).join(`<i>→</i>`);
      const ts = IDX.coins.map((c) => c.updated).filter(Boolean);
      if (ts.length) built = ` · boards built ${hms(Math.min(...ts))}–${hms(Math.max(...ts))}`;
    } else chips = `<span class="act-hb me">${esc(shown)} ${hms(B.updated)}</span><i>live server</i>`;
    const bad = IDX && IDX.coins ? IDX.coins.filter((c) => c.error) : [];
    L.beat.innerHTML = `<div class="act-hbs">${chips}</div>` +
      (lost ? `<div class="act-warn">can't read the board since ${hms(lastRead)} · the one shown was built ${hms(B.updated)} UTC</div>` : "") +
      `<div class="${old ? "act-warn" : ""}">${old ? `no new board for ${ago(now - B.updated)}` : `last run ${ago(now - B.updated)} ago`}${built} · run #${pp.runs ?? "—"} on this ledger</div>` +
      bad.map((c) => `<div class="act-warn">${esc(c.coin)} step failed: ${esc(c.error.slice(0, 60))}</div>`).join("") +
      (staticBoard ? `<div class="act-dim">scheduled ${mins} UTC · GitHub skips many runs · a late run catches up on every close it missed</div>` : "");
  }

  // ---- research: backtests and audits, from research.json (verbatim quotes, pinned by a test) ------------------
  function nextReaudit(now, s) {
    const today = Math.floor(now / DAY) * DAY, y = new Date(now).getUTCFullYear();
    for (const yy of [y, y + 1]) for (const m of s.months) { const t = Date.UTC(yy, m - 1, s.day); if (t >= today) return t; }
    return null;
  }
  let rschKey = "";
  function drawResearch(now) {
    if (!research || !B) return;
    const key = shown + "|" + Math.floor(now / 60000);
    if (key === rschKey) return;
    rschKey = key;
    const items = research.items.filter((x) => x.tab === "all" || x.tab === shown);
    L.rsch.innerHTML = `<div class="act-rsh">RESEARCH · BACKTESTS AND AUDITS, NOT LIVE RESULTS</div>` + items.map((x) => {
      let text = x.text;
      if (x.schedule) {
        const s = x.schedule, nx = nextReaudit(now, s), from = Date.parse(s.new_data_from + "T00:00:00Z");
        const today = Math.floor(now / DAY) * DAY;
        text = text.replace("{next}", nx ? ymd(nx) : "—").replace("{days}", nx ? Math.round((nx - today) / DAY) : "—")
          .replace("{from}", s.new_data_from).replace("{have}", Math.max(0, Math.floor((now - from) / DAY))).replace("{need}", s.need_days);
      }
      const qs = Array.isArray(x.quote) ? x.quote : [x.quote];   // a quote is from src, or {src, text} from its own file
      const src = [...new Set([x.src, x.src2, ...qs.map((q) => q && q.src)].filter(Boolean).map((p) => p.split("/").pop()))].join(" · ");
      const q = qs.map((q) => (typeof q === "string" ? q : q.text)).join(" … ").replace(/\*\*|`/g, "");
      return `<div class="act-rs" title="${esc(q)}"><b>${esc(x.tag)}</b> ${esc(text)} <i>${esc(src)}</i></div>`;
    }).join("");
  }
  // read once, when the log first comes into view (its last block): not in the first screen's loading
  let rschAsked = false;
  function loadResearch() {
    if (rschAsked) return;
    rschAsked = true;
    fetch("research.json", { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (d && Array.isArray(d.items)) { research = d; rschKey = ""; drawResearch(Date.now()); }
    }).catch(() => { /* the block stays empty: nothing is shown that the repo's files don't say */ });
  }

  function footer() {
    const bad = Object.entries(modes).filter(([, m]) => m && m !== "PAPER");
    L.foot.classList.toggle("act-livemode", bad.length > 0);
    set(L.foot, bad.length ? `MODE: ${bad.map(([c, m]) => `${c} ${m}`).join(", ")} · this board is not paper`
      : "PAPER · no real money · the rules are fixed in desk/thresholds.py and the desk does not change them · backtests are not promises");
  }

  // ---- the clock: once a second, only while a panel is on screen and the page is visible --------------------
  function secondly() {
    if (!B) return;
    const now = Date.now();
    secN++;
    status(now);
    countdown(now);
    if (basket()) vtLive(now); else meter(now);   // ages, and a close that just passed; set() writes only changes
    if (logOn) liveRows(now);
    if (secN % 60 === 0) { beat(now); drawResearch(now); }
  }

  // ---- app.js's events --------------------------------------------------------------------------------------
  function cancelAll() {
    stopRun("strip"); stopRun("log");
    tape.length = 0; drawTape();
    prevMid = null; lastPct = null; liveShape = ""; rschKey = "";
  }
  addEventListener("desk:board", (e) => {
    const d = e.detail || {};
    if (!d.board || !d.board.header) return;
    const now = Date.now(), first = !B, switched = d.coin !== shown, fresh = !first && !switched && d.updated !== shownUpdated;
    if (switched || first) cancelAll();
    lastRead = now; unreadSaid = false;
    B = d.board; IDX = d.index || null; staticBoard = d.static !== false;
    const lv = d.live;   // where app.js's price comes from as it draws this board, until its next desk:tick
    if (lv) tk = { coin: d.coin, price: Number(lv.price), source: lv.source || null, at: lv.priceAt || 0, closed: lv.closed || null };
    mon.classList.remove("act-wait"); log.classList.remove("act-wait");
    modes[d.coin] = B.header.mode;
    shown = d.coin; shownUpdated = d.updated;
    status(now); countdown(now); footer();
    if (first || switched || fresh) {
      if (basket()) { vtStrip(false); vtLive(now); }
      else { buildStrip(); restStrip(); meter(now); }
      expanded = false;
      buildLog(); liveRows(now); beat(now); drawResearch(now);
      // the last run's work replays when it lands (or when you switch to it). A stale board sits still: the desk
      // is not running, and the page doesn't pretend it is (the ▶ buttons still replay it)
      const still = stale(now);
      if (!still) {
        if (monOn) replayStrip(); else stripPending = first;   // off screen later on: its final state, no replay
        if (logOn) playLog(); else logPending = true;
      }
      const fills = (B.paper && B.paper.new_events || []).some((x) => TRADE.test(x.text));
      if (!stale(now)) flash(M.dot, fills ? "fill" : "read");
      tapeBoard(now, first || switched ? "first" : "new");
    } else {
      beat(now);
      if (!stale(now)) flash(M.dot, "read");      // the dot blinks when a board is read: a real event, not a timer
      tapeBoard(now, "same");
    }
    clockRun();
  });
  addEventListener("desk:tick", (e) => {
    const d = e.detail || {};
    if (!d.mids) return;
    mids = d.mids; midsAt = d.at || Date.now();
    tk = { coin: d.coin, price: Number(d.price), source: d.source || null, at: d.priceAt || midsAt, closed: d.closed || null };
    if (!B || document.hidden) return;     // hidden: keep the price; the clock redraws it when the page is back
    const now = Date.now();
    if (basket()) vtLive(now); else meter(now);
    liveRows(now);
    if (feedTick()) tapeFeed(now);
    else setTimeout(() => { if (B) tapeTick(now); }, 50);   // the request's timing entry lands just after its answer
  });
  document.addEventListener("visibilitychange", () => {
    document.documentElement.classList.toggle("act-hidden", document.hidden);
    if (document.hidden) {           // hidden: finish what was playing, at its final state
      if (runs.strip) finishStrip();
      if (runs.log) playLog();
    } else visibleSince = Date.now();   // a hidden tab's re-reads may have been held back: give them time to land
    clockRun();
  });
  if (reduced.addEventListener) reduced.addEventListener("change", () => { if (reduced.matches) { if (runs.strip) finishStrip(); if (runs.log) playLog(); } });
  if (wide.addEventListener) wide.addEventListener("change", () => { if (runs.log) playLog(); });
})();
