"""Unit tests for security helpers (pure functions).

Part of next phase test modularization.
"""

import pytest

from app import security


@pytest.mark.unit
def test_new_slug_length():
    s = security.new_slug(14)
    assert len(s) == 14
    assert all(c in security._BASE62 for c in s)  # wait, _BASE62 is private, but for test ok


def test_new_slug_default():
    s = security.new_slug()
    assert len(s) == 14


def test_new_pin_format():
    p = security.new_pin()
    assert len(p) == 4
    assert p.isdigit()


def test_sign_unsign_roundtrip():
    val = "test-value-123"
    tok = security.sign(val)
    assert security.unsign(tok) == val


def test_unsign_bad():
    assert security.unsign("garbage") is None
