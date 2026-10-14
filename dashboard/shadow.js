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
  const when = (ms) => ms ? new Date(ms).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "—";
  const ago = (ms, now) => ms ? `${Math.max(0, Math.round((now - ms) / 60000))} min ago` : "never";

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
    const plain = await subtle.decrypt({ name: "AES-GCM", iv: b64(env.iv), additionalData: enc.encode(env.aad) }, key, b64(env.ct));
    return JSON.parse(new TextDecoder().decode(plain));
  }

  // ---- the public status: no amounts, no key needed
  function statusHtml(st, now) {
    if (!st) return "No SHADOW status yet: SHADOW starts once the COINBASE_CDP_KEY and DESK_STATE_KEY secrets are set (docs/coinbase-setup.md).";
    const stale = now - st.updated > STALE_MS;
    const state = !st.ok ? `<b class="bad">NOT WORKING (${esc(st.problem || "error")})</b>` + (st.note ? `: ${esc(st.note)}` : "") : stale ? `<b class="bad">NOT UPDATED for ${ago(st.updated, now)}</b>` : `<b class="ok">OK</b>`;
    return `${esc(st.mode)} ${state} · updated ${when(st.updated)} (${ago(st.updated, now)}) · run ${esc(st.runs)}` +
      (st.failed_runs ? ` (${esc(st.failed_runs)} failed)` : "") +
      ` · key ${st.key_view_only ? "View only" : st.key_view_only === false ? "<b class='bad'>NOT view only</b>" : "not checked"}` +
      ` · checks ${esc(st.checks_passed ?? "—")}/${esc(st.checks_total ?? "—")}` +
      (st.decision ? ` · desk: ${esc(st.decision)}` : "") +
      (st.would_send_last_24h != null ? ` · ${esc(st.would_send_last_24h)} order(s) it would have sent in 24h (none sent)` : "") +
      (st.alerts_open ? ` · <b class="bad">${esc(st.alerts_open)} alert(s)</b>` : "") +
      (st.opened ? "" : " · the account isn't open yet (no money in it)") +
      (stale ? ` · GitHub runs this about every 15 minutes, best effort: check the Actions tab` : "");
  }

  // ---- the board, once opened
  function rows(pairs) {
    return pairs.filter(Boolean).map(([k, v, cls]) => `<div class="${cls || ""}"><span>${esc(k)}</span><b>${v}</b></div>`).join("");
  }

  function boardHtml(s, now) {
    const sh = s.shadow || {}, a = s.account || {}, t = s.ticker || {}, st = s.status || {};
    const out = [];
    const stale = s.updated && now - s.updated > STALE_MS;
    if (!st.ok || stale) {
      out.push(`<section class="panel sh-warn"><b>${!st.ok ? "This run didn't finish" : "Not updated for " + ago(s.updated, now)}</b>` +
        (st.problem ? ` (${esc(st.problem)})` : "") + `. The numbers below are from ${when(s.updated)}.` +
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
        ["margin window", esc(a.margin_window || "—")], ["intraday margin", esc(a.intraday_setting || "—")],
        a.usdc ? ["USDC in the portfolio", `<span class="bad">${esc(a.usdc)}</span>`] : null,
        a.balance_block ? ["balance check", `<span class="bad">${esc(a.balance_block)}</span>`] : ["balance check", "adds up"],
      ])}</div></div>` +
      `<div><div class="label muted">SHADOW (EST.)</div><div class="big">${usd(sim)}</div><div class="stats">${rows([
        ["simulated P&L (est.)", signed(sh.simulated_pnl)], ["position", esc(t.position || "—") + (t.position_detail ? " " + esc(t.position_detail) : "")],
        ["record", esc(t.record || "—")], ["fees (est.)", usd(t.fees)], ["funding (est.)", usd(t.funding)],
        sh.opened ? ["opened", `${usd(sh.opened.amount)} on ${esc(sh.opened.date)} (${esc(sh.opened.what)}): money in, not profit`] : ["opened", "not yet: no money in the account"],
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
    out.push(`<section class="panel sh-checks"><div class="panel-head"><span>EVERY READ, THIS RUN</span><span class="muted">a View-only key: it can't trade or move money</span></div><ul>` +
      (sh.checks || []).map((c) => `<li class="${c.ok === true ? "ok" : c.ok === false ? "bad" : "muted"}"><b>${c.ok === true ? "OK" : c.ok === false ? "FAIL" : "—"}</b> ${esc(c.label)}${c.note ? `<i>${esc(c.note)}</i>` : ""}</li>`).join("") + `</ul></section>`);
    // what it would have sent: LIVE's exact requests, none sent
    const ws = (sh.would_send || []).slice().reverse();
    out.push(`<section class="panel sh-sends"><div class="panel-head"><span>ORDERS IT WOULD SEND</span><span class="muted">LIVE's exact requests · NONE SENT · fills simulated</span></div><ul>` +
      (ws.length ? ws.map((r) => `<li>${esc(when(r.ts).slice(0, 10))} ${esc(r.text)}<details><summary>request body</summary><pre>${esc(JSON.stringify(r.body, null, 1))}</pre></details></li>`).join("") : "<li class='muted'>none yet</li>") + `</ul></section>`);
    // the virtual book, and what the real account holds besides
    const book = sh.book || {};
    out.push(`<section class="panel sh-book"><div class="panel-head"><span>SIMULATED BOOK</span><span class="muted">what LIVE would hold on Coinbase now</span></div><div class="stats">${rows([
      ["position", book.position ? `${esc(book.position.side)} ${esc(book.position.contracts)} contract(s) @ ${usd(book.position.entry)} · open P&L ${usd(book.position.upnl)} (est.)` : "flat"],
      ["resting orders", (book.orders || []).length ? book.orders.map((o) => `${esc(o.side)} ${esc(o.n)} stop ${usd(o.stop, 0)} limit ${usd(o.limit, 0)} (${esc(o.trigger || o.status)})`).join("<br>") : "none"],
      ["fees, funding (est.)", `${usd(book.fees)} · ${usd(book.funding)}`],
    ])}</div>` +
      `<div class="panel-head"><span>THE REAL ACCOUNT HOLDS</span><span class="muted">positions and orders the desk didn't place block its entries</span></div><div class="stats">${rows([
        ["positions", (a.positions || []).length ? a.positions.map((p) => `${esc(p.product_id)} ${esc(p.side)} ${esc(p.contracts)}`).join("<br>") : "none"],
        ["open orders", (a.orders || []).length ? a.orders.map((o) => `${esc(o.product_id)} ${esc(o.side)} ${esc(o.type || "")}`).join("<br>") : "none"],
        ["fills (BIP, last 30 days)", (a.fills || []).length ? `${a.fills.length}` : "none"],
        ["pending sweeps", (a.sweeps || []).length ? a.sweeps.map((w) => `${esc(w.status)} ${esc(w.amount || "all")}`).join("<br>") : "none"],
        ["fee tier", a.fee_tier ? `${esc(a.fee_tier.tier || "—")} · taker ${esc(a.fee_tier.taker)} · maker ${esc(a.fee_tier.maker)} (fees on the board stay the estimate)` : "—"],
      ])}</div></section>`);
    // events and alerts, then the honest small print and the run's own log
    const ev = (s.events || []).slice().reverse().slice(0, 30);
    out.push(`<section class="panel sh-events"><div class="panel-head"><span>EVENTS</span><span class="muted">${esc(s.header && s.header.tagline || "")}</span></div><ul class="events">` +
      ev.map((e) => `<li><b>${esc(when(e.ts).slice(5))}</b> ${esc(e.agent.toUpperCase())}: ${esc(e.text)}</li>`).join("") + `</ul>` +
      ((sh.alerts || []).length ? `<div class="panel-head"><span>ALERTS THIS RUN</span></div><ul>${sh.alerts.map((x) => `<li class="bad">${esc(x)}</li>`).join("")}</ul>` : "") +
      `</section>`);
    out.push(`<section class="panel sh-notes"><ul>${(sh.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` +
      `<details><summary>this run's log (private)</summary><pre>${esc((sh.log || []).join("\n"))}</pre></details></section>`);
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
    try { st = await fetchJson("boards/BTC-SHADOW.status.json"); } catch (e) { $("status").textContent = `couldn't read the status (${e.message})`; }
    $("status").innerHTML = statusHtml(st, now);
    try { env = await fetchJson("boards/BTC-SHADOW.enc.json"); } catch { env = null; }
    if (!env) { $("lock").hidden = true; $("board").hidden = true; $("foot").textContent = "no SHADOW board published yet"; return; }
    if (!password) { $("lock").hidden = false; $("board").hidden = true; return; }
    try {
      const board = await decrypt(env, password);
      $("lock").hidden = true; $("err").textContent = "";
      $("board").innerHTML = boardHtml(board, now); $("board").hidden = false;
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
  if (typeof module !== "undefined") module.exports = { decrypt, statusHtml, boardHtml, BOARD_AAD };
})();
