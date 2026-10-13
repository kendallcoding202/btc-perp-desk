// Perp Desk board. Polls the desk's state (live server or static boards/*.json), pulls live prices straight
// from Hyperliquid every few seconds, and draws it all. No build step, no libraries.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const COLORS = {
    chief: "--white", scan: "--green", vet: "--red", crowd: "--blue",
    size: "--pink", fills: "--white", risk: "--orange", crawler: "--pink",
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
    const body = css(COLORS[key]);
    map.forEach((row, y) => [...row].forEach((ch, x) => {
      if (ch === ".") return;
      g.fillStyle = ch === "k" ? "#0d0f1f" : body;
      g.fillRect(x * px, y * px, px, px);
    }));
    return c;
  }

  // ---- formatting ---------------------------------------------------------
  const usd = (x, dp = 2) => x == null || isNaN(x) ? "—" : (x < 0 ? "-$" : "$") + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const usdShort = (x) => x == null ? "—" : Math.abs(x) >= 1e9 ? `$${(x / 1e9).toFixed(2)}B` : Math.abs(x) >= 1e6 ? `$${(x / 1e6).toFixed(2)}M` : Math.abs(x) >= 1e3 ? `$${(x / 1e3).toFixed(1)}K` : usd(x, 2);
  const px = (x) => x == null || isNaN(x) ? "—" : usd(x, Math.abs(x) >= 1000 ? 0 : 2);
  const pxAxis = (x) => Math.abs(x) >= 1000 ? Math.round(x).toLocaleString("en-US") : Number(x).toFixed(2);
  const pct = (x, dp = 2) => x == null || isNaN(x) ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(dp)}%`;
  const num = (x, dp = 2) => x == null || isNaN(x) ? "—" : Number(x).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const hhmm = (ms) => new Date(ms).toISOString().slice(11, 16);
  const mmddhhmm = (ms) => new Date(ms).toISOString().slice(5, 16).replace("T", " ");
  const DAY_MS = 86400000;

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
    const padR = 64, padT = 8, padB = 18;
    const vals = [];
    cs.forEach((c) => vals.push(c[2], c[3]));
    [chart.ema_fast, chart.ema_slow, chart.don_hi].forEach((s) => s.forEach((v) => v != null && vals.push(v)));
    if (chart.stop) vals.push(chart.stop);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
    const cw = (w - padR) / cs.length;
    const X = (k) => k * cw + cw / 2;
    const Y = (p) => padT + (hi - p) / (hi - lo) * (h - padT - padB);
    const tsIndex = new Map(cs.map((c, k) => [c[0], k]));

    g.font = "10px JetBrains Mono, monospace";
    for (let k = 0; k <= 4; k++) {
      const p = lo + (hi - lo) * k / 4, y = Y(p);
      line(g, [[0, y], [w - padR, y]], css("--line"), 1);
      g.fillStyle = css("--dim"); g.fillText(pxAxis(p), w - padR + 6, y + 3);
    }
    const step = Math.max(1, Math.round(cs.length / 6));
    g.fillStyle = css("--dim");
    for (let k = 0; k < cs.length; k += step) {
      const d = new Date(cs[k][0]);
      g.fillText(`${d.toISOString().slice(5, 10)}`, X(k) - 14, h - 4);
    }

    line(g, chart.don_hi.map((v, k) => [X(k), v == null ? null : Y(v)]), "rgba(138,143,176,.55)", 1, [3, 4]);
    line(g, chart.don_lo.map((v, k) => [X(k), v == null ? null : Y(v)]), "rgba(138,143,176,.3)", 1, [3, 4]);
    line(g, chart.ema_slow.map((v, k) => [X(k), v == null ? null : Y(v)]), css("--blue"), 1.4);
    line(g, chart.ema_fast.map((v, k) => [X(k), v == null ? null : Y(v)]), css("--pink"), 1.4);

    const bw = Math.max(1, cw * 0.62);
    cs.forEach(([, o, hgh, l, c], k) => {
      const up = c >= o, col = up ? css("--green") : css("--red");
      g.strokeStyle = col; g.beginPath(); g.moveTo(X(k), Y(hgh)); g.lineTo(X(k), Y(l)); g.stroke();
      g.fillStyle = col;
      const y1 = Y(Math.max(o, c)), y2 = Y(Math.min(o, c));
      g.fillRect(X(k) - bw / 2, y1, bw, Math.max(1, y2 - y1));
    });

    if (chart.stop) {
      line(g, [[0, Y(chart.stop)], [w - padR, Y(chart.stop)]], css("--orange"), 1.2, [6, 4]);
      tag(g, w - padR, Y(chart.stop), `STOP ${pxAxis(chart.stop)}`, css("--orange"));
    }
    if (chart.entry) line(g, [[0, Y(chart.entry)], [w - padR, Y(chart.entry)]], "rgba(241,241,246,.6)", 1, [2, 3]);

    for (const m of chart.markers) {
      const k = nearest(tsIndex, m.ts, chart.bar_ms, cs);
      if (k == null) continue;
      const x = X(k), y = Y(m.price);
      if (m.kind === "entry") {
        g.fillStyle = css("--green"); g.beginPath(); g.moveTo(x, y + 4); g.lineTo(x - 5, y + 12); g.lineTo(x + 5, y + 12); g.fill();
      } else {
        g.fillStyle = m.pnl >= 0 ? css("--green") : css("--red");
        g.beginPath(); g.arc(x, y, 4, 0, Math.PI * 2); g.fill();
        g.strokeStyle = "#0d0f1f"; g.stroke();
      }
    }
    const last = cs[cs.length - 1][4];
    tag(g, w - padR, Y(last), pxAxis(last), css("--green"), true);
    lastGeom = { X, Y, cs, w, h, padR };
  }

  function nearest(index, ts, barMs, cs) {
    const open = ts - (ts - cs[0][0]) % barMs;
    if (index.has(open)) return index.get(open);
    if (ts < cs[0][0]) return null;
    return Math.min(cs.length - 1, Math.floor((ts - cs[0][0]) / barMs));
  }

  function tag(g, x, y, text, color, filled = false) {
    g.save(); g.font = "bold 10px JetBrains Mono, monospace";
    const tw = g.measureText(text).width + 8;
    g.fillStyle = filled ? color : "#0d0f1f"; g.strokeStyle = color;
    g.fillRect(x + 2, y - 8, tw, 16); g.strokeRect(x + 2, y - 8, tw, 16);
    g.fillStyle = filled ? "#0d0f1f" : color; g.fillText(text, x + 6, y + 4); g.restore();
  }

  // ---- the crawler's web: legs reaching out to the last candles' highs ------------
  function drawWeb() {
    const canvas = $("web");
    const { g, w, h } = fit(canvas);
    if (!lastGeom || window.innerWidth < 980) return;
    const { X, Y, cs } = lastGeom;
    const chartTop = $("chart").offsetTop;
    const cx = w * 0.56, cy = h * 0.78;
    const pink = css("--pink");
    const picks = [];
    for (let k = cs.length - 1; k >= 0 && picks.length < 9; k -= Math.max(1, Math.floor(cs.length / 18))) picks.push(k);
    g.save();
    g.strokeStyle = "rgba(255,95,210,.75)"; g.lineWidth = 1.3; g.shadowColor = pink; g.shadowBlur = 6;
    picks.forEach((k, n) => {
      const tx = X(k) + 6, ty = Y(n % 2 ? cs[k][3] : cs[k][2]) + chartTop;
      const kx = (cx + tx) / 2 + (n % 2 ? 18 : -18), ky = (cy + ty) / 2 + 22;
      g.beginPath(); g.moveTo(cx, cy); g.lineTo(kx, ky); g.lineTo(tx, ty); g.stroke();
      g.fillStyle = "#fff"; g.beginPath(); g.arc(tx, ty, 2.4, 0, Math.PI * 2); g.fill();
      g.beginPath(); g.arc(kx, ky, 1.6, 0, Math.PI * 2); g.fill();
    });
    if (document.documentElement.dataset.spider === "on") return g.restore();   // spider.js walks the real one
    g.shadowBlur = 0;
    g.fillStyle = "#1a1d33"; g.strokeStyle = "#fff"; g.lineWidth = 2;
    g.beginPath(); g.ellipse(cx, cy, 13, 10, -0.4, 0, Math.PI * 2); g.fill(); g.stroke();
    g.fillStyle = pink; g.beginPath(); g.ellipse(cx + 12, cy + 6, 8, 6, -0.4, 0, Math.PI * 2); g.fill();
    g.restore();
  }

  // ---- equity + pnl bars ----------------------------------------------------
  function drawEquity(eq) {
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
    const X = (k) => padL + k / (eq.length - 1) * (w - padL - 8);
    const Y = (v) => 6 + (hi - v) / (hi - lo) * (h - 14);
    g.font = "10px JetBrains Mono, monospace"; g.fillStyle = css("--dim");
    [hi, (hi + lo) / 2, lo].forEach((v) => { g.fillText(usdShort(v), 0, Y(v) + 3); line(g, [[padL, Y(v)], [w, Y(v)]], css("--line"), 1); });
    const grad = g.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(62,224,143,.28)"); grad.addColorStop(1, "rgba(62,224,143,0)");
    g.beginPath(); g.moveTo(X(0), h); eq.forEach((p, k) => g.lineTo(X(k), Y(p[1]))); g.lineTo(X(eq.length - 1), h); g.closePath();
    g.fillStyle = grad; g.fill();
    line(g, eq.map((p, k) => [X(k), Y(p[1])]), css("--green"), 2);
    const lx = X(eq.length - 1), ly = Y(ys[ys.length - 1]);
    g.fillStyle = css("--green"); g.beginPath(); g.arc(lx, ly, 4, 0, Math.PI * 2); g.fill();
    g.strokeStyle = "rgba(62,224,143,.35)"; g.lineWidth = 4; g.beginPath(); g.arc(lx, ly, 8, 0, Math.PI * 2); g.stroke();
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

  function renderStage(s) {
    const t = s.ticker, k = s.kills, coin = s.header.coin || "BTC";
    const liveTag = live.at ? `<span class="live-dot" aria-hidden="true"></span>LIVE · ` : "";
    $("ticker").innerHTML = `‹ ${liveTag}TRADES · ${esc(coin)} ${px(t.price)} · BANK ${usd(t.bank)} · ` +
      `<span class="hl">${esc(t.position)}${t.position_detail ? " " + esc(t.position_detail) : ""}</span>` +
      ` · ${esc(t.record)} · HOLD ${pct(t.hold_pct)} · P&amp;L ${usd(t.pnl)} (${pct(t.pnl_pct)}) · FEES ${usd(t.fees)} · FUNDING ${usd(t.funding)}`;
    $("chart-title").textContent = `${coin}-PERP · ${s.header.interval} · ${px(t.price)}`;
    $("chart-sub").textContent = `24h ${pct(t.change_24h)} · ema.fast pink · ema.slow blue · channel dashed`;
    const chips = $("chips");
    chips.innerHTML = s.agents.map((a) => `<span class="chip ${a.state === "idle" ? "off" : ""}" title="${esc(a.name)}: ${esc(a.state)}"
      style="background:var(${COLORS[a.key]})">${esc(a.name[0])}</span>`).join("");
    if (!$("chief-sprite").children.length) $("chief-sprite").append(sprite("chief", 4));
    $("mini-time").textContent = s.header.time;
    $("kill-state").innerHTML = [
      ["flat_kill", k.flat_kill], ["entry_kill", k.entry_kill], ["jev_kill", k.jev_kill],
      ["drawdown halt", k.halted], ["daily loss lock", k.daily_locked], ["cooldown", k.cooldown],
    ].map(([n, on]) => `<div class="${on ? "on" : ""}"><span>${n}</span><span>${on ? "ON" : "off"}</span></div>`).join("");
    const j = s.jev, ans = j.answers;
    const row = (label, v) => `<div class="q"><span>${label}</span><div class="bar"><i style="width:${Math.round((v ?? 0) * 100)}%;background:var(--white)"></i></div></div>`;
    $("jevbox").innerHTML = `<div class="q"><span>MODE</span>${esc(j.mode)}${j.enabled ? "" : " · no key"} · ${j.calls} calls</div>` +
      (ans ? row(`spent ${num(ans.momentum_already_spent?.noul)}`, ans.momentum_already_spent?.noul) +
             row(`follow-through ${num(ans.follow_through?.noul)}`, ans.follow_through?.noul) +
             row(`trend ${num(ans.regime?.probabilities?.trend)}`, ans.regime?.probabilities?.trend)
           : `<div class="q"><span>LAST ANSWER</span>asked only when every other gate passes</div>`);
  }

  function renderBalance(s) {
    const t = s.ticker, goal = s.goal;
    $("bal-day").textContent = `DAY ${s.header.day}`;
    $("bal-mode").textContent = s.header.mode;
    $("bal-record").textContent = t.record;
    $("bal-hold").textContent = pct(t.hold_pct);
    $("balance").textContent = usd(t.bank);
    $("balance").style.color = t.pnl >= 0 ? "var(--green)" : "var(--red)";
    if (goal) {
      $("goal-from").textContent = usd(goal.start, 0);
      $("goal-to").textContent = usd(goal.target, 0);
      $("goal-bar").style.width = `${(goal.progress * 100).toFixed(1)}%`;
      const yrs = (y) => y == null ? "—" : y > 200 ? "200+ years" : `${y.toFixed(y < 10 ? 1 : 0)} years`;
      $("goal-note").innerHTML = `${(goal.progress * 100).toFixed(1)}% of the way (log scale) · ${num(goal.multiple_left, 0)}x to go<br>` +
        `at backtest pace (${pct(goal.backtest_cagr, 1)}/yr): ${yrs(goal.years_at_backtest_rate)}` +
        (goal.realised_cagr != null ? `<br>at this account's own pace (${pct(goal.realised_cagr, 1)}/yr): ${yrs(goal.years_at_realised_rate)}` : "");
    }
  }

  const GATE_FMT = (g) => {
    if (g.value == null) return g.note || "";
    const v = Math.abs(g.value) >= 1000 ? Math.round(g.value).toLocaleString("en-US") : num(g.value, 2);
    const t = g.threshold == null ? "" : ` ${g.op === "min" ? "≥" : g.op === "max" ? "≤" : "vs"} ${Math.abs(g.threshold) >= 1000 ? Math.round(g.threshold).toLocaleString("en-US") : num(g.threshold, 2)}`;
    return `${v}${t}`;
  };

  function renderAnalysis(s) {
    const st = s.analysis.stats, basket = s.header.kind === "basket", t = s.ticker;
    $("analysis-title").textContent = basket ? "BASKET · ANALYSIS" : "DESK · ANALYSIS";
    $("scanned").textContent = `${s.analysis.scanned.toLocaleString("en-US")} ${basket ? "DAILY CLOSES" : "BARS SCANNED"}`;
    const cells = basket ? [
      ["$" + s.header.coin, "basket", "sym"], ["Cash", usd(t.cash)], ["Held", usd(t.notional)],
      ["Exposure", `${num(t.exposure)}x`], ["Unrealised", usd(t.unrealised)], ["Fees", usd(t.fees)], ["Funding", usd(t.funding)],
    ] : [
      ["$" + (s.header.coin || "BTC"), "perp · " + s.header.venue, "sym"], ["Price", px(st.price)], ["24h", pct(st.change_24h)],
      ["ATR", `${num(st.atr_pct)}%`], ["Funding APR", `${num(st.funding_apr, 1)}%`],
      ["Open interest", usdShort(st.open_interest_usd)], ["Spread", st.spread_bps == null ? "—" : `${num(st.spread_bps, 2)} bps`],
    ];
    $("stats").innerHTML = cells.map(([k, v, cls]) => `<div class="${cls || ""}"><span>${k}</span><b>${esc(v)}</b></div>`).join("");
    const ol = s.analysis.outlook || [];
    const day = (ms) => new Date(ms).toLocaleString("en-US", { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" });
    const olHead = s.analysis.outlook_side === "short"
      ? `SHORT LINE · a 2h close below it shorts; steps up as old lows age out (if ${esc(s.header.coin || "BTC")} makes no new low)`
      : `BUY LINE · steps down as old highs age out (if ${esc(s.header.coin || "BTC")} makes no new high)`;
    $("outlook").innerHTML = (s.header.experiment ? `<div class="ol-exp">EXPERIMENT · ${esc(s.header.experiment)}</div>` : "") +
      (ol.length ? `<div class="ol-head">${olHead}</div>` +
      `<div class="ol-rows">` + ol.map((r, k) => `<div class="${k === 0 ? "now" : ""}"><span>${k === 0 ? "next close" : day(r.ts) + " UTC"}</span>` +
      `<b>${px(r.level)}</b><i>${pct(r.level / st.price - 1, 1)}</i></div>`).join("") + `</div>` : "");
    $("gates").innerHTML = s.analysis.gates.map((g) =>
      `<li><span>${esc(g.name.replace(/_/g, " "))} <i class="na">${esc(GATE_FMT(g))}</i></span><span class="v ${g.verdict}">${g.verdict}</span></li>`).join("");
    const when = basket ? mmddhhmm : hhmm;   // the basket trades once a day: its events need the date
    $("events").innerHTML = s.events.slice().reverse().map((e) =>
      `<li><b style="color:var(${COLORS[e.agent] || "--muted"})">${esc(e.agent.toUpperCase())}</b>${when(e.ts)} ${esc(e.text)}</li>`).join("") ||
      `<li>no events yet — the desk speaks when something happens.</li>`;
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
    const liveTag = live.at ? `<span class="live-dot" aria-hidden="true"></span>LIVE · ` : "";
    $("vt-ticker").innerHTML = `‹ ${liveTag}BASKET · ${esc(s.header.coins.join(" · "))} · BANK ${usd(t.bank)} · ` +
      `<span class="hl">${esc(t.position)}${t.position_detail ? " " + esc(t.position_detail) : ""}</span>` +
      ` · ${esc(t.record)} · HOLD ${pct(t.hold_pct)} · P&amp;L ${usd(t.pnl)} (${pct(t.pnl_pct)}) · FEES ${usd(t.fees)} · FUNDING ${usd(t.funding)}`;
    const next = Math.floor(now / DAY_MS + 1) * DAY_MS, left = next - now;   // every 00:00 UTC
    $("vt-next").textContent = `NEXT DECISION 00:00 UTC · IN ${Math.floor(left / 3600000)}H ${Math.floor(left % 3600000 / 60000)}M`;
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
        : h.qty > 0 ? ["HOLD", h.entry_ts ? `since ${new Date(h.entry_ts).toISOString().slice(0, 10)}` : ""]
        : h.target_units > 0 ? ["FLAT", `entry under the $${r.min_order_usd} minimum; checked again at the next close`]
        : ["FLAT", h.warm ? `waits for a close above the ${r.sma_bars}-day average` : "warming up the averages"];
      return `<div class="h-row">` +
        `<div class="h-coin"><span>COIN</span><b>${esc(h.coin)}</b><i>${px(h.mark)}</i></div>` +
        cell(1, gap, h.sma50 ? `avg ${px(h.sma50)}` : "warming up") +
        cell(2, h.vol10 == null ? "—" : `${(h.vol10 * 100).toFixed(0)}%`, "annualised") +
        cell(3, `${num(h.held_units)} held`, `target ${num(h.target_units ?? 0)}${band}`) +
        cell(4, usd(h.notional), h.qty > 0 ? `${(h.weight * 100).toFixed(1)}% of the bank · ${num(h.qty, 5)} ${esc(h.coin)}` : "") +
        cell(5, h.qty > 0 ? `<span class="${h.unrealised >= 0 ? "ok" : "bad"}">${usd(h.unrealised)}</span>` : "—", h.qty > 0 ? `avg entry ${px(h.avg_entry)}` : "") +
        cell(6, order[0], order[1], o ? "pend" : "") + `</div>`;
    });
    $("holdings").innerHTML = `<div class="h-row h-head">${head.map((x) => `<div>${x}</div>`).join("")}</div>` + rows.join("");
    const caught = b.last_close_ts && b.last_close_ts < Math.floor(now / DAY_MS) * DAY_MS
      ? " The latest 00:00 close is picked up on the next run (every 15 minutes)." : "";
    $("vt-note").textContent = `Prices live from Hyperliquid; averages, vol and units are from the last close` +
      (b.last_close_ts ? ` (${new Date(b.last_close_ts).toISOString().slice(0, 10)} 00:00 UTC)` : "") + `. ` +
      `${b.orders.filled} fills, ${b.orders.blocked} orders skipped by the $${r.min_order_usd} minimum.${caught}`;
  }

  const RULES = {
    coins: "one sleeve each, an equal share of the book", interval: "decided at the 00:00 UTC close, filled at the next open",
    vol_lookback: "days of returns in the realised vol (x √365)", target_vol: "units = this ÷ the coin's realised vol",
    max_units: "no coin gets more units than this", sma_bars: "held only while the close is above this many days' average",
    band: "held units change only when the new size is further away than this", book_leverage: "a coin's notional = bank x this ÷ 3 x units",
    min_order_usd: "Hyperliquid's minimum order: smaller entries and resizes are skipped", starting_equity: "the paper account's start",
    goal_equity: "the goal the balance panel tracks",
  };
  function renderRules(rules) {
    const fmt = (k, v) => Array.isArray(v) ? v.join(" · ") : /usd|equity/.test(k) ? usd(v, 0) : String(v);
    $("thr").innerHTML = `<div class="thr-sec">VolTrend — frozen: these exact numbers passed the edge hunt's pre-set bar; not tuned</div>` +
      Object.entries(rules).map(([k, v]) => `<div class="rule"><span class="n">${esc(k)}</span><b>${esc(fmt(k, v))}</b><i>${esc(RULES[k] || "")}</i></div>`).join("");
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

  async function hl(body) {
    const res = await fetch(HL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(res.status);
    return res.json();
  }

  // A bank re-marked at live mids: its P&L, the goal, and a point on the equity curve.
  function rebank(s, bank) {
    const t = s.ticker, g = s.goal;
    t.bank = bank; t.pnl = bank - g.start; t.pnl_pct = bank / g.start - 1;
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

  // Overlay live prices on the last published snapshot: price, unrealised P&L, balance, forming candle.
  function withLive(snap) {
    if (!snap) return snap;
    const s = JSON.parse(JSON.stringify(snap));
    if (s.header.kind === "basket") return withLiveBasket(s);
    const mid = parseFloat(live.mids[s.header.coin || "BTC"]);
    if (!mid) return s;
    const t = s.ticker;
    t.price = mid; s.analysis.stats.price = mid;
    const p = t.pos;
    const u = p ? (p.side === "long" ? 1 : -1) * p.qty * (mid - p.entry) : 0;
    if (t.cash != null && t.cash + u > 0) {
      t.hold_pct = p ? u / (p.qty * p.entry) : 0;
      rebank(s, t.cash + u);
    }
    const c = live.candle, ch = s.chart;
    if (c && live.candleCoin === (s.header.coin || "BTC") && ch.candles.length && c[0] > ch.candles[ch.candles.length - 1][0]) {
      c[4] = mid; c[2] = Math.max(c[2], mid); c[3] = Math.min(c[3], mid);
      ch.candles.push(c);
      const line = (s.analysis.outlook && s.analysis.outlook[0]) ? s.analysis.outlook[0].level : null;
      const short = s.analysis.outlook_side === "short";
      ch.ema_fast.push(null); ch.ema_slow.push(null);
      ch.don_lo.push(short ? line : null); ch.don_hi.push(short ? null : line);
    }
    return s;
  }

  function renderCoins() {
    const nav = $("coins");
    if (!index || !index.coins) { nav.hidden = true; return; }
    nav.hidden = false;
    let start = 0, bank = 0;
    const cards = index.coins.map((c) => {
      if (c.error) return `<button class="coin" data-coin="${esc(c.coin)}"><span class="c-name">${esc(c.coin)}</span><span class="c-sub">error: ${esc(c.error)}</span></button>`;
      const basket = c.kind === "basket", lp = parseFloat(live.mids[c.coin]);
      const open = c.coin === (state && state.header.coin) && state.ticker.bank != null;
      let b = open ? state.ticker.bank : c.bank;
      if (basket && !open && live.at && c.holdings) {   // the basket's tab isn't open: re-mark it from the same mids
        const m = basketLive(c.cash, JSON.parse(JSON.stringify(c.holdings)));
        if (m.bank > 0) b = m.bank;
      }
      start += c.start; bank += b;
      const label = basket ? (c.holdings || []).map((h) => h.coin).join("·") : lp ? px(lp) : px(c.price);
      const sub = basket ? " · basket" : ` · risk ${(c.risk_per_trade * 100).toFixed(0)}%${c.shorts ? " · +shorts" : ""}`;
      return `<button class="coin ${c.coin === coin ? "on" : ""}" data-coin="${esc(c.coin)}" aria-pressed="${c.coin === coin}">` +
        `<span class="c-name">${esc(c.coin)}<i>${esc(label)}</i></span><span class="c-bank">${usd(b)}</span>` +
        `<span class="c-sub">${pct(b / c.start - 1)} · ${esc(c.position)} · ${esc(c.record)}${sub}</span></button>`;
    });
    cards.push(`<div class="coin total"><span class="c-name">ALL</span><span class="c-bank">${usd(bank)}</span>` +
      `<span class="c-sub">${pct(start ? bank / start - 1 : 0)} on ${usd(start, 0)} paper</span></div>`);
    nav.style.setProperty("--tabs", cards.length);
    nav.innerHTML = cards.join("");
    nav.querySelectorAll("[data-coin]").forEach((b) => b.addEventListener("click", () => {
      coin = b.dataset.coin; storeSet("desk-coin", coin); history.replaceState(null, "", "#" + coin); missing = null;
      live.candle = null; src = `boards/${coin}.json`; tick().then(pollCandle);   // the candle for the new tab's board
    }));
  }

  function render() {
    state = withLive(base);
    if (!state) return;
    const s = state, hdr = s.header;
    $("title").textContent = hdr.title;
    $("tagline").textContent = hdr.tagline;
    $("mode").textContent = hdr.mode; $("mode").className = `mode ${hdr.mode.toLowerCase()}`;
    $("daydate").textContent = `DAY ${hdr.day} · ${hdr.date}`;
    $("time").textContent = hdr.time;
    const basket = hdr.kind === "basket";
    document.body.classList.toggle("kind-basket", basket);
    renderCoins();
    if (basket) renderBasket(s);
    else { renderAgents(s.agents); renderStage(s); renderBubbles(s.agents); drawChart(s.chart); drawWeb(); }
    renderBalance(s); drawEquity(s.equity); drawPnlBars(s.pnl_bars);
    renderAnalysis(s); if (basket) renderRules(s.basket.rules); else renderThresholds(s.thresholds);
    renderKills(s.kills);
    const age = Math.round((Date.now() - s.updated) / 60000);
    $("foot").textContent = `desk updated ${new Date(s.updated).toISOString().replace("T", " ").slice(0, 16)} UTC (${age} min ago)` +
      (live.at ? ` · live price ${Math.round((Date.now() - live.at) / 1000)}s ago from Hyperliquid` : "") +
      (missing ? ` · no ${missing} board this run (its step failed), showing ${hdr.coin || "BTC"}` : "") +
      ` · not financial advice; the desk can and will lose money`;
    if (staticMode) {
      document.querySelectorAll("[data-kill]").forEach((b) => { b.disabled = true; });
      $("kill-hint").textContent = "read-only board: flip switches in ledger/<COIN>/kills.json on GitHub";
    }
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
      if (res.status === 404 && staticMode && coin !== "BTC" && src === `boards/${coin}.json`) {
        // show BTC's board (and the tab row) instead of a blank page; the remembered tab stays, for when it's back
        missing = coin; coin = "BTC"; src = "boards/BTC.json";
        res = await fetch(`${src}?t=${Date.now()}`, { cache: "no-store" });
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
        agents: (base.agents || []).map((a) => a.key), static: staticMode });
    } catch (err) {
      if (mine === asked) $("foot").textContent = `can't reach the desk (${err.message}). is \`python -m desk paper\` running?`;
    }
  }

  async function pollMids() {
    try { live.mids = await hl({ type: "allMids" }); live.at = Date.now(); render(); emit("desk:tick", { price: state && state.ticker.price }); } catch { /* keep the last price */ }
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

  window.addEventListener("resize", render);
  (async function loop() {
    await tick();
    setTimeout(loop, staticMode ? 60000 : 2000);
  })();
  pollMids(); pollCandle();
  setInterval(pollMids, 5000);
  setInterval(pollCandle, 30000);
})();
