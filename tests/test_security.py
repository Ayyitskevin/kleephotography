"""Security helper tests — pure units + DB-backed lockout/session contracts."""

import pytest

from app import config, db, security

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_new_slug_length():
    s = security.new_slug(14)
    assert len(s) == 14
    assert all(c in security._BASE62 for c in s)


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


# ── DB-backed contracts (integration) ───────────────────────────────────────


@pytest.mark.integration
def test_pin_lockout_trips_at_max_fails():
    ip, gid = "203.0.113.50", -910_001
    db.run("DELETE FROM pin_attempts WHERE gallery_id=?", (gid,))
    assert not security.pin_locked(ip, gid)
    for _ in range(config.PIN_MAX_FAILS - 1):
        security.pin_fail(ip, gid)
        assert not security.pin_locked(ip, gid)
    security.pin_fail(ip, gid)
    assert security.pin_locked(ip, gid)
    security.pin_clear(ip, gid)
    assert not security.pin_locked(ip, gid)


@pytest.mark.integration
def test_inquiry_bucket_does_not_lock_gallery_pin():
    """Inquiry throttle sentinels must not count as gallery PIN failures."""
    ip = "203.0.113.51"
    gallery_id = 910_002
    inquiry_bucket = security.INQUIRY_BUCKET_CONTACT
    db.run("DELETE FROM pin_attempts WHERE ip=?", (ip,))
    for _ in range(security.INQUIRY_MAX_PER_WINDOW):
        security.inquiry_record(ip, inquiry_bucket)
    assert security.inquiry_throttled(ip, inquiry_bucket)
    assert not security.pin_locked(ip, gallery_id)
    db.run("DELETE FROM pin_attempts WHERE ip=?", (ip,))


@pytest.mark.integration
def test_admin_session_create_destroy_and_everywhere():
    db.run("DELETE FROM admin_sessions")
    tok = security.create_admin_session()
    assert db.one("SELECT 1 AS x FROM admin_sessions WHERE token=?", (tok,)) is not None
    # Second session; destroy-all clears every row (logout everywhere).
    security.create_admin_session()
    assert db.one("SELECT COUNT(*) AS n FROM admin_sessions")["n"] == 2
    security.destroy_all_admin_sessions()
    assert db.one("SELECT COUNT(*) AS n FROM admin_sessions")["n"] == 0


@pytest.mark.integration
def test_admin_login_bucket_exempt_from_target_circuit_breaker(monkeypatch):
    """gallery_id 0 (admin password) must not trip the distributed PIN breaker."""
    monkeypatch.setattr(config, "PIN_TARGET_MAX_FAILS", 3)
    monkeypatch.setattr(config, "PIN_MAX_FAILS", 100)  # stay under per-IP lockout
    db.run("DELETE FROM pin_attempts WHERE gallery_id=0")
    for i in range(5):
        security.pin_fail(f"203.0.113.{60 + i}", 0)
    assert not security.pin_locked("203.0.113.60", 0)
    db.run("DELETE FROM pin_attempts WHERE gallery_id=0")
