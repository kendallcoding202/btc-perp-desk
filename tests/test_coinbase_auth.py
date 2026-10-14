"""Coinbase CDP keys and request signing (desk/brokers/coinbase_auth.py). Every key here is generated fresh by the test
and named .../apiKeys/test-...-not-real under an all-zero organization: none is, or could be, a real Coinbase key."""

from __future__ import annotations

import base64
import copy
import json
import pickle
import re

import jwt
import pytest

from desk.brokers import coinbase_auth as ca
from fake_coinbase import make_ec_key, make_ed_key


def _decode(tok, fk):
    return jwt.get_unverified_header(tok), jwt.decode(tok, fk.public, algorithms=[fk.alg],
                                                     options={"verify_exp": False, "verify_nbf": False})


@pytest.mark.parametrize("make,alg", [(lambda: make_ec_key(), "ES256"),
                                      (lambda: make_ed_key(form="pkcs8"), "EdDSA"),
                                      (lambda: make_ed_key(form="raw32"), "EdDSA"),
                                      (lambda: make_ed_key(form="raw64"), "EdDSA")])
def test_the_key_file_loads_for_each_key_type_coinbase_issues(make, alg):
    fk = make()
    k = ca.load_cdp_key(fk.file_json())
    assert (k.name, k.alg) == (fk.name, alg)
    tok = ca.build_jwt(k, "GET", "/api/v3/brokerage/key_permissions", 1_791_900_000.5)
    head, claims = _decode(tok, fk)
    assert head["alg"] == alg


def test_id_stands_in_for_name_and_extra_fields_are_ignored():
    fk = make_ec_key()
    raw = json.dumps({"id": fk.name, "privateKey": fk.private_text, "nickname": "x", "permissions": ["view"]})
    assert ca.load_cdp_key(raw).name == fk.name


def test_a_literal_backslash_n_and_a_base64_wrapped_file_load_too():
    fk = make_ec_key()
    escaped = json.dumps({"name": fk.name, "privateKey": fk.private_text.replace("\n", "\\n")})  # double-escaped
    assert "\\\\n" in escaped
    assert ca.load_cdp_key(escaped).alg == "ES256"
    wrapped = base64.b64encode(fk.file_json().encode()).decode()
    assert ca.load_cdp_key(wrapped).name == fk.name
    # wrapped over several lines, as a terminal might paste it
    assert ca.load_cdp_key("\n".join(wrapped[i:i + 60] for i in range(0, len(wrapped), 60))).name == fk.name


def test_a_64_byte_ed25519_key_whose_halves_dont_match_is_refused():
    a, b = make_ed_key(form="raw64"), make_ed_key(form="raw64")
    seed_a = base64.b64decode(a.private_text)[:32]
    pub_b = base64.b64decode(b.private_text)[32:]
    bad = json.dumps({"name": a.name, "privateKey": base64.b64encode(seed_a + pub_b).decode()})
    with pytest.raises(ca.CdpKeyError, match="second half isn't the first half's public key"):
        ca.load_cdp_key(bad)


@pytest.mark.parametrize("raw", ["", "   ", "not json at all", "[1, 2]", '{"name": "x"}', '{"privateKey": "y"}',
                                 '{"name": "", "privateKey": "abc"}', '{"name": "n", "privateKey": "!!!notbase64!!!"}',
                                 '{"name": "n", "privateKey": "' + base64.b64encode(b"x" * 20).decode() + '"}',
                                 '{"name": "n", "privateKey": "-----BEGIN EC PRIVATE KEY-----\\nAAAA\\n-----END EC '
                                 'PRIVATE KEY-----"}'])
def test_anything_else_is_refused_without_echoing_it(raw):
    with pytest.raises(ca.CdpKeyError) as e:
        ca.load_cdp_key(raw)
    text = str(e.value)
    for piece in ("not json at all", "!!!notbase64!!!", "AAAA", base64.b64encode(b"x" * 20).decode()):
        assert piece not in text


def test_the_jwt_carries_exactly_the_sdks_claims():
    """coinbase-advanced-py 1.8.4 build_jwt / rest_base.set_headers: header kid + a 64-hex nonce (+ PyJWT's typ),
    claims sub, iss "cdp", nbf, exp = nbf + 120, uri = "METHOD api.coinbase.com/path" without the query; no aud."""
    fk = make_ec_key()
    k = ca.load_cdp_key(fk.file_json())
    tok = ca.build_jwt(k, "get", "/api/v3/brokerage/orders/historical/batch?order_status=OPEN", 1_791_900_123.9)
    head, claims = _decode(tok, fk)
    assert head["kid"] == fk.name and head["typ"] == "JWT" and head["alg"] == "ES256"
    assert re.fullmatch(r"[0-9a-f]{64}", head["nonce"])
    assert claims == {"sub": fk.name, "iss": "cdp", "nbf": 1_791_900_123, "exp": 1_791_900_243,
                      "uri": "GET api.coinbase.com/api/v3/brokerage/orders/historical/batch"}
    tok2 = ca.build_jwt(k, "POST", "/api/v3/brokerage/orders", 1_791_900_123.9)
    assert jwt.get_unverified_header(tok2)["nonce"] != head["nonce"]       # a fresh nonce every token
    assert _decode(tok2, fk)[1]["uri"] == "POST api.coinbase.com/api/v3/brokerage/orders"


def test_key_material_never_shows_in_repr_str_errors_or_copies():
    fk = make_ec_key()
    k = ca.load_cdp_key(fk.file_json())
    body_lines = [ln for ln in fk.private_text.splitlines() if not ln.startswith("-----")]
    for text in (repr(k), str(k), f"{k}", f"{k!r}"):
        assert fk.name not in text and all(ln not in text for ln in body_lines)
        assert text == f"CdpKey(ES256 …{fk.name[-4:]})"
    for copier in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError, match="can't be copied or pickled"):
            copier(k)


def test_add_mask_lines_only_on_actions_one_line_each():
    fk = make_ec_key()
    raw = json.dumps({"name": fk.name, "privateKey": fk.private_text.replace("\n", "\\n")})
    k = ca.load_cdp_key(raw)
    out: list[str] = []
    ca.mask_for_actions(k, emit=out.append, env={})
    assert out == []                                                # not on Actions: nothing printed at all
    ca.mask_for_actions(k, emit=out.append, env={"GITHUB_ACTIONS": "true"})
    assert out and all(ln.startswith("::add-mask::") and "\n" not in ln and "\r" not in ln for ln in out)
    masked = {ln[len("::add-mask::"):] for ln in out}
    assert fk.name in masked and "test-key-not-real" in masked
    for ln in fk.private_text.splitlines():
        if not ln.startswith("-----"):
            assert ln in masked                                     # each base64 line of the PEM
    assert json.dumps(k._masks)                                     # (plain strings)
    # the escaped one-line form as it sits in the JSON (\n literal) is masked whole, so a print of the raw secret's
    # privateKey value can't show it
    assert fk.private_text.replace("\n", "\\n").strip() in masked or \
        fk.private_text.replace("\n", "\\n") in {m for m in masked}
    tok_out: list[str] = []
    ca.mask_lines("aaa.bbb.ccc", emit=tok_out.append, env={"GITHUB_ACTIONS": "true"})
    assert tok_out == ["::add-mask::aaa.bbb.ccc"]


def test_a_base64_wrapped_secret_is_masked_line_by_line_too():
    fk = make_ec_key()
    wrapped = base64.b64encode(fk.file_json().encode()).decode()
    pasted = "\n".join(wrapped[i:i + 60] for i in range(0, len(wrapped), 60))
    k = ca.load_cdp_key(pasted)
    out: list[str] = []
    ca.mask_for_actions(k, emit=out.append, env={"GITHUB_ACTIONS": "true"})
    masked = {ln[len("::add-mask::"):] for ln in out}
    assert all(piece in masked for piece in pasted.splitlines())
