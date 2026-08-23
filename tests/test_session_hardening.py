"""G5 — session/auth hardening.

Covers the three properties that are easy to regress silently: admin tokens are
never at rest in the clear, the admin cookie expires on its own shorter clock,
and PIN compares do not leak position.
"""

import hashlib

import pytest

from app import config, db, security

pytestmark = pytest.mark.integration


class _Req:
    def __init__(self, cookie: str | None = None):
        self.cookies = {security.ADMIN_COOKIE: cookie} if cookie else {}


def test_admin_token_is_never_stored_in_the_clear():
    """The nightly snapshot copies whatever is at rest.

    Before this, every backup in /opt/mise/data/backups carried live 90-day
    admin tokens verbatim — a stolen backup was a stolen admin session, with no
    login and nothing in the logs. G1 then started copying those snapshots to a
    second disk.
    """
    db.run("DELETE FROM admin_sessions")
    tok = security.create_admin_session()
    rows = db.all_("SELECT token_hash FROM admin_sessions")
    assert len(rows) == 1
    stored = rows[0]["token_hash"]
    assert stored != tok
    assert stored == hashlib.sha256(tok.encode("utf-8")).hexdigest()
    # the cookie value still authenticates against the hashed row
    assert security.is_admin(_Req(security.sign(tok))) is True


def test_admin_cookie_expires_on_the_admin_clock(monkeypatch):
    """90 days is right for a client browsing their own photos and wrong for
    admin, where it meant one stolen laptop session stayed valid for a quarter.
    """
    assert config.ADMIN_SESSION_MAX_AGE < config.SESSION_MAX_AGE
    tok = security.create_admin_session()
    raw = security.sign(tok)
    assert security.is_admin(_Req(raw)) is True

    # Expire the admin clock only; the client clock is untouched.
    monkeypatch.setattr(config, "ADMIN_SESSION_MAX_AGE", -1)
    assert security.is_admin(_Req(raw)) is False
    # Same cookie, client lifetime: still valid — proving the two are independent
    # rather than one number reused in two places.
    assert security.unsign(raw) == tok


def test_expired_admin_cookie_is_rejected_even_with_a_live_row(monkeypatch):
    """Signature expiry must gate before the row lookup, so an old cookie cannot
    be revived by the session row still being present."""
    tok = security.create_admin_session()
    raw = security.sign(tok)
    monkeypatch.setattr(config, "ADMIN_SESSION_MAX_AGE", -1)
    assert security.is_admin(_Req(raw)) is False
    assert db.one(
        "SELECT 1 AS x FROM admin_sessions WHERE token_hash=?",
        (security._admin_token_hash(tok),),
    ), "row still live — the rejection came from the clock, which is the point"


@pytest.mark.unit
@pytest.mark.parametrize(
    "supplied,expected,want",
    [
        ("1234", "1234", True),
        (" 1234 ", "1234", True),  # strip() preserved from the old compare
        ("1235", "1234", False),
        ("123", "1234", False),  # prefix must not pass
        ("12345", "1234", False),
        ("", "1234", False),
        ("1234", None, False),  # gallery with no PIN set never matches
        ("１２３４", "1234", False),  # non-ASCII must not raise (see check_admin_password)
    ],
)
def test_pin_matches(supplied, expected, want):
    assert security.pin_matches(supplied, expected) is want


@pytest.mark.unit
def test_pin_compare_is_constant_time_not_early_exit():
    """`!=` exits at the first wrong character, leaking how many leading
    characters were right — a per-character oracle that recovers a short numeric
    PIN in ~10*len guesses instead of 10**len. This asserts the primitive, not
    wall-clock timing, which is too noisy to test reliably.
    """
    import inspect

    src = inspect.getsource(security.pin_matches)
    assert "compare_digest" in src
    for path in ("app/public/gallery.py", "app/public/portal.py", "app/public/workspace.py"):
        text = open(path).read()
        assert "pin_matches" in text, path
        assert "pin.strip() != " not in text, f"{path} still uses a leaky compare"
