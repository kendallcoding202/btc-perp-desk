"""The lock on Coinbase LIVE can't be gone around in code: only desk/coinbase_live.py makes an Armed token, and only
it hands one to CoinbaseREST; the transport check in coinbase_rest.py is the only other place that names it. And no
tracked file holds key material."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESK = ROOT / "desk"


def _sources():
    return sorted(p for p in DESK.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_coinbase_live_makes_an_armed_token_or_passes_one_to_the_client():
    allowed = {DESK / "coinbase_live.py", DESK / "brokers" / "coinbase_rest.py"}
    for path in _sources():
        text = path.read_text()
        tree = ast.parse(text)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        makes = [n for n in calls if (isinstance(n.func, ast.Name) and n.func.id == "Armed")
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == "Armed")]
        passes = [n for n in calls for k in n.keywords if k.arg == "armed"]
        if path in allowed:
            continue
        assert not makes and "Armed(" not in text, f"{path.relative_to(ROOT)} makes an Armed token"
        assert not passes and "armed=" not in text, f"{path.relative_to(ROOT)} hands a client an Armed token"
        assert "_SEAL" not in text, f"{path.relative_to(ROOT)} reaches for the lock's seal"
    rest = (DESK / "brokers" / "coinbase_rest.py").read_text()
    assert "Armed(" not in rest                     # the transport only checks a token, never makes one


def test_the_client_has_no_way_to_delete_or_put_and_posts_only_order_paths():
    from desk.brokers import coinbase_rest as cr

    assert not hasattr(cr.CoinbaseREST, "delete") and not hasattr(cr.CoinbaseREST, "put")
    assert cr.ORDER_PATHS == {"/api/v3/brokerage/orders", "/api/v3/brokerage/orders/batch_cancel",
                              "/api/v3/brokerage/orders/close_position"}
    for p in cr.READ_ALLOWLIST:
        assert not re.search(r"preview|edit|sweeps/schedule|move_funds|withdraw|deposit|send", p.pattern)


PEM_BLOCK = re.compile(r"-----BEGIN (?:EC |ENCRYPTED |OPENSSH |RSA )?PRIVATE KEY-----\s*\\?n?\s*[A-Za-z0-9+/]{40,}")
CDP_NAME = re.compile(r"organizations/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/apiKeys/")


def test_no_tracked_file_holds_key_material():
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")
    others = subprocess.run(["git", "ls-files", "-z", "--others", "--exclude-standard"], cwd=ROOT,
                            capture_output=True, check=True).stdout.split(b"\0")
    for name in filter(None, set(files + others)):
        path = ROOT / name.decode()
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        assert not PEM_BLOCK.search(text), f"{name.decode()} holds a private key block"
        for m in CDP_NAME.finditer(text):
            assert m.group(1) == "00000000-0000-0000-0000-000000000000", \
                f"{name.decode()} names a CDP key under a real-looking organization"
