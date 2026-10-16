"""The published site, stamped with the commit it was built from, and the page that updates itself onto a new one.

The phone kept running the old page for 8+ hours after a release: nothing told it a new version was out, and its asset
URLs carried no version, so even a reload could keep cached copies (Pages sends max-age=600). Now the workflow's
"Stamp the site with its build", right after the Build (tools/stamp_site.py), writes version.json, puts the build in
each page's head and ?v=<short sha> on the site's own .js and .css (Google Fonts untouched): the same bytes every run on
one commit, again and again, and a failure leaves every file whole and can't stop the deploy. dashboard/version.js
reads version.json when the page comes back to the foreground and every 10 minutes, reloads once onto a new build and
never in a loop (at most once per 15 minutes: the CDN's old page; by its tab's record, and by the ?b= it reloads to
when that record is lost), never on a 404, garbage or no network, never while the SHADOW key is being typed, back where
it was scrolled to, and says so once in the footer, beside the build. Its functions are run in node."""

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
        # at the start of the head, right after the charset: before every script, wherever one is loaded
        assert f'<head>\n<meta charset="utf-8">\n<meta name="desk-build" content="{SHA}">\n<meta name="viewport"' in text
        assert text.index('<meta name="desk-build"') < text.index("<script")
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


@pytest.mark.parametrize("page, want", [
    ('<html><head>\n<meta charset="utf-8">\n<title>t</title></head>', '<html><head>\n<meta charset="utf-8">\n{M}\n<title>t</title></head>'),
    ('<html><HEAD lang="en"><title>t</title><script src="a.js"></script></head>',
     '<html><HEAD lang="en">{M}\n<title>t</title><script src="a.js?v=e94d763"></script></head>'),
    ('<html><head><meta charset=utf-8><script src="a.js"></script></head>',
     '<html><head><meta charset=utf-8>{M}\n<script src="a.js?v=e94d763"></script></head>'),
    # an earlier deploy's stamp at the end of the head: moved to the start
    ('<html><head>\n<meta charset="utf-8">\n<title>t</title>\n<meta name="desk-build" content="0123456">\n</head>',
     '<html><head>\n<meta charset="utf-8">\n{M}\n<title>t</title>\n</head>'),
    ("<html><header>no head</header></html>", None),
])
def test_the_build_goes_at_the_start_of_the_head(page, want):
    """Right after the head's start tag and its charset, so every script finds it wherever it is loaded (a script in
    the head too); again, the same; no head, refused."""
    if want is None:
        with pytest.raises(ValueError, match="no <head>"):
            stamp_site.stamp_page(page, SHA)
        return
    out = stamp_site.stamp_page(page, SHA)
    assert out == want.replace("{M}", f'<meta name="desk-build" content="{SHA}">')
    assert stamp_site.stamp_page(out, SHA) == out


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
    assert _files(site) == before and "no <head>" in capsys.readouterr().err
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
    run = stamp["run"].splitlines()
    assert run[0] == 'timeout -k 5 60 python3 -m tools.stamp_site _site "$GITHUB_SHA" || {' and run[-1] == "}"
    assert 60 + 5 < stamp["timeout-minutes"] * 60                      # killed with time left for the clean-up
    assert run[1:-1] == ["  rm -f _site/*.tmp _site/version.json || true",            # nothing a failed stamp left
                         '  echo "::warning::the site goes up without version.json this run (why: above), so an open page '
                         "won't update itself to it\""]
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


# the helper stopped by the step's timeout part way (a stalled disk, say): it hangs renaming a file into place, before
# the rename (shadow.html: its .tmp written) or after it (version.json: in, the run never done); or it won't stop when
# told to (SIGTERM ignored) and is killed 5 s on. The step's 60 s and 5 s are 2 s and 1 s here, for the test only
STALL = """
import pathlib, signal, time
if %r:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
_replace = pathlib.Path.replace
def replace(self, target):
    if pathlib.Path(target).name == %r:
        if %r:
            _replace(self, target)
        time.sleep(600)
    return _replace(self, target)
pathlib.Path.replace = replace
"""


@pytest.mark.parametrize("case", ["stamped", "no helper", "a page it can't stamp", "no commit", "stopped part way",
                                  "stopped after version.json", "stopped, it won't stop: killed"])
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
    if case.startswith("stopped"):
        assert run.count("timeout -k 5 60 ") == 1
        run = run.replace("timeout -k 5 60 ", "timeout -k 1 2 ")
        (ws / "hook").mkdir()
        hang = ((case.endswith("killed"),) + (("version.json", True) if case == "stopped after version.json" else ("shadow.html", False)))
        (ws / "hook" / "sitecustomize.py").write_text(STALL % hang)
        env["PYTHONPATH"] = "hook"
    res = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run], cwd=ws, env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr                                  # GitHub runs a step as bash -eo pipefail
    now = _files(site)
    if case == "stamped":
        assert res.stdout.strip() == "stamped build e94d763: index.html, shadow.html, version.json"
        assert json.loads((site / "version.json").read_text()) == {"build": SHA}
        assert f'<meta name="desk-build" content="{SHA}">' in (site / "shadow.html").read_text()
        return
    assert res.stdout.strip() == ("::warning::the site goes up without version.json this run (why: above), so an open "
                                  "page won't update itself to it")
    assert set(now) == set(before)                                         # no version.json, no temp copy goes up
    stamped = _site(tmp_path / "ok")
    stamp_site.stamp_site(stamped, SHA)
    want = _files(stamped)
    if case in ("stopped part way", "stopped, it won't stop: killed"):     # each page whole: stamped or as built
        assert now["index.html"] == want["index.html"] and now["shadow.html"] == before["shadow.html"]
    elif case == "stopped after version.json":
        assert now["index.html"] == want["index.html"] and now["shadow.html"] == want["shadow.html"]
    else:
        assert now == before                                               # the site as the Build left it
    assert {k: v for k, v in now.items() if not k.endswith(".html")} == {k: v for k, v in before.items() if not k.endswith(".html")}


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
    now: () => w.now, typing: () => w.typing, scroll: () => w.y || 0, reload: (b) => { w.reloads.push(w.now); w.to = b; },
    read: () => w.rec && JSON.parse(w.rec),
    write: (r) => { if (w.stuck) return false; w.rec = JSON.stringify(r); return true; },
  };
  return w;
}
""" % json.dumps(str(DASH / "version.js"))


@node
def test_a_new_build_reloads_the_page_once_onto_its_own_address():
    js = WORLD + """
(async () => {
  const out = {};
  const w = world(ok(A));
  const check = m.updater(A, w.io);
  out.same = await check();
  out.asked = w.asked[0];
  w.served = ok(B); w.now += 10 * MIN; w.y = 1234;
  out.reload = await check();
  out.rec = JSON.parse(w.rec); out.to = w.to;
  // the reload: the page now runs B, in the same tab (the same sessionStorage), loaded at ?b=bbbbbbb
  const after = m.updater(B, w.io);
  out.after = [await after(), await after()];
  w.now += 60 * MIN; out.later = await after();
  out.reloads = w.reloads.length;
  console.log(JSON.stringify(out));
})();"""
    out = _node(js)
    assert out["same"] == "same" and out["reload"] == "reload" and out["reloads"] == 1
    assert out["asked"] == ["version.json?t=1792100000000", {"cache": "no-store"}]
    assert out["rec"] == {"from": "a" * 40, "to": "b" * 40, "at": 1792100000000 + 600000, "y": 1234}   # and where it was
    assert out["to"] == "b" * 40
    assert out["after"] == ["same", "same"] and out["later"] == "same"


@node
def test_only_its_own_reload_is_read_from_the_address():
    """?b= and the short sha of the build a reload went for: whether this load brought it. Anything else (no ?b=, not
    7 lower-case hex digits) is a load like any other."""
    js = """const m = require(%s), A = "a".repeat(40), B = "b".repeat(40);
console.log(JSON.stringify([m.landed(B, "?b=bbbbbbb"), m.landed(A, "?b=bbbbbbb"), m.landed(B, "?state=x&b=bbbbbbb"),
  ...["", "?state=boards/BTC.json", "?b=", "?b=bbbbbb", "?b=bbbbbbbb", "?b=BBBBBBB", "?b=bbbbbbg", "?b=<i>"].map((s) => m.landed(B, s))]));""" \
        % json.dumps(str(DASH / "version.js"))
    assert _node(js) == [{"b": "bbbbbbb", "got": True}, {"b": "bbbbbbb", "got": False}, {"b": "bbbbbbb", "got": True}] + [None] * 8


@node
def test_the_cdns_old_page_reloads_at_most_once_per_15_minutes():
    """version.json says B while the CDN still serves page A: a reload gets A again. Checked every minute for two hours
    (the foreground's events, the 10 min timer), it reloads at most once per 15 minutes. A phone clock set back never
    makes that sooner (it waits 15 min from when it's seen) nor holds it more than 15 min longer, and never loops."""
    js = WORLD + """
(async () => {
  const w = world(ok(B)), t0 = w.now, seen = [];
  for (let k = 0; k <= 120; k++) { w.now = t0 + k * MIN; seen.push(await m.updater(A, w.io)()); }   // each reload: page A again
  const gaps = (at) => at.slice(1).map((t, i) => (t - at[i]) / MIN);
  // the clock set back 30 min at minute 60, and an hour at minute 140: real minutes between reloads
  const lag = world(ok(B)), real = [];
  for (let k = 0; k <= 240; k++) {
    lag.now = t0 + k * MIN - (k >= 60 ? 30 * MIN : 0) - (k >= 140 ? 60 * MIN : 0);
    if ((await m.updater(A, lag.io)()) === "reload") real.push(k * MIN);
  }
  const back = world(ok(B)); const c = m.updater(A, back.io), r = [];
  r.push(await c()); back.now -= 60 * MIN; r.push(await c()); back.now += MIN; r.push(await c()); back.now += 14 * MIN; r.push(await c());
  console.log(JSON.stringify({ n: w.reloads.length, gaps: gaps(w.reloads), waiting: seen.filter((s) => s === "waiting").length,
    lag: gaps(real), back: r }));
})();"""
    out = _node(js)
    assert out["n"] == 9 and set(out["gaps"]) == {15} and out["waiting"] == 121 - 9
    # set back at 60 (15 min after the last reload) and at 140 (5 after): 15 min from then
    assert out["lag"] == [15, 15, 15, 30, 15, 15, 15, 15, 20, 15, 15, 15, 15, 15]
    assert out["back"] == ["reload", "waiting", "waiting", "reload"]


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
  // what counts as filling in: the SHADOW key's field focused or typed in, the remember box focused; on screen only
  // (its form hidden once the key is sent: some browsers leave the field focused)
  const rects = (shown) => () => shown ? [{ width: 200, height: 30 }] : [];
  const field = (value, shown = true) => ({ value, getClientRects: rects(shown) });
  const doc = (active, fields) => ({ activeElement: active, querySelectorAll: () => fields });
  const input = (shown = true) => ({ matches: (q) => q === "input, textarea, select", getClientRects: rects(shown) });
  const body = { matches: () => false, getClientRects: rects(true) };
  out.fields = [m.typing(doc(input(), [field("")])), m.typing(doc(body, [field("s3cret-not-sent")])),
    m.typing(doc(body, [field("")])), m.typing(doc(null, [])), m.typing(doc(body, [field(""), field("")])),
    m.typing(doc(input(false), [field("", false)])), m.typing(doc(body, [field("left-in-a-hidden-form", false)]))];
  console.log(JSON.stringify(out));
})();"""
    out = _node(js)
    assert out["typing"] == ["typing", 0, None] and out["sent"] == "reload"
    assert out["stuck"] == ["unrecorded", 0]
    assert sorted(out["together"]) == ["asking", "asking", "same"] and out["requests"] == 1
    assert out["fields"] == [True, True, False, False, False, False, False]
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
def test_a_page_its_own_reload_brought_without_the_build_waits_15_minutes_whatever_the_tab_says():
    """held: the page loaded by its own reload (?b=) and still the old build. Its 15 min are its own, not the tab's
    record: with that record lost, it still waits them, and a clock set back waits them from when that's seen."""
    js = WORLD + """
(async () => {
  const w = world(ok(B)), t0 = w.now, c = m.updater(A, w.io, t0), r = [];
  for (const dt of [0, MIN, 14 * MIN, 15 * MIN]) { w.now = t0 + dt; r.push(await c()); }
  const k = world(ok(B)), c2 = m.updater(A, k.io, k.now), r2 = [];
  k.now -= 60 * MIN; r2.push(await c2()); k.now += 14 * MIN; r2.push(await c2()); k.now += MIN; r2.push(await c2());
  const n = world(ok(B));
  console.log(JSON.stringify({ r, rec: w.rec === null, r2, plain: await m.updater(A, n.io, 0)() }));
})();"""
    out = _node(js)
    assert out["r"] == ["waiting", "waiting", "waiting", "reload"] and out["r2"] == ["waiting", "waiting", "reload"]
    assert out["plain"] == "reload"


# version.js as a page runs it, in node with a stand-in document and window: page(meta, what version.json says, o),
# o its address (path, search, hash), hidden, how far down the browser already has it (y) and how tall it is (height);
# every page in one tab (one sessionStorage: store), on one clock (NOW)
PAGE = """
const src = require("fs").readFileSync(%s, "utf8");
const A = "a".repeat(40), B = "b".repeat(40), MIN = 60000;
let store = {}, NOW = 1792100000000, cur = null;
Date.now = () => NOW;
Object.defineProperty(globalThis, "scrollY", { get: () => cur.y });
globalThis.innerHeight = 800;
async function page(meta, served, o = {}) {
  const p = cur = { on: {}, reloads: [], asked: 0, el: { hidden: true, textContent: "", title: "" }, scrolls: [], timers: [],
    ro: null, y: o.y || 0, height: o.height || 900, address: null };
  const at = new URL((o.path || "/btc-perp-desk/") + (o.search || "") + (o.hash || ""), "https://kendallcoding202.github.io");
  const location = { pathname: at.pathname, search: at.search, hash: at.hash, replace: (u) => { p.reloads.push(u); } };
  const history = { state: "app", replaceState: (s, _, u) => {
    const x = new URL(u, at); p.address = [s, x.pathname + x.search + x.hash]; location.search = x.search; location.hash = x.hash; } };
  const document = { hidden: !!o.hidden, activeElement: null, querySelectorAll: () => [], body: {},
    documentElement: { get scrollHeight() { return p.height; } },
    querySelector: (q) => q === 'meta[name="desk-build"]' && meta != null ? { content: meta } : null,
    getElementById: (id) => id === "build" ? p.el : null, addEventListener: (e, f) => { p.on["document " + e] = f; } };
  const g = { document, location, history, module: undefined,
    addEventListener: (e, f) => { p.on[e] = f; }, removeEventListener: (e, f) => { if (p.on[e] === f) delete p.on[e]; },
    setInterval: (f, ms) => { p.on.every = ms; p.tick = f; },
    setTimeout: (f, ms) => p.timers.push({ f, ms }), clearTimeout: (k) => { if (p.timers[k - 1]) p.timers[k - 1].f = null; },
    scrollTo: (x, y) => { p.scrolls.push(y); p.y = y; },
    ResizeObserver: class { constructor(f) { p.ro = { f, on: false }; } observe() { p.ro.on = true; p.resize(p.height); } disconnect() { p.ro.on = false; } },
    sessionStorage: { getItem: (k) => k in store ? store[k] : null, setItem: (k, v) => { store[k] = String(v); } },
    fetch: async () => { p.asked++; return { ok: true, status: 200, json: async () => ({ build: served }) }; } };
  p.resize = (h) => { p.height = h; if (p.ro && p.ro.on) p.ro.f(); };
  new Function(...Object.keys(g), src)(...Object.values(g));
  p.doc = document;
  p.fire = async (name) => { (name === "tick" ? p.tick : p.on[name])(); await new Promise((r) => setTimeout(r, 5)); };
  return p;
}
// the page a reload goes to (the address it was given), the same build or another
const next = (p, meta, served) => { const u = new URL(p.reloads[0], "https://h.io"); return page(meta, served, { path: u.pathname, search: u.search, hash: u.hash }); };
""" % json.dumps(str(DASH / "version.js"))


@node
def test_the_page_wires_itself_only_with_a_build_and_checks_only_while_it_shows():
    """No meta (the local server, a fixture), nothing at all; with one, the footer's build, the foreground's events and
    the 10 minute timer, never while hidden. A new build: one reload, to the same address with ?b=, and that load
    (only that one) says it updated itself, with ?b= off its address."""
    js = PAGE + """
(async () => {
  const out = {};
  for (const meta of [null, "", "not-a-build", "<b>"]) {
    const p = await page(meta, B, { search: "?b=bbbbbbb" });
    out[String(meta)] = { on: Object.keys(p.on), hidden: p.el.hidden, text: p.el.textContent, asked: p.asked, address: p.address };
  }
  const a = await page(A, B, { hidden: true });
  out.events = Object.keys(a.on).sort(); out.every = a.on.every;
  out.foot = [a.el.hidden, a.el.textContent, a.el.title, a.address];
  await a.fire("tick"); await a.fire("document visibilitychange"); await a.fire("focus"); await a.fire("pageshow");
  out.hidden = [a.asked, a.reloads.slice()];
  a.doc.hidden = false; await a.fire("document visibilitychange");
  out.shown = [a.asked, a.reloads.slice()];
  const b = await next(a, B, B);
  out.after = [b.el.textContent, b.address];
  await b.fire("pageshow"); await b.fire("tick");
  out.quiet = [b.asked, b.reloads.length];
  out.again = (await page(B, B)).el.textContent;
  out.store = Object.keys(store);
  console.log(JSON.stringify(out));
})();"""
    out = _node(js)
    for meta in ("null", "", "not-a-build", "<b>"):
        assert out[meta] == {"on": [], "hidden": True, "text": "", "asked": 0, "address": None}, meta
    assert out["events"] == ["document visibilitychange", "every", "focus", "pageshow"] and out["every"] == 600000
    assert out["foot"] == [False, "build\u00a0aaaaaaa",
                           "the code this page runs: commit " + "a" * 40 + ". a new version reloads the page by itself", None]
    assert out["hidden"] == [0, []]                                    # hidden: the timer and the events ask nothing
    assert out["shown"] == [1, ["/btc-perp-desk/?b=bbbbbbb"]]
    assert re.fullmatch(r"build\u00a0bbbbbbb · this page updated itself to the newest version at "
                        r"\d{1,2}:\d\d [ap]m MT", out["after"][0]), out["after"]
    assert out["after"][1] == ["app", "/btc-perp-desk/"]                # ?b= off, the page's history state kept
    assert out["quiet"] == [2, 0] and out["again"] == "build\u00a0bbbbbbb"
    assert out["store"] == ["desk-build /btc-perp-desk/"]


@node
def test_each_page_keeps_its_own_record_and_only_the_page_that_reloaded_says_so():
    """index.html and shadow.html in one tab: each reloads at most once per 15 minutes on its own record, so one's
    reload never holds the other on old code; and only the load a page's own reload brings says it updated itself.
    The reload keeps the address's #hash (the tab) and its other parameters; ?b= comes off as it loads."""
    js = PAGE + """
(async () => {
  const out = {}, IX = "/btc-perp-desk/", SH = "/btc-perp-desk/shadow.html";
  const i1 = await page(A, B, { path: IX, hash: "#ETH" }); await i1.fire("pageshow");   // the index, B out: it reloads
  const i2 = await next(i1, A, B); await i2.fire("pageshow");                          // and the CDN answers with its old page
  const i3 = await page(A, B, { path: "/btc-perp-desk/index.html" }); await i3.fire("pageshow");   // by hand, by name
  out.index = [i1.reloads, i2.el.textContent, i2.reloads.length, i2.address, i3.reloads.length];
  out.byHand = (await page(B, B, { path: SH })).el.textContent;                        // the SHADOW link: the new page
  const s1 = await page(A, B, { path: SH }); await s1.fire("pageshow");                // or its old one: its own reload, now
  out.shadow = [s1.reloads, (await next(s1, B, B)).el.textContent];
  out.store = Object.keys(store).sort();
  const q = await page(A, B, { search: "?state=boards/ETH.json&b=aaaaaaa", hash: "#ETH" });
  out.params = [q.address];
  await q.fire("pageshow"); out.params.push(q.reloads);
  console.log(JSON.stringify(out));
})();"""
    out = _node(js)
    assert out["index"] == [["/btc-perp-desk/?b=bbbbbbb#ETH"], "build\u00a0aaaaaaa", 0, ["app", "/btc-perp-desk/#ETH"], 0]
    assert out["byHand"] == "build\u00a0bbbbbbb"
    assert out["shadow"][0] == ["/btc-perp-desk/shadow.html?b=bbbbbbb"]
    assert re.fullmatch(r"build\u00a0bbbbbbb · this page updated itself to the newest version at \d{1,2}:\d\d [ap]m MT",
                        out["shadow"][1]), out["shadow"]
    assert out["store"] == ["desk-build /btc-perp-desk/", "desk-build /btc-perp-desk/shadow.html"]
    assert out["params"] == [["app", "/btc-perp-desk/?state=boards%2FETH.json#ETH"], []]   # (held: the index's record)


@node
@pytest.mark.parametrize("tab", ["kept", "lost at every load", "lost, and no ?b= (the guard taken out)"])
def test_no_loop_when_the_cdn_keeps_the_old_page_even_with_the_tabs_storage_lost(tab):
    """version.json says B while the CDN serves page A for two hours, checked every minute. The tab's record kept: one
    reload per 15 minutes. The record lost on every load (a browser that clears it, or drops the write): still one
    per 15 minutes, by the ?b= each reload carries, every one to the same address. Without ?b= (the page as before
    this guard) the same tab reloads at every check: the loop this guard is for."""
    js = PAGE + """
(async () => {
  const tab = %s, t0 = NOW, loads = [], addresses = new Set();
  let p = await page(A, B, { hash: "#SOL" });
  for (let k = 0; k <= 120; k++) {
    NOW = t0 + k * MIN;
    await p.fire("tick");
    if (!p.reloads.length) continue;
    loads.push(k); addresses.add(p.reloads[0]);
    if (tab !== "kept") store = {};
    p = tab.startsWith("lost, and no") ? await page(A, B, { hash: "#SOL" }) : await next(p, A, B);
    if (p.address) addresses.add(p.address[1]);
  }
  console.log(JSON.stringify({ loads, addresses: [...addresses] }));
})();""" % json.dumps(tab)
    out = _node(js)
    if tab.startswith("lost, and no"):
        assert len(out["loads"]) == 121
        return
    assert out["loads"] == list(range(0, 121, 15))
    assert out["addresses"] == ["/btc-perp-desk/?b=bbbbbbb#SOL", "/btc-perp-desk/#SOL"]


@node
def test_the_reload_lands_where_the_owner_was():
    """The scroll position goes in the reload's record; the page it brings scrolls back there once it's tall enough
    (its first render), and back again as it lays itself out, for 10 s, until the owner touches it. Not where the
    browser has already scrolled the page, not for a load by hand, nor a record of another reload."""
    js = PAGE + """
(async () => {
  const out = {};
  const a = await page(A, B, { y: 1500, height: 4000 }); await a.fire("pageshow");     // 1500 px down, B out
  out.rec = JSON.parse(store["desk-build /btc-perp-desk/"]).y;
  const b = await next(a, B, B);                       // it starts short (connecting...) and grows as the board is drawn
  out.short = b.scrolls.slice();
  b.resize(1800); out.notYet = b.scrolls.slice();      // 1800 - 800 < 1500
  b.resize(4000); out.there = [b.scrolls.slice(), b.y];
  b.y = 1530; b.resize(4100); out.back = [b.scrolls.slice(), b.y];   // laid out some more: put back
  b.on.touchstart();                                   // the owner's finger: never again
  b.y = 900; b.resize(4200);
  out.touched = [b.scrolls.slice(), b.y, Object.keys(b.on).filter((e) => /touch|wheel|key|mouse/.test(e)), b.ro.on];
  out.keep = b.timers.map((x) => x.ms);
  const c = await page(B, B, { search: "?b=bbbbbbb" }); c.timers[0].f(); c.resize(4000);   // 10 s on and still short
  out.late = [c.scrolls, c.ro.on];
  out.browser = (await page(B, B, { search: "?b=bbbbbbb", y: 300, height: 4000 })).scrolls;   // the browser's own
  out.byHand = (await page(B, B, { height: 4000 })).scrolls;
  out.old = (await page(A, B, { search: "?b=bbbbbbb", height: 4000 })).scrolls;              // the CDN's old page too
  store["desk-build /btc-perp-desk/"] = JSON.stringify({ from: A, to: "c".repeat(40), at: NOW, y: 700 });
  out.other = (await page(B, B, { search: "?b=bbbbbbb", height: 4000 })).scrolls;
  console.log(JSON.stringify(out));
})();"""
    out = _node(js)
    assert out["rec"] == 1500 and out["short"] == [] and out["notYet"] == []
    assert out["there"] == [[1500], 1500] and out["back"] == [[1500, 1500], 1500]
    assert out["touched"] == [[1500, 1500], 900, [], False] and out["keep"] == [10000]
    assert out["late"] == [[], False]
    assert out["browser"] == [] and out["byHand"] == [] and out["old"] == [1500] and out["other"] == []


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
    assert ('const KEY = "desk-build", EVERY = 10 * 60 * 1000, ONCE_PER = 15 * 60 * 1000, ASKING = 20 * 1000, '
            'KEEP = 10 * 1000;') in VERSION
    # the reload: the same path and #hash, ?b= added (location.replace: no extra step in the back button's history)
    assert "location.replace(`${location.pathname}?${q}${location.hash}`)" in VERSION and 'cache: "no-store"' in VERSION
    assert "location.reload" not in VERSION and VERSION.count("location.replace(") == 1
    assert VERSION.count("history.replaceState(history.state, ") == 1
    assert not re.search(r"removeItem|\.clear\(|localStorage|location\.(href|hash|search|pathname)\s*=|location\.assign", VERSION)
    assert re.findall(r"sessionStorage\.(\w+)\((\w+)", VERSION) == [("getItem", "slot"), ("setItem", "slot"),
                                                                     ("getItem", "slot")]   # its own record only
    assert "const slot = `${KEY} ${location.pathname" in VERSION
    for p in DASH.iterdir():                                           # the SHADOW key, the edition, the tab: untouched
        assert p.name == "version.js" or "desk-build" not in p.read_text(), p.name
    css = (DASH / "style.css").read_text()
    assert ".foot .build { display: block; }" in css and ".foot .build[hidden] { display: none; }" in css   # its own line
