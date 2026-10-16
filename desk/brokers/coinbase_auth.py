"""Coinbase CDP API keys: reading the key file, signing each request, keeping the key out of every log.

The owner downloads one key file from the Coinbase Developer Platform and stores it whole as ONE GitHub secret,
COINBASE_CDP_KEY. This module reads it the way Coinbase's own SDK does (coinbase-advanced-py 1.8.4, rest/__init__.py
and jwt_generator.py): the key's id is "name" (or "id"), its secret is "privateKey", and a literal backslash-n in the
secret is a newline (bytes(s, "utf8").decode("unicode_escape")). The same JSON, base64-encoded, is accepted too:
GitHub's docs advise against structured data (JSON) as a secret value, since redaction matches lines exactly.

    privateKey   a SEC1 PEM ("-----BEGIN EC PRIVATE KEY-----", P-256)    -> ES256   (the type the owner is told to make)
                 a PKCS8 PEM (EC P-256 -> ES256, Ed25519 -> EdDSA)
                 raw base64 Ed25519: 32 bytes (the seed) or 64 (seed + public key; the halves must match) -> EdDSA

Each REST call carries a fresh JWT (build_jwt), with the SDK's claims: sub = the key name, iss "cdp", nbf = now,
exp = now + 120, uri = "METHOD api.coinbase.com/path" (no scheme, no query string), no aud; header kid = the key name
and a 64-hex nonce (PyJWT adds typ JWT). Third-party ccxt signs Coinbase JWTs with older claims (aud
retail_rest_api_proxy, iss coinbase-cloud): not the model here.

Nothing here ever prints, logs or raises with any part of the key: a CdpKey shows only its algorithm and the last four
characters of its name, can't be pickled, and every error is a fixed text. On GitHub Actions, mask_for_actions() emits
"::add-mask::" for the key name, its id, each line of the private key in every form it may appear in, and the raw
secret's lines (GitHub's hardening guide asks for derived values to be masked; it masks one line per command, so every
value is split on newlines first). The REST client masks each JWT the same way before it uses it.

PyJWT and cryptography are imported lazily: backtest and paper never need them (requirements-coinbase.txt).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
from dataclasses import dataclass, field

API_HOST = "api.coinbase.com"
JWT_LIFETIME_S = 120
ARMOR = re.compile(r"-----(BEGIN|END) [A-Z0-9 ]+-----")     # a PEM block's first and last lines: not secret

BAD_KEY = ('COINBASE_CDP_KEY isn\'t a Coinbase API key file: expected JSON with "name" (or "id") and "privateKey" '
           '(its contents are not shown)')


class CdpKeyError(ValueError):
    """The key secret can't be used. Its text never includes any of the secret."""


def _crypto():
    try:
        import jwt  # noqa: F401
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    except ImportError:
        raise CdpKeyError("the Coinbase signing libraries aren't installed: pip install --require-hashes -r "
                          "requirements-coinbase.txt") from None
    return serialization, ec, ed25519


@dataclass(frozen=True, repr=False, eq=False)
class CdpKey:
    """A loaded CDP key. Only its algorithm and the last four characters of its name are ever shown."""
    name: str
    alg: str                                    # "ES256" or "EdDSA"
    _private: object = field(compare=False)     # a cryptography private key object
    _masks: tuple = field(default=(), compare=False)

    def __repr__(self) -> str:
        return f"CdpKey({self.alg} …{self.name[-4:]})"

    __str__ = __repr__

    def __reduce__(self):       # pickle, copy, deepcopy: never a second copy of the key anywhere
        raise TypeError("a CdpKey can't be copied or pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("a CdpKey can't be copied or pickled")

    def __getstate__(self):
        raise TypeError("a CdpKey can't be copied or pickled")

    @property
    def tail(self) -> str:
        return self.name[-4:]


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode("".join(text.split()), validate=True).decode("utf-8").strip()
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise CdpKeyError(BAD_KEY) from None
    try:
        d = json.loads(text)
    except ValueError:
        raise CdpKeyError(BAD_KEY) from None
    if not isinstance(d, dict):
        raise CdpKeyError(BAD_KEY)
    return d


def _private_key(secret: str):
    serialization, ec, ed25519 = _crypto()
    if secret.lstrip().startswith("-----BEGIN"):
        try:
            k = serialization.load_pem_private_key(secret.strip().encode("utf-8"), password=None)
        except Exception:
            raise CdpKeyError(f"{BAD_KEY}: privateKey is a PEM block that doesn't load") from None
        if isinstance(k, ec.EllipticCurvePrivateKey):
            if not isinstance(k.curve, ec.SECP256R1):
                raise CdpKeyError("COINBASE_CDP_KEY's privateKey is an EC key on another curve, not P-256 (ECDSA keys "
                                  "from Coinbase are P-256)")
            return k, "ES256"
        if isinstance(k, ed25519.Ed25519PrivateKey):
            return k, "EdDSA"
        raise CdpKeyError("COINBASE_CDP_KEY's privateKey is neither an ECDSA (P-256) nor an Ed25519 key")
    try:
        raw = base64.b64decode("".join(secret.split()), validate=True)
    except (binascii.Error, ValueError):
        raise CdpKeyError(f"{BAD_KEY}: privateKey is neither PEM nor base64") from None
    if len(raw) not in (32, 64):
        raise CdpKeyError(f"COINBASE_CDP_KEY's privateKey decodes to {len(raw)} bytes; an Ed25519 key is 32 or 64")
    k = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
    if len(raw) == 64:
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if pub != raw[32:]:
            raise CdpKeyError("COINBASE_CDP_KEY's privateKey is 64 bytes whose second half isn't the first half's "
                              "public key: not an Ed25519 key as Coinbase issues it")
    return k, "EdDSA"


def _lines(value: str) -> list[str]:
    """A value as GitHub's add-mask takes it: one line at a time, no line endings, nothing empty."""
    return [ln.strip() for ln in re.split(r"[\r\n]+", value or "") if ln.strip()]


def _masks(name: str, stored: str, secret: str, raw: str, wrapped: bool) -> tuple:
    """Every value that would give the key away if it were printed: the name and its id, the private key as stored
    in the JSON (escaped), as decoded, and line by line (PEM armor lines are public and left out), and the raw secret
    line by line when it was base64-wrapped."""
    out: list[str] = []

    def add(v: str) -> None:
        for ln in _lines(v):
            if ARMOR.fullmatch(ln) or ln in out:
                continue
            out.append(ln)

    add(name)
    m = re.search(r"apiKeys/([^/\s]+)", name)
    if m:
        add(m.group(0))
        add(m.group(1))
    for piece in (stored, secret):
        add(piece)
        for part in piece.split("\\n"):         # the escaped form, as it sits in the JSON text
            add(part)
    add(json.dumps(stored)[1:-1])
    if wrapped:
        add(raw)
    assert all("\n" not in v and "\r" not in v for v in out)
    return tuple(out)


def load_cdp_key(raw: str | None) -> CdpKey:
    """The key in COINBASE_CDP_KEY (the downloaded key file's JSON, or that JSON base64-encoded)."""
    if not (raw or "").strip():
        raise CdpKeyError("COINBASE_CDP_KEY is not set")
    d = _parse_json(raw)
    name = d.get("name") or d.get("id")
    stored = d.get("privateKey")
    if not isinstance(name, str) or not name.strip() or not isinstance(stored, str) or not stored.strip():
        raise CdpKeyError(BAD_KEY)
    try:
        secret = bytes(stored, encoding="utf8").decode("unicode_escape")    # as the SDK: a literal \n is a newline
    except UnicodeDecodeError:
        raise CdpKeyError(BAD_KEY) from None
    key, alg = _private_key(secret)
    wrapped = not raw.strip().startswith("{")
    return CdpKey(name.strip(), alg, key, _masks(name.strip(), stored, secret, raw, wrapped))


def build_jwt(key: CdpKey, method: str, path: str, now_s: float) -> str:
    """One request's token. `path` is the URL path alone (a query string is cut off, as the SDK does); `now_s` is
    Coinbase's clock (the REST client adds the skew it measured)."""
    import jwt

    path = path.split("?", 1)[0]
    if not path.startswith("/"):
        raise ValueError("a Coinbase request path starts with /")
    nbf = int(now_s)
    claims = {"sub": key.name, "iss": "cdp", "nbf": nbf, "exp": nbf + JWT_LIFETIME_S,
              "uri": f"{method.upper()} {API_HOST}{path}"}
    return jwt.encode(claims, key._private, algorithm=key.alg, headers={"kid": key.name, "nonce": secrets.token_hex()})


def on_actions(env=None) -> bool:
    return (env if env is not None else os.environ).get("GITHUB_ACTIONS") == "true"


def mask_lines(value: str, emit=print, env=None) -> None:
    """GitHub Actions: mask `value` in the job's log from here on, line by line. Outside Actions: nothing."""
    if not on_actions(env):
        return
    for ln in _lines(value):
        assert "\n" not in ln and "\r" not in ln
        emit(f"::add-mask::{ln}")


def mask_for_actions(key: CdpKey, emit=print, env=None) -> None:
    """GitHub Actions: mask every form of the key (see _masks). Outside Actions: prints nothing."""
    if not on_actions(env):
        return
    for v in key._masks:
        mask_lines(v, emit=emit, env=env)
