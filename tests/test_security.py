"""Tests for password hashing and token helpers."""

from __future__ import annotations

from app.config import get_settings
from app.security import (
    hash_password,
    hash_session_token,
    new_invite_code,
    new_session_token,
    new_verify_token,
    verify_password,
)


def test_password_roundtrip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_password_is_salted():
    """The same password must not produce the same hash twice."""
    assert hash_password("same") != hash_password("same")


def test_hash_format_is_self_describing():
    """The work factor is embedded, so raising it later does not orphan hashes."""
    from app.config import Settings

    stored = hash_password("x")
    scheme, n, r, p, salt, key = stored.split("$")
    assert scheme == "scrypt"
    assert int(r) == 8
    assert int(p) == 1
    assert len(salt) == 64  # 32 bytes hex
    assert len(key) == 128  # 64 bytes hex

    # The suite runs at a lowered factor for speed, so assert the parameters
    # recorded in the hash match whatever was configured rather than a literal.
    assert int(n) == get_settings().scrypt_n

    # And pin the production default separately, since nothing else would catch
    # a change that quietly weakens it. Read the field default rather than
    # constructing Settings(), which would pick up the suite's override.
    assert Settings.model_fields["scrypt_n"].default == 2**14


def test_verify_survives_malformed_stored_value():
    """A corrupted row reads as a failed login, not a crash."""
    for bad in ["", "garbage", "scrypt$1$2$3", "bcrypt$a$b$c$d$e", "scrypt$x$y$z$aa$bb"]:
        assert verify_password("anything", bad) is False


def test_verify_rejects_empty_password_against_real_hash():
    stored = hash_password("real")
    assert not verify_password("", stored)


def test_session_tokens_are_unique_and_hashed_consistently():
    tokens = {new_session_token() for _ in range(50)}
    assert len(tokens) == 50
    token = next(iter(tokens))
    assert hash_session_token(token) == hash_session_token(token)
    assert hash_session_token(token) != token
    assert len(hash_session_token(token)) == 64


def test_invite_codes_avoid_lookalikes():
    codes = {new_invite_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert len(code) == 8
        assert not set(code) & set("O0I1")


def test_verify_tokens_have_prefix_and_short_body():
    token = new_verify_token()
    assert token.startswith("ACW-")
    body = token[4:]
    assert len(body) == 4
    # Fits inside Codeforces' 16-character organization field with room to spare.
    assert len(token) <= 16
    assert not set(body) & set("O0I1")


def test_temporary_passwords_avoid_punctuation():
    """Administrators read these out loud, so no base64url punctuation."""
    from app.security import new_temporary_password

    for _ in range(100):
        password = new_temporary_password()
        assert len(password) == 16
        assert password.isalnum()
        assert not set(password) & set("0O1Il")
