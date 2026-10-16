// BTC SHADOW: the real Coinbase account, read only, with the orders LIVE would send simulated (desk/coinbase_run.py).
// The site is public, so the board is encrypted (desk/statebox.py: PBKDF2-SHA256 then AES-256-GCM) and opened here,
// on the owner's device, with DESK_STATE_KEY. The key never leaves the browser: WebCrypto does the work. It is kept
// for this tab only (sessionStorage) unless "remember" is ticked (localStorage; every project page on this
// github.io origin can read that, so it's off by default). The public status (boards/BTC-SHADOW.status.json) has no
// amounts: it's shown without the key.
(function () {
  "use strict";
  const BOARD_AAD = "btc-perp-desk:board-shadow:BTC";
  const STALE_MS = 60 * 60 * 1000;      // a status older than this: the job isn't running (GitHub runs it ~every 15 min)

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const usd = (x, dp = 2) => x == null || isNaN(x) ? "—" : (x < 0 ? "-$" : "$") + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const signed = (x) => x == null || isNaN(x) ? "—" : (x >= 0 ? "+" : "-") + usd(Math.abs(x)).slice(0);
  // Every time on this page is Mountain Time, America/Denver's (MDT in summer, MST in winter: the zone, never a fixed
  // offset), like "Oct 15, 10:08 am MT". What's stored stays UTC epoch ms.
  const MT = new Intl.DateTimeFormat("en-US", { timeZone: "America/Denver", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", hour12: true });
  const mt = (ms) => { const p = {}; for (const x of MT.formatToParts(new Date(ms))) p[x.type] = x.value; return p; };
  const day = (ms) => { if (!ms) return "—"; const p = mt(ms); return `${p.month} ${p.day}`; };
  const clock = (ms) => { const p = mt(ms); return `${p.hour}:${p.minute} ${String(p.dayPeriod || "").toLowerCase()} MT`; };
  const when = (ms) => ms ? `${day(ms)}, ${clock(ms)}` : "—";
  const ago = (ms, now) => ms ? `${Math.max(0, Math.round((now - ms) / 60000))} min ago` : "never";
  const bps = (x) => x == null || x === "" || isNaN(x) ? "—" : `${+(x * 1e4).toFixed(2)}bps`;

  // Coinbase's codes in plain words: "FCM_MARGIN_WINDOW_TYPE_OVERNIGHT" reads "overnight". A code loses its type's
  // prefix, then the few that read badly as they are have their own words; any other reads in lower case with spaces
  // ("FCM_MARGIN_WINDOW_TYPE_LUNCH": "lunch"), so a code Coinbase adds later still says something. Words that aren't a
  // code ("Advanced 1") stay as they are. The value Coinbase sent stays in the title (a desktop shows it on hover).
  // The codes the page shows: the margin window and setting, a sweep's status, sides, a real order's type, a simulated
  // stop's trigger and status, and the portfolio in an older board's check (desk/coinbase_run.py words it now)
  const PREFIXES = /^(FCM_MARGIN_WINDOW_TYPE|MARGIN_WINDOW_TYPE|INTRADAY_MARGIN_SETTING)_/;
  const WORDS = {
    UNSPECIFIED: "not said", UNKNOWN_FCM_SWEEP_STATUS: "unknown", STOP_LIMIT: "stop-limit",
    STOP_PENDING: "waiting to trigger", STOP_TRIGGERED: "triggered", QUEUED: "queued for the reopen",
    DEFAULT: "Default", CONSUMER: "Consumer", INTX: "International (INTX)", UNDEFINED: "unnamed",
  };
  // the desk's own name for what stopped a read, as a failed check's note ends ("candles: CoinbaseUnavailable",
  // desk/brokers/coinbase_rest.py), in the words desk/coinbase_run.py's failure() says it; another name stays as it is
  const ERRORS = {
    CoinbaseUnavailable: "Coinbase didn't answer", DeadlineExceeded: "Coinbase didn't answer",
    CoinbaseUncertain: "Coinbase didn't answer", CoinbaseDataError: "an answer the desk can't read",
    FeedError: "an answer the desk can't read", CoinbaseRejected: "Coinbase refused the read",
    CoinbaseAuthError: "Coinbase refused the key", CoinbasePermissionError: "a read the key isn't allowed to make",
    ClockSkewError: "the clock too far off Coinbase's", CoinbaseError: "a check of the desk's own",
  };
  const isCode = (s) => /^[A-Z0-9]+(_[A-Z0-9]+)*$/.test(s) || /^[a-z0-9]+(_[a-z0-9]+)+$/.test(s);
  function plain(code) {
    const raw = String(code ?? "").trim();
    if (!isCode(raw)) return raw;
    const k = raw.toUpperCase(), rest = k.replace(PREFIXES, "");
    return WORDS[k] || WORDS[rest] || rest.toLowerCase().replace(/_/g, " ") || raw;
  }
  // as HTML: the plain words, escaped, with the code itself (escaped) as the title when they differ (a <data>, not a
  // <span>: the stats rows style their spans as labels); `none` when missing
  const coded = (code, none = "—") => {
    const raw = String(code ?? "").trim(), said = plain(raw);
    if (!raw) return none;
    return said === raw ? esc(raw) : `<data value="${esc(raw)}" title="${esc(raw)}">${esc(said)}</data>`;
  };
  // a check's note, as HTML: one that ends in the desk's name for an error has that part in words, the name in the title
  const noted = (note) => {
    const m = /^(.*?)\b([A-Z][A-Za-z]+)$/.exec(String(note ?? "").trim());
    if (!m || !ERRORS[m[2]]) return coded(note);
    return `${esc(m[1])}<data value="${esc(m[2])}" title="${esc(m[2])}">${esc(ERRORS[m[2]])}</data>`;
  };

  function b64(s) {
    const bin = atob(String(s));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  // The board: an envelope {v, kdf, iter, salt, iv, aad, zlib: false, ct}. Throws on a wrong key (AES-GCM's tag).
  async function decrypt(env, password, subtle) {
    subtle = subtle || crypto.subtle;
    if (!env || env.v !== 1 || env.kdf !== "pbkdf2-sha256" || env.aad !== BOARD_AAD || env.zlib) throw new Error("not the SHADOW board");
    if (!(env.iter >= 1000 && env.iter <= 5000000)) throw new Error("not the SHADOW board");
    const enc = new TextEncoder();
    const base = await subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveKey"]);
    const key = await subtle.deriveKey({ name: "PBKDF2", salt: b64(env.salt), iterations: env.iter, hash: "SHA-256" },
      base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    const opened = await subtle.decrypt({ name: "AES-GCM", iv: b64(env.iv), additionalData: enc.encode(env.aad) }, key, b64(env.ct));
    return JSON.parse(new TextDecoder().decode(opened));
  }

  // ---- the public status: no amounts, no key needed
  // board: whether the encrypted board is published (a board with no status: BTC/status.json is gone from the branch)
  function statusHtml(st, now, board) {
    if (!st && board) return `<b class="bad">NOT SAVED: the SHADOW status is missing from the coinbase-state branch</b> (BTC/status.json deleted or edited, say), so this page stops moving. The next SHADOW run that opens the saved state writes it back from it (your phone gets BTC SHADOW: NOT SAVED, then working again); a run refused over a secret first writes nothing, so fix the secret and wait for the next run. If the saved state was deleted too, every run is refused (BTC SHADOW: not working) until you put both back as they were (revert the commit that deleted them), or delete the coinbase-state branch to start over (docs/coinbase-setup.md, step 22).`;
    if (!st) return "No SHADOW status on the coinbase-state branch. SHADOW doesn't run until the COINBASE_CDP_KEY and DESK_STATE_KEY secrets are both set, and its first run saves one. If SHADOW ran before, BTC/status.json (and the SHADOW board) were deleted from the branch, or every run is being refused before it can save: your phone says why (BTC SHADOW: not working), and so does the shadow job's log on the Actions tab (docs/coinbase-setup.md, step 22).";
    const stale = now - st.updated > STALE_MS;
    const state = !st.ok ? `<b class="bad">NOT WORKING (${esc(st.problem || "error")})</b>` + (st.note ? `: ${esc(st.note)}` : "") : stale ? `<b class="bad">NOT UPDATED for ${ago(st.updated, now)}</b>` : `<b class="ok">OK</b>` + (st.note ? ` · <b class="bad">${esc(st.note)}</b>` : "");
    return `${esc(st.mode)} ${state} · updated ${when(st.updated)} (${ago(st.updated, now)}) · ` +
      (st.runs ? `run ${esc(st.runs)}` : "no run has worked yet") +
      (st.failed_runs ? ` (${esc(st.failed_runs)} failed)` : "") +
      ` · key ${st.key_view_only ? "View only" : st.key_view_only === false ? "<b class='bad'>NOT view only</b>" : "not checked"}` +
      ` · checks ${esc(st.checks_passed ?? "—")}/${esc(st.checks_total ?? "—")}` +
      (st.decision ? ` · desk: ${esc(st.decision)}` : "") +
      (st.would_send_last_24h != null ? ` · ${esc(st.would_send_last_24h)} order(s) it would have sent in 24h (none sent)` : "") +
      (st.alerts_open ? ` · <b class="bad">${esc(st.alerts_open)} alert(s)</b>` : "") +
      (st.opened ? "" : (st.runs || 0) > (st.failed_runs || 0) ? " · the account isn't open yet (no money in it)" :
        " · the balance hasn't been read yet") +
      (stale ? ` · GitHub runs this about every 15 minutes, best effort: check the Actions tab` : "");
  }

  // ---- the board, once opened
  function rows(pairs) {
    return pairs.filter(Boolean).map(([k, v, cls]) => `<div class="${cls || ""}"><span>${esc(k)}</span><b>${v}</b></div>`).join("");
  }

  // what every board shows, a SHADOW that never worked included: its run's checks, its alerts, its own log. `run`:
  // "THIS RUN" when the board is the last run's, else the earlier run's time (a run since saved only its status)
  const checksHtml = (sh, run) => `<section class="panel sh-checks"><div class="panel-head"><span>EVERY READ, ${esc(run || "THIS RUN")}</span><span class="muted">a View-only key: it can't trade or move money</span></div><ul>` +
    (sh.checks || []).map((c) => `<li class="${c.ok === true ? "ok" : c.ok === false ? "bad" : "muted"}"><b>${c.ok === true ? "OK" : c.ok === false ? "FAIL" : "—"}</b> ${esc(c.label)}${c.note ? `<i>${noted(c.note)}</i>` : ""}</li>`).join("") + `</ul></section>`;
  const alertsHtml = (sh, earlier) => (sh.alerts || []).length ? `<div class="panel-head"><span>ALERTS ${earlier ? "THAT RUN" : "THIS RUN"}</span></div><ul>${sh.alerts.map((x) => `<li class="bad">${esc(x)}</li>`).join("")}</ul>` : "";
  const logHtml = (sh, open, earlier) => `<details${open ? " open" : ""}><summary>${earlier ? "that run's log (private)" : "this run's log (private)"}</summary><pre>${esc((sh.log || []).join("\n"))}</pre></details>`;

  // pub: the public status, when it's published. A status newer than the board's own run means a run since saved its
  // status alone (refused before it could save a board): the board is an earlier run's, and says so
  function boardHtml(s, now, pub) {
    const sh = s.shadow || {}, a = s.account || {}, t = s.ticker || {}, st = s.status || {};
    const out = [];
    const earlier = !!(pub && pub.updated && st.checked && pub.updated > st.checked);
    const run = earlier ? `THE RUN OF ${when(st.checked).toUpperCase()}` : "THIS RUN";
    const since = earlier ? ` The last run, ${when(pub.updated)}, saved only its status (above), which says why it stopped.` : "";
    if (!s.updated) {
      // no full board: no run has worked yet (or the last board was deleted), so only that run's checks, alerts and
      // log (what stopped it)
      return `<section class="panel sh-warn"><b>${st.runs > 0 ? (earlier ? "That run didn't finish" : "This run didn't finish") + ", and there's no earlier board to show" : "No SHADOW run has worked yet"}</b>` +
        (st.problem ? ` (${esc(st.problem)})` : "") +
        `, so there are no numbers to show. Below: what ${earlier ? "an earlier run" : "the last run"}, ${when(st.checked)}, checked, and its log.${since}` +
        (st.why ? `<br><span class="muted">${esc(st.why)}</span>` : "") + `</section>` + checksHtml(sh, run) +
        `<section class="panel sh-events">${alertsHtml(sh, earlier)}${logHtml(sh, true, earlier)}</section>`;
    }
    const stale = s.updated && now - s.updated > STALE_MS;
    if (!st.ok || stale || earlier) {
      out.push(`<section class="panel sh-warn"><b>${earlier ? "This board is from an earlier run" : !st.ok ? "This run didn't finish" : "Not updated for " + ago(s.updated, now)}</b>` +
        (st.problem ? ` (${esc(st.problem)})` : "") + `. The numbers below are from ${when(s.updated)}.${since}` +
        (st.why ? `<br><span class="muted">${esc(st.why)}</span>` : "") + `</section>`);
    }
    // the two balances, side by side: the real account, and what LIVE would hold (an estimate)
    const sim = sh.simulated_equity, real = sh.real_equity;
    out.push(`<section class="panel sh-money"><div class="panel-head"><span>MONEY</span><span class="muted">real = Coinbase's numbers · SHADOW = real + simulated trades (est.)</span></div>` +
      `<div class="sh-two"><div><div class="label muted">REAL ACCOUNT (COINBASE)</div><div class="big">${usd(real)}</div>` +
      `<div class="stats">${rows([
        ["spot USD", usd(a.spot_usd)], ["futures USD", usd(a.futures_usd)], ["open P&L", usd(a.open_pnl)],
        ["P&L not settled yet", usd(a.unsettled_pnl)], ["in transit (sweeps)", usd(a.in_transit)],
        ["buying power", usd(a.buying_power)], ["available margin", usd(a.available_margin)], ["holds", usd(a.holds)],
        ["liquidation buffer", a.liquidation_buffer_pct == null ? "—" : `${Number(a.liquidation_buffer_pct).toFixed(1)}%`],
        ["margin window", coded(a.margin_window)], ["intraday margin", coded(a.intraday_setting)],
        // the two warnings: the row in red (a span in the value would take the label's grey)
        a.usdc ? ["USDC in the portfolio", isNaN(a.usdc) ? esc(a.usdc) : `${usd(+a.usdc).replace("$", "")} USDC`, "bad"] : null,
        a.balance_block ? ["balance check", esc(a.balance_block), "bad"] : ["balance check", "adds up"],
      ])}</div></div>` +
      `<div><div class="label muted">SHADOW (EST.)</div><div class="big">${usd(sim)}</div><div class="stats">${rows([
        ["simulated P&L (est.)", signed(sh.simulated_pnl)], ["position", coded(t.position) + (t.position_detail ? " " + esc(t.position_detail) : "")],
        ["record", esc(t.record || "—")], ["fees (est.)", usd(t.fees)], ["funding (est.)", usd(t.funding)],
        sh.opened ? ["opened", `${usd(sh.opened.amount)} on ${esc(sh.opened.ts ? day(sh.opened.ts) : sh.opened.date)} (${esc(sh.opened.what)}): money in, not profit`] : ["opened", "not yet: no money in the account"],
      ])}</div></div></div></section>`);
    // what the desk thinks
    const dec = (s.agents || []).find((g) => g.key === "chief");
    const blocked = (s.analysis && s.analysis.gates || []).find((g) => g.verdict === "DROP" || g.verdict === "WAIT");
    out.push(`<section class="panel sh-desk"><div class="panel-head"><span>THE DESK</span><span class="muted">decided on Coinbase's 2h candles, as LIVE would</span></div><div class="sh-body">` +
      `<p><b>${esc(dec ? dec.value : "—")}</b>` + (blocked ? ` · ${esc(blocked.name.replace(/_/g, " "))}${blocked.note ? ": " + esc(blocked.note) : ""}` : "") + `</p>` +
      (sh.paper_check ? `<p class="${sh.paper_check.ok === false ? "bad" : "muted"}">${esc(sh.paper_check.note)}</p>` : "") +
      (sh.min_equity_for_one ? `<p>${esc(sh.min_equity_for_one.note)}</p>` : "") +
      (sh.what_if ? `<p class="amber">${esc(sh.what_if.text)}</p>` : "") +
      (sh.risk ? `<p>${esc(sh.risk.text)}</p>` : "") +
      (sh.entry_kill_reason ? `<p class="bad">${esc(sh.entry_kill_reason)}</p>` : "") +
      `</div></section>`);
    // the checklist
    out.push(checksHtml(sh, run));
    // what it would have sent: LIVE's exact requests, none sent
    const ws = (sh.would_send || []).slice().reverse();
    out.push(`<section class="panel sh-sends"><div class="panel-head"><span>ORDERS IT WOULD SEND</span><span class="muted">LIVE's exact requests · NONE SENT · fills simulated</span></div><ul>` +
      (ws.length ? ws.map((r) => `<li>${esc(day(r.ts))} ${esc(r.text)}<details><summary>request body</summary><pre>${esc(JSON.stringify(r.body, null, 1))}</pre></details></li>`).join("") : "<li class='muted'>none yet</li>") + `</ul></section>`);
    // the virtual book (a resting stop says its trigger, or, while the market is shut, that it's queued for the
    // reopen), and what the real account holds besides
    const book = sh.book || {};
    out.push(`<section class="panel sh-book"><div class="panel-head"><span>SIMULATED BOOK</span><span class="muted">what LIVE would hold on Coinbase now</span></div><div class="stats">${rows([
      ["position", book.position ? `${coded(book.position.side)} ${esc(book.position.contracts)} contract(s) @ ${usd(book.position.entry)} · open P&L ${usd(book.position.upnl)} (est.)` : "flat"],
      ["resting orders", (book.orders || []).length ? book.orders.map((o) => `${coded(o.side)} ${esc(o.n)} stop ${usd(o.stop, 0)} limit ${usd(o.limit, 0)} (${coded(o.status === "QUEUED" ? o.status : o.trigger || o.status)})`).join("<br>") : "none"],
      ["fees, funding (est.)", `${usd(book.fees)} · ${usd(book.funding)}`],
    ])}</div>` +
      `<div class="panel-head"><span>THE REAL ACCOUNT HOLDS</span><span class="muted">positions and orders the desk didn't place block its entries</span></div><div class="stats">${rows([
        ["positions", (a.positions || []).length ? a.positions.map((p) => `${esc(p.product_id)} ${coded(p.side)} ${esc(p.contracts)}`).join("<br>") : "none"],
        ["open orders", (a.orders || []).length ? a.orders.map((o) => `${esc(o.product_id)} ${coded(o.side)} ${o.type === "other" ? "(an order type the desk doesn't know)" : coded(o.type, "")}`).join("<br>") : "none"],
        ["fills (BIP, last 30 days)", (a.fills || []).length ? `${a.fills.length}` : "none"],
        ["pending sweeps", (a.sweeps || []).length ? a.sweeps.map((w) => `${coded(w.status)} ${w.all || w.amount == null || w.amount === "" ? "all" : isNaN(w.amount) ? esc(w.amount) : usd(+w.amount)}`).join("<br>") : "none"],
        ["fee tier", a.fee_tier ? `${coded(a.fee_tier.tier)} · taker ${bps(a.fee_tier.taker)} · maker ${bps(a.fee_tier.maker)} (fees on the board stay the estimate)` : "—"],
      ])}</div></section>`);
    // events and alerts, then the honest small print and the run's own log
    const ev = (s.events || []).slice().reverse().slice(0, 30);
    out.push(`<section class="panel sh-events"><div class="panel-head"><span>EVENTS</span><span class="muted">${esc(s.header && s.header.tagline || "")}</span></div><ul class="events">` +
      ev.map((e) => `<li><b>${esc(when(e.ts))}</b> ${esc(e.agent.toUpperCase())}: ${esc(e.text)}</li>`).join("") + `</ul>` +
      alertsHtml(sh, earlier) + `</section>`);
    out.push(`<section class="panel sh-notes"><ul>${(sh.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` +
      logHtml(sh, false, earlier) + `</section>`);
    return out.join("");
  }

  // ---- the page
  const store = {
    get() { try { return sessionStorage.getItem("shadow-key") || localStorage.getItem("shadow-key") || ""; } catch { return ""; } },
    set(v, keep) { try { sessionStorage.setItem("shadow-key", v); if (keep) localStorage.setItem("shadow-key", v); } catch { /* private mode */ } },
    forget() { try { sessionStorage.removeItem("shadow-key"); localStorage.removeItem("shadow-key"); } catch { /* private mode */ } },
  };

  async function fetchJson(path) {
    const res = await fetch(`${path}?t=${Date.now()}`, { cache: "no-store" });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(res.status);
    return res.json();
  }

  async function load(password) {
    const $ = (id) => document.getElementById(id);
    const now = Date.now();
    let st = null, env = null;
    let unread = false;
    try { st = await fetchJson("boards/BTC-SHADOW.status.json"); } catch (e) { unread = true; $("status").textContent = `couldn't read the status (${e.message})`; }
    try { env = await fetchJson("boards/BTC-SHADOW.enc.json"); } catch { env = null; }
    if (!unread) $("status").innerHTML = statusHtml(st, now, !!env);
    if (!env) { $("lock").hidden = true; $("board").hidden = true; $("foot").textContent = "no SHADOW board published yet"; return; }
    if (!password) { $("lock").hidden = false; $("board").hidden = true; return; }
    try {
      const board = await decrypt(env, password);
      $("lock").hidden = true; $("err").textContent = "";
      $("board").innerHTML = boardHtml(board, now, st); $("board").hidden = false;
      $("foot").textContent = `SHADOW board from ${when(board.updated)} · status ${st ? when(st.updated) : "—"}`;
    } catch {
      store.forget();
      $("lock").hidden = false; $("board").hidden = true;
      $("err").textContent = "That key doesn't open this board.";
    }
  }

  if (typeof document !== "undefined") {
    const $ = (id) => document.getElementById(id);
    $("unlock").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = $("pw").value; $("pw").value = "";
      store.set(v, $("remember").checked);
      load(v);
    });
    $("forget").addEventListener("click", (e) => { e.preventDefault(); store.forget(); $("board").innerHTML = ""; load(""); });
    load(store.get());
    setInterval(() => load(store.get()), 5 * 60 * 1000);
  }
  if (typeof module !== "undefined") module.exports = { decrypt, statusHtml, boardHtml, BOARD_AAD, when, day, clock, plain, coded, noted };
})();
