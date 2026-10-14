"""Encrypted files for the real Coinbase account's state and board (SHADOW, and LIVE once it's armed).

The repository and its Pages site are public, so nothing about the real account (balances, deposits, fills, positions)
is ever committed, published or logged in plain text. SHADOW keeps its state and its board on the coinbase-state branch
as these envelopes, and the dashboard opens the board on the owner's device with the same password:

    {"v": 1, "kdf": "pbkdf2-sha256", "iter": 600000, "salt": <16 random bytes, base64>, "iv": <12 random bytes, base64>,
     "aad": "btc-perp-desk:state-shadow:BTC", "zlib": true, "ct": <base64 of AES-256-GCM(zlib(json))>}

The key is PBKDF2-SHA256 (600,000 rounds, a fresh salt each time) of DESK_STATE_KEY, a password of 32 characters or
more held as a GitHub secret. AES-256-GCM authenticates as it decrypts: a wrong password, a changed byte, or a file
meant for something else (the "aad" names what it is: the state and the board can't be swapped) all fail the same way,
and nothing is written over the saved state. The board is sealed without zlib (the browser's WebCrypto opens it as is).

`cryptography` is imported lazily: paper and backtest never need it (requirements-coinbase.txt). is_envelope() is
standard library only, for the workflow's save check (desk/coinbase_state.py).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import zlib

V = 1
KDF = "pbkdf2-sha256"
ITERATIONS = 600_000
MIN_ITERATIONS = 1_000              # what open() accepts (the tests seal with fewer rounds to stay fast)
MAX_ITERATIONS = 5_000_000
MIN_KEY_CHARS = 32
MAX_PLAIN = 64 * 1024 * 1024        # a decompressed bundle bigger than this isn't one of the desk's
STATE_AAD = "btc-perp-desk:state-{mode}:BTC"
BOARD_AAD = "btc-perp-desk:board-{mode}:BTC"
FIELDS = ("v", "kdf", "iter", "salt", "iv", "aad", "ct")


class BoxError(ValueError):
    """An envelope can't be made or opened. Its text never includes the password or what's inside."""


class WrongKey(BoxError):
    """The password doesn't open it (or it was changed, or it's another file's)."""


def state_aad(mode: str = "shadow") -> str:
    return STATE_AAD.format(mode=mode)


def board_aad(mode: str = "shadow") -> str:
    return BOARD_AAD.format(mode=mode)


def check_password(password: str | None) -> str:
    if not password:
        raise BoxError("DESK_STATE_KEY is not set: it encrypts the real account's state and board (this repository "
                       "and its site are public)")
    if len(password) < MIN_KEY_CHARS:
        raise BoxError(f"DESK_STATE_KEY is {len(password)} characters; it must be at least {MIN_KEY_CHARS} (a random "
                       f"password from your password manager)")
    return password


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError:
        raise BoxError("the encryption library isn't installed: pip install --require-hashes -r "
                       "requirements-coinbase.txt") from None
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(
        password.encode("utf-8"))


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s, what: str) -> bytes:
    try:
        return base64.b64decode(str(s), validate=True)
    except (binascii.Error, ValueError):
        raise BoxError(f"the envelope's {what} isn't base64") from None


def seal(data: bytes, password: str, aad: str, *, compress: bool = True, iterations: int | None = None) -> dict:
    """`data` encrypted under `password`, bound to `aad` (what the file is)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    check_password(password)
    it = int(iterations or ITERATIONS)
    salt, iv = os.urandom(16), os.urandom(12)
    body = zlib.compress(data, 9) if compress else data
    ct = AESGCM(_derive(password, salt, it)).encrypt(iv, body, aad.encode("utf-8"))
    return {"v": V, "kdf": KDF, "iter": it, "salt": _b64(salt), "iv": _b64(iv), "aad": aad, "zlib": bool(compress),
            "ct": _b64(ct)}


def is_envelope(d) -> bool:
    """Whether `d` (a parsed JSON value) has an envelope's shape. Standard library only: it doesn't decrypt."""
    if not isinstance(d, dict) or set(d) - set(FIELDS) - {"zlib"} or any(k not in d for k in FIELDS):
        return False
    if d.get("v") != V or d.get("kdf") != KDF or not isinstance(d.get("iter"), int):
        return False
    try:
        return len(base64.b64decode(d["salt"], validate=True)) == 16 and \
            len(base64.b64decode(d["iv"], validate=True)) == 12 and len(base64.b64decode(d["ct"], validate=True)) > 16
    except (binascii.Error, ValueError, TypeError):
        return False


def open_(env: dict, password: str, aad: str) -> bytes:
    """What `env` holds, if `password` opens it and it's the file `aad` says. Raises WrongKey or BoxError."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    check_password(password)
    if not is_envelope(env):
        raise BoxError("this isn't one of the desk's encrypted files")
    if env["aad"] != aad:
        raise BoxError(f"this encrypted file is {env['aad']!r}, not {aad!r}: not opened in its place")
    it = env["iter"]
    if not MIN_ITERATIONS <= it <= MAX_ITERATIONS:
        raise BoxError(f"the envelope asks for {it} key rounds: not a file the desk made")
    salt, iv, ct = _unb64(env["salt"], "salt"), _unb64(env["iv"], "iv"), _unb64(env["ct"], "ct")
    try:
        body = AESGCM(_derive(password, salt, it)).decrypt(iv, ct, aad.encode("utf-8"))
    except InvalidTag:
        raise WrongKey("DESK_STATE_KEY doesn't open it (a different key, or the file was changed)") from None
    if env.get("zlib", True):
        d = zlib.decompressobj()
        out = d.decompress(body, MAX_PLAIN)
        if d.unconsumed_tail:
            raise BoxError("the encrypted file is bigger than the desk ever makes one")
        return out
    return body


def seal_json(obj, password: str, aad: str, *, compress: bool = True, iterations: int | None = None) -> str:
    data = json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    return json.dumps(seal(data, password, aad, compress=compress, iterations=iterations), separators=(",", ":"))


def open_json(text: str, password: str, aad: str):
    try:
        env = json.loads(text)
    except ValueError:
        raise BoxError("the encrypted file isn't JSON") from None
    try:
        return json.loads(open_(env, password, aad))
    except ValueError as exc:
        if isinstance(exc, BoxError):
            raise
        raise BoxError("what the encrypted file holds isn't JSON") from None
