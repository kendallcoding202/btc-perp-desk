"""The published site, stamped with the commit it was built from, and the page that updates itself onto a new one.

The phone kept running the old page for 8+ hours after a release: nothing told it a new version was out, and its asset
URLs carried no version, so even a reload could keep cached copies (Pages sends max-age=600). Now the workflow's
"Stamp the site with its build", right after the Build (tools/stamp_site.py), writes version.json, puts the build in
each page's head and ?v=<short sha> on the site's own .js and .css (Google Fonts untouched): the same bytes every run on
one commit, again and again, and a failure leaves every file whole and can't stop the deploy. dashboard/version.js
reads version.json when the page comes back to the foreground and every 10 minutes, reloads once onto a new build and
never in a loop (at most once per 15 minutes: the CDN's old page), never on a 404, garbage or no network, never while
the SHADOW key is being typed, and says so once in the footer, beside the build. Its functions are run in node."""

from __future__ import annotations

import html.parser
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import stamp_site

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
WF = ROOT / ".github" / "workflows" / "paper.yml"
VERSION = (DASH / "version.js").read_text()
SHA = "e94d763" + "a1" * 16 + "b"
SHA2 = "0123456789abcdef" * 2 + "01234567"
STAMP = "Stamp the site with its build"

node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _site(where: Path) -> Path:
    """dashboard/* in where/_site, as the Build copies it, with a board beside."""
    site = where / "_site"
    (site / "boards").mkdir(parents=True)
    for p in DASH.iterdir():
        shutil.copy(p, site / p.name)
    (site / "boards" / "BTC.json").write_text('{"updated": 1}')
    return site


def _files(site: Path) -> dict:
    return {str(p.relative_to(site)): p.read_bytes() for p in sorted(site.rglob("*")) if p.is_file()}


class _Loads(html.parser.HTMLParser):
    """Every file a page loads: a script's src, a link's href (read apart from the stamp's own pattern)."""

    def __init__(self, text):
        super().__init__()
        self.urls, self.metas = [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("src"):
            self.urls.append(a["src"])
        elif tag == "link" and a.get("href"):
            self.urls.append(a["href"])
        elif tag == "meta" and a.get("name") == "desk-build":
            self.metas.append(a.get("content"))


OWN = {"index.html": ["style.css", "activity.css", "theme-comic.css", "app.js", "spider.js", "activity.js", "edition.js",
                      "version.js"],
       "shadow.html": ["style.css", "theme-comic.css", "shadow.js", "edition.js", "version.js"]}


# ---------------------------------------------------------------- the stamp

def test_every_local_asset_carries_the_build_and_nothing_else_changes(tmp_path):
    site = _site(tmp_path)
    before = _files(site)
    assert stamp_site.stamp_site(site, SHA) == "stamped build e94d763: index.html, shadow.html, version.json"
    after = _files(site)
    assert json.loads(after.pop("version.json")) == {"build": SHA}
    assert set(after) == set(before)                                   # no file left over beside them
    for name, data in before.items():
        if not name.endswith(".html"):
            assert after[name] == data, name                           # the scripts, styles and boards: as they were
    for name, own in OWN.items():
        old, new = _Loads(before[name].decode()), _Loads(after[name].decode())
        local = [u for u in old.urls if not u.startswith("https://")]
        assert local == own and all((site / u).is_file() for u in local)
        assert new.urls == [u + "?v=e94d763" if u in own else u for u in old.urls]
        assert [u for u in new.urls if u.startswith("https://")] == [u for u in old.urls if u.startswith("https://")]
        assert any("fonts.googleapis.com" in u for u in new.urls)
        assert new.metas == [SHA] and old.metas == []
        text = after[name].decode()
        assert text.index('<meta name="desk-build"') < text.index("</head>")
        # and nothing else: take the stamp out and it's the page as it was, byte for byte
        bare = text.replace("?v=e94d763", "").replace(f'<meta name="desk-build" content="{SHA}">\n', "")
        assert bare.encode() == before[name]


@pytest.mark.parametrize("url, out", [
    ("app.js", "app.js?v=abc1234"),
    ("theme-comic.css", "theme-comic.css?v=abc1234"),
    ("app.js?v=e94d763", "app.js?v=abc1234"),
    ("lib/app.js?x=1&v=e94d763#top", "lib/app.js?x=1&v=abc1234#top"),
    ("https://fonts.googleapis.com/css2?family=Bangers&display=swap", None),
    ("https://cdn.example/app.js", None),
    ("//cdn.example/app.js", None),
    ("/app.js", None),
    ("data:text/css,a{}", None),
    ("shadow.html", None),
    ("./", None),
    ("#forget", None),
])
def test_only_the_sites_own_js_and_css_are_versioned(url, out):
    assert stamp_site.versioned(url, "abc1234") == (out or url)


def test_stamping_again_changes_nothing_and_another_commit_replaces_the_stamp(tmp_path):
    a, b = _site(tmp_path / "a"), _site(tmp_path / "b")
    (b / "boards" / "BTC.json").write_text('{"updated": 2}')            # another run on the same commit: new boards
    stamp_site.stamp_site(a, SHA)
    once = _files(a)
    stamp_site.stamp_site(a, SHA)
    assert _files(a) == once
    stamp_site.stamp_site(b, SHA)
    assert {k: v for k, v in _files(b).items() if not k.startswith("boards")} == \
        {k: v for k, v in once.items() if not k.startswith("boards")}   # the same build: nothing for a page to reload on
    stamp_site.stamp_site(a, SHA2)
    fresh = _site(tmp_path / "c")
    stamp_site.stamp_site(fresh, SHA2)
    assert _files(a) == _files(fresh)
    assert "e94d763" not in (a / "index.html").read_text()


def test_a_stamp_that_fails_leaves_every_file_whole(tmp_path, monkeypatch, capsys):
    site = _site(tmp_path)
    assert stamp_site.main([]) == 2
    # a page with no head: refused before anything is written
    (site / "zz.html").write_text("<html><body>no head</body></html>")
    before = _files(site)
    assert stamp_site.main([str(site), SHA]) == 1
    assert _files(site) == before and "no </head>" in capsys.readouterr().err
    (site / "zz.html").unlink()
    before = _files(site)
    # not a commit (an unset GITHUB_SHA, say)
    for bad in ("", "  ", "e94d76", "g" * 40, "e94d763 rm -rf", SHA * 2):
        assert stamp_site.main([str(site), bad]) == 1 and _files(site) == before, bad
    assert stamp_site.main([str(tmp_path / "nowhere"), SHA]) == 1
    stamped = _site(tmp_path / "ok")
    stamp_site.stamp_site(stamped, SHA)
    want = _files(stamped)
    # the disk fills while shadow.html is written: index.html is wholly stamped, shadow.html as it was, no version.json
    # (a page without it just doesn't update itself) and no half-written file left in the site
    real_write, real_replace = Path.write_bytes, Path.replace

    def full(self, data):
        if self.name == "shadow.html.tmp":
            real_write(self, data[:len(data) // 2])
            raise OSError(28, "No space left on device")
        return real_write(self, data)
    monkeypatch.setattr(Path, "write_bytes", full)
    assert stamp_site.main([str(site), SHA]) == 1
    now = _files(site)
    assert set(now) == set(before)
    assert now["index.html"] == want["index.html"] and now["shadow.html"] == before["shadow.html"]
    # the rename of version.json fails: every page stamped, version.json not there, nothing left over
    monkeypatch.setattr(Path, "write_bytes", real_write)

    def no_rename(self, target):
        if self.name == "version.json.tmp":
            raise OSError(5, "Input/output error")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", no_rename)
    assert stamp_site.main([str(site), SHA]) == 1
    now = _files(site)
    assert set(now) == set(before)
    assert now["index.html"] == want["index.html"] and now["shadow.html"] == want["shadow.html"]
    monkeypatch.setattr(Path, "replace", real_replace)
    assert stamp_site.main([str(site), SHA]) == 0 and _files(site) == want


# ---------------------------------------------------------------- the workflow step

def _yaml(text=None):
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(WF.read_text() if text is None else text)


def test_the_stamp_step_comes_right_after_the_build_and_cant_stop_the_deploy():
    steps = _yaml()["jobs"]["step"]["steps"]
    names = [s.get("name") or s.get("uses") for s in steps]
    i = names.index("Build the boards")
    assert names[i + 1:i + 3] == [STAMP, "actions/upload-pages-artifact@v3"]
    build, stamp, upload = steps[i:i + 3]
    deploy = next(s for s in steps if s.get("id") == "deploy")
    assert stamp["if"] == build["if"] == upload["if"] == deploy["if"] == "${{ !cancelled() && steps.load.outcome == 'success' }}"
    assert "steps.stamp" not in str(steps) and "id" not in stamp      # nothing waits on the stamp
    assert stamp["continue-on-error"] is True and stamp["timeout-minutes"] <= 2
    assert "env" not in stamp and "secrets." not in str(stamp) and "git " not in stamp["run"]
    assert stamp["run"].startswith('timeout 60 python3 -m tools.stamp_site _site "$GITHUB_SHA" \\\n  || echo "::warning::')
    assert upload["with"] == {"path": "_site"}
    assert not [p for p in DASH.iterdir() if p.is_dir() or p.suffix == ".py"]   # the helper is never copied into the site


def test_the_only_workflow_change_is_the_stamp_step():
    old = subprocess.run(["git", "show", "e94d763:.github/workflows/paper.yml"], cwd=ROOT, capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip("e94d763 isn't in this checkout")
    new, was = _yaml(), _yaml(old.stdout)
    assert [s for s in new["jobs"]["step"]["steps"] if s.get("name") != STAMP] == was["jobs"]["step"]["steps"]
    assert new["jobs"]["shadow"] == was["jobs"]["shadow"]
    assert {k: v for k, v in new.items() if k != "jobs"} == {k: v for k, v in was.items() if k != "jobs"}
    assert {k: v for k, v in new["jobs"]["step"].items() if k != "steps"} == \
        {k: v for k, v in was["jobs"]["step"].items() if k != "steps"}


@pytest.mark.parametrize("case", ["stamped", "no helper", "a page it can't stamp", "no commit"])
def test_the_stamp_step_stamps_the_site_or_warns_and_the_deploy_goes_on(tmp_path, case):
    ws = tmp_path / "ws"
    site = _site(ws)
    shutil.copytree(ROOT / "tools", ws / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    if case == "no helper":
        (ws / "tools" / "stamp_site.py").unlink()
    if case == "a page it can't stamp":
        (site / "zz.html").write_text("<html>no head</html>")
    before = _files(site)
    run = next(s for s in _yaml()["jobs"]["step"]["steps"] if s.get("name") == STAMP)["run"]
    env = {k: v for k, v in os.environ.items() if k not in ("JEV_API_KEY", "NTFY_TOPIC", "NTFY_TOKEN")}
    env["GITHUB_SHA"] = "" if case == "no commit" else SHA
    res = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run], cwd=ws, env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr                                  # GitHub runs a step as bash -eo pipefail
    if case == "stamped":
        assert res.stdout.strip() == "stamped build e94d763: index.html, shadow.html, version.json"
        assert json.loads((site / "version.json").read_text()) == {"build": SHA}
        assert f'<meta name="desk-build" content="{SHA}">' in (site / "shadow.html").read_text()
    else:
        assert "::warning::the site goes up unstamped this run" in res.stdout
        assert _files(site) == before                                     # the site as the Build left it


# ---------------------------------------------------------------- the page

def _node(js: str):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout.strip()
    return json.loads(out)


WORLD = """
const m = require(%s);
const A = "a".repeat(40), B = "b".repeat(40), MIN = 60000;
const ok = (build) => ({ status: 200, body: JSON.stringify({ build }) });
// a page's world: what version.json answers, the clock, the tab's sessionStorage, a field being typed in
function world(served, at = 1792100000000) {
  const w = { served, now: at, rec: null, reloads: [], asked: [], typing: false, stuck: false };
  w.io = {
    fetch: async (url, opt) => {
      w.asked.push([url, opt]);
      const s = w.served;
      if (s === "offline") throw new TypeError("Failed to fetch");
      return { ok: s.status >= 200 && s.status < 300, status: s.status, json: async () => JSON.parse(s.body) };
    },
    now: () => w.now, typing: () => w.typing, reload: () => w.reloads.push(w.now),
    read: () => w.rec && JSON.parse(w.rec),
    write: (r) => { if (w.stuck) return false; w.rec = JSON.stringify(r); return true; },
  };
  return w;
}
""" % json.dumps(str(DASH / "version.js"))


@node
def test_a_new_build_reloads_the_page_once_and_it_says_so_once():
    js = WORLD + """
(async () => {
  const out = {};
  const w = world(ok(A));
  const check = m.updater(A, w.io);
  out.same = await check();
  out.asked = w.asked[0];
  w.served = ok(B); w.now += 10 * MIN;
  out.reload = await check();
  out.rec = JSON.parse(w.rec);
  // the reload: the page now runs B, in the same tab (the same sessionStorage)
  out.told = m.told(B, w.io) === out.rec.at;
  out.told_again = m.told(B, w.io);
  const after = m.updater(B, w.io);
  out.after = [await after(), await after()];
  w.now += 60 * MIN; out.later = await after();
  out.reloads = w.reloads.length;
  out.old_page_told = m.told(A, world(ok(B)).io);     // never reloaded: nothing to say
  console.log(JSON.stringify(out));
})();"""
    out = _node(js)
    assert out["same"] == "same" and out["reload"] == "reload" and out["reloads"] == 1
    assert out["asked"] == ["version.json?t=1792100000000", {"cache": "no-store"}]
    assert out["rec"] == {"from": "a" * 40, "to": "b" * 40, "at": 1792100000000 + 600000}
    assert out["told"] is True and out["told_again"] == 0 and out["old_page_told"] == 0
    assert out["after"] == ["same", "same"] and out["later"] == "same"


@node
def test_the_cdns_old_page_reloads_at_most_once_per_15_minutes():
    """version.json says B while the CDN still serves page A: a reload gets A again. Checked every minute for two hours
    (the foreground's events, the 10 min timer), it reloads at most once per 15 minutes; a phone clock set back an hour
    never holds the next reload longer, and never makes it loop."""
    js = WORLD + """
(async () => {
  const w = world(ok(B)), t0 = w.now, seen = [];
  for (let k = 0; k <= 120; k++) { w.now = t0 + k * MIN; seen.push(await m.updater(A, w.io)()); }   // each reload: page A again
  const gaps = w.reloads.slice(1).map((t, i) => (t - w.reloads[i]) / MIN);
  const back = world(ok(B)); const c = m.updater(A, back.io), r = [];
  r.push(await c()); back.now -= 60 * MIN; r.push(await c()); back.now += MIN; r.push(await c()); back.now += 14 * MIN; r.push(await c());
  console.log(JSON.stringify({ n: w.reloads.length, gaps, waiting: seen.filter((s) => s === "waiting").length, back: r }));
})();"""
    out = _node(js)
    assert out["n"] == 9 and set(out["gaps"]) == {15} and out["waiting"] == 121 - 9
    assert out["back"] == ["reload", "reload", "waiting", "reload"]


@node
def test_a_missing_garbled_or_empty_version_or_no_network_changes_nothing():
    js = WORLD + """
(async () => {
  const cases = [{ status: 404, body: "nf" }, { status: 500, body: "" }, "offline", { status: 200, body: "" },
    { status: 200, body: "<!doctype html><title>404</title>" }, { status: 200, body: "{" }, { status: 200, body: "null" },
    { status: 200, body: "[]" }, { status: 200, body: "{}" }, ok(""), ok("   "), ok(7), ok(null), ok("<b>new</b>"),
    ok("B".repeat(40)), ok("b".repeat(40) + "\\n"), ok("abc")];
  const out = [];
  for (const s of cases) { const w = world(s); out.push([await m.updater(A, w.io)(), w.reloads.length, w.rec]); }
  console.log(JSON.stringify(out));
})();"""
    for res, reloads, rec in _node(js):
        assert res == "unread" and reloads == 0 and rec is None


@node
def test_no_reload_while_a_field_is_being_filled_in_or_where_the_tab_cant_remember_it():
    js = WORLD + """
(async () => {
  const out = {};
  const w = world(ok(B)), check = m.updater(A, w.io);
  w.typing = true; out.typing = [await check(), w.reloads.length, w.rec];
  w.typing = false; out.sent = await check();
  const s = world(ok(B)); s.stuck = true;
  out.stuck = [await m.updater(A, s.io)(), s.reloads.length];
  // the foreground's events together (visible, focus): one request
  const t = world(ok(A)), c = m.updater(A, t.io);
  out.together = await Promise.all([c(), c(), c()]); out.requests = t.asked.length;
  // what counts as filling in: the SHADOW key's field focused or typed in, the remember box focused
  const doc = (active, values) => ({ activeElement: active, querySelectorAll: () => values.map((value) => ({ value })) });
  const input = { matches: (q) => q === "input, textarea, select" }, body = { matches: () => false };
  out.fields = [m.typing(doc(input, [""])), m.typing(doc(body, ["s3cret-not-sent"])), m.typing(doc(body, [""])),
    m.typing(doc(null, [])), m.typing(doc(body, ["", ""]))];
  console.log(JSON.stringify(out));
})();"""
    out = _node(js)
    assert out["typing"] == ["typing", 0, None] and out["sent"] == "reload"
    assert out["stuck"] == ["unrecorded", 0]
    assert sorted(out["together"]) == ["asking", "asking", "same"] and out["requests"] == 1
    assert out["fields"] == [True, True, False, False, False]
    fields = re.search(r'const FIELDS = "([^"]+)";', VERSION).group(1).split(", ")
    assert "input[type=password]" in fields and "textarea" in fields
    assert not [f for f in fields if re.search(r"checkbox|radio|submit|button|hidden", f)]



@node
def test_a_request_that_never_answers_holds_the_next_for_20_seconds_at_most():
    """A page frozen mid-request (the phone locked as it asked; back from the back-forward cache) may never hear back:
    the next foreground waits on it 20 s at most, and its late answer doesn't clear the newer request's mark."""
    js = WORLD + """
(async () => {
  const w = world(ok(B)), hung = [];
  const real = w.io.fetch;
  w.io.fetch = (url, opt) => w.hang ? new Promise((resolve, reject) => hung.push(reject)) : real(url, opt);
  const check = m.updater(A, w.io), out = [];
  w.hang = true; const first = check();
  w.now += 19000; out.push(await check());
  w.now += 2000; const second = check();                    // 21 s on: another request
  out.push(await check());                                 // that one is out now: one at a time again
  hung[0](new TypeError("frozen")); out.push(await first); // the old one fails late
  out.push(await check());                                 // the second's still out: its mark held
  w.hang = false; hung[1](new TypeError("frozen")); out.push(await second);
  out.push(await check(), w.reloads.length);
  console.log(JSON.stringify(out));
})();"""
    assert _node(js) == ["asking", "asking", "unread", "asking", "unread", "reload", 1]

@node
def test_the_page_wires_itself_only_with_a_build_and_checks_only_while_it_shows():
    """version.js as the page runs it, in node with a stand-in document: no meta (the local server, a fixture), nothing
    at all; with one, the footer's build, the foreground's events and the 10 minute timer, never while hidden."""
    js = """
const src = require("fs").readFileSync(%s, "utf8");
const A = "a".repeat(40), B = "b".repeat(40), store = {};
async function page(meta, served, hidden = false) {
  const p = { on: {}, reloads: 0, asked: 0, el: { hidden: true, textContent: "", title: "" } };
  const document = { hidden, activeElement: null, querySelectorAll: () => [],
    querySelector: (q) => q === 'meta[name="desk-build"]' && meta != null ? { content: meta } : null,
    getElementById: (id) => id === "build" ? p.el : null, addEventListener: (e, f) => { p.on["document " + e] = f; } };
  const g = { document, module: undefined, location: { reload: () => { p.reloads++; } },
    addEventListener: (e, f) => { p.on[e] = f; }, setInterval: (f, ms) => { p.on.every = ms; p.tick = f; },
    sessionStorage: { getItem: (k) => k in store ? store[k] : null, setItem: (k, v) => { store[k] = String(v); } },
    fetch: async () => { p.asked++; return { ok: true, status: 200, json: async () => ({ build: served }) }; } };
  new Function(...Object.keys(g), src)(...Object.values(g));
  p.doc = document;
  p.fire = async (name) => { (name === "tick" ? p.tick : p.on[name])(); await new Promise((r) => setTimeout(r, 5)); };
  return p;
}
(async () => {
  const out = {};
  for (const meta of [null, "", "not-a-build", "<b>"]) {
    const p = await page(meta, B);
    out[String(meta)] = { on: Object.keys(p.on), hidden: p.el.hidden, text: p.el.textContent, asked: p.asked };
  }
  const a = await page(A, B, true);
  out.events = Object.keys(a.on).sort(); out.every = a.on.every;
  out.foot = [a.el.hidden, a.el.textContent, a.el.title];
  await a.fire("tick"); await a.fire("document visibilitychange"); await a.fire("focus"); await a.fire("pageshow");
  out.hidden = [a.asked, a.reloads];
  a.doc.hidden = false; await a.fire("document visibilitychange");
  out.shown = [a.asked, a.reloads];
  const b = await page(B, B);
  out.after = b.el.textContent;
  await b.fire("pageshow"); await b.fire("tick");
  out.quiet = [b.asked, b.reloads];
  out.again = (await page(B, B)).el.textContent;
  out.store = Object.keys(store);
  console.log(JSON.stringify(out));
})();""" % json.dumps(str(DASH / "version.js"))
    out = _node(js)
    for meta in ("null", "", "not-a-build", "<b>"):
        assert out[meta] == {"on": [], "hidden": True, "text": "", "asked": 0}, meta
    assert out["events"] == ["document visibilitychange", "every", "focus", "pageshow"] and out["every"] == 600000
    assert out["foot"] == [False, "build\u00a0aaaaaaa",
                           "the code this page runs: commit " + "a" * 40 + ". a new version reloads the page by itself"]
    assert out["hidden"] == [0, 0]                                     # hidden: the timer and the events ask nothing
    assert out["shown"] == [1, 1]
    assert re.fullmatch(r"build\u00a0bbbbbbb · this page updated itself to the newest version at "
                        r"\d{1,2}:\d\d [ap]m MT", out["after"]), out["after"]
    assert out["quiet"] == [2, 0] and out["again"] == "build\u00a0bbbbbbb"
    assert out["store"] == ["desk-build"]


@node
def test_the_footer_says_the_build_and_the_update_in_mountain_time():
    js = """const m = require(%s);
console.log(JSON.stringify([m.footText("e94d763" + "0".repeat(33), 0),
  m.footText("e94d763" + "0".repeat(33), Date.UTC(2026, 9, 16, 15, 12)),
  m.footText("e94d763" + "0".repeat(33), Date.UTC(2026, 11, 1, 16, 5))]));""" % json.dumps(str(DASH / "version.js"))
    plain, summer, winter = _node(js)
    assert plain == "build\u00a0e94d763"
    assert summer == "build\u00a0e94d763 · this page updated itself to the newest version at 9:12 am MT"   # MDT
    assert winter == "build\u00a0e94d763 · this page updated itself to the newest version at 9:05 am MT"   # MST


def test_both_pages_load_it_and_keep_everything_else_they_store():
    for name in ("index.html", "shadow.html"):
        page = (DASH / name).read_text()
        assert page.index('<script src="edition.js"></script>') < page.index('<script src="version.js"></script>')
        foot = page[page.index('<footer class="foot">'):page.index("</footer>")]
        assert '<span class="build" id="build" hidden></span>' in foot
        assert "desk-build" not in page                                # unstamped here: the local server never reloads
    assert 'const KEY = "desk-build", EVERY = 10 * 60 * 1000, ONCE_PER = 15 * 60 * 1000, ASKING = 20 * 1000;' in VERSION
    assert "location.reload()" in VERSION and 'cache: "no-store"' in VERSION
    assert not re.search(r"removeItem|\.clear\(|localStorage|location\.(href|hash|search)\s*=|replace\(", VERSION)
    assert re.findall(r"sessionStorage\.(\w+)\((\w+)", VERSION) == [("getItem", "KEY"), ("setItem", "KEY"),
                                                                     ("getItem", "KEY")]   # its own record only
    for p in DASH.iterdir():                                           # the SHADOW key, the edition, the tab: untouched
        assert p.name == "version.js" or "desk-build" not in p.read_text(), p.name
    assert re.search(r"\.foot \.build \{ display: block; \}", (DASH / "style.css").read_text())   # its own line
