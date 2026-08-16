"""Prints phase one + reactivation (revenue roadmap 6 + 7).

What must hold: both asks create the right inquiry kind and notify Kevin; the
prints ask stays behind the PIN and off expired galleries; reactivation works
ONLY on expired galleries (it is necessarily an open form, so honeypot and
throttle carry the abuse load); and neither route trusts a tampered email.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app import db, inquiry_notify
from app.main import app

pytestmark = pytest.mark.integration

_STAMP = str(time.time_ns())


@pytest.fixture(autouse=True)
def _clean():
    yield
    db.run("DELETE FROM inquiries WHERE service LIKE 'pnr-%'")
    db.run(
        "DELETE FROM favorites WHERE visitor_id IN "
        "(SELECT id FROM visitors WHERE gallery_id IN "
        " (SELECT id FROM galleries WHERE slug LIKE 'pnr-%'))"
    )
    db.run(
        "DELETE FROM visitors WHERE gallery_id IN "
        "(SELECT id FROM galleries WHERE slug LIKE 'pnr-%')"
    )
    db.run(
        "DELETE FROM assets WHERE gallery_id IN (SELECT id FROM galleries WHERE slug LIKE 'pnr-%')"
    )
    db.run("DELETE FROM galleries WHERE slug LIKE 'pnr-%'")


def _gallery(expired=False):
    slug = f"pnr-{time.time_ns()}"
    db.run(
        "INSERT INTO galleries (slug, title, pin, published, expires_at) VALUES (?,?,?,1,?)",
        (slug, f"pnr-{slug}", "1234", "2020-01-01" if expired else None),
    )
    return slug, db.one("SELECT id FROM galleries WHERE slug=?", (slug,))["id"]


@pytest.fixture
def notified(monkeypatch):
    ids = []
    monkeypatch.setattr(inquiry_notify, "enqueue_owner_email", lambda iid: ids.append(iid))
    return ids


def test_prints_request_creates_inquiry_and_notifies(notified):
    slug, gid = _gallery()
    with TestClient(app) as c:
        c.post(f"/g/{slug}/pin", data={"pin": "1234"}, follow_redirects=False)
        r = c.post(
            f"/g/{slug}/prints",
            data={"email": "buyer@example.com", "note": "two 16x20s framed"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"].endswith("?prints=1")
    q = db.one("SELECT * FROM inquiries WHERE kind='prints' ORDER BY id DESC LIMIT 1")
    assert q["email"] == "buyer@example.com"
    assert "two 16x20s framed" in q["message"]
    assert f"/admin/galleries/{gid}" in q["message"]
    assert notified == [q["id"]]


def test_prints_requires_the_pin(notified):
    slug, _ = _gallery()
    with TestClient(app) as c:  # no PIN
        r = c.post(f"/g/{slug}/prints", data={"email": "x@x.com"})
        assert r.status_code == 403
    assert notified == []


def test_prints_refuses_expired_and_garbage_email(notified):
    slug, _ = _gallery(expired=True)
    with TestClient(app) as c:
        assert c.post(f"/g/{slug}/prints", data={"email": "x@x.com"}).status_code == 410
    live, _ = _gallery()
    with TestClient(app) as c:
        c.post(f"/g/{live}/pin", data={"pin": "1234"}, follow_redirects=False)
        assert c.post(f"/g/{live}/prints", data={"email": "not-an-email"}).status_code == 400
    assert notified == []


def test_reactivation_only_on_expired(notified):
    live_slug, _ = _gallery()
    dead_slug, gid = _gallery(expired=True)
    with TestClient(app) as c:
        # live gallery: the route does not exist for it
        assert c.post(f"/g/{live_slug}/reactivate", data={"email": "a@b.com"}).status_code == 404
        # expired gallery: the page itself offers the form...
        assert "Request re-opening" in c.get(f"/g/{dead_slug}").text
        # ...and the POST records the ask
        r = c.post(f"/g/{dead_slug}/reactivate", data={"email": "back@example.com", "note": "hi"})
        assert r.status_code == 200 and "Request received" in r.text
    q = db.one("SELECT * FROM inquiries WHERE kind='reactivation' ORDER BY id DESC LIMIT 1")
    assert q["email"] == "back@example.com"
    assert f"/admin/galleries/{gid}" in q["message"]
    assert notified == [q["id"]]


def test_reactivation_honeypot_pretends_success(notified):
    slug, _ = _gallery(expired=True)
    with TestClient(app) as c:
        r = c.post(
            f"/g/{slug}/reactivate",
            data={"email": "bot@spam.com", "website": "http://spam"},
        )
        assert r.status_code == 200 and "Request received" in r.text
    assert db.one("SELECT COUNT(*) n FROM inquiries WHERE email='bot@spam.com'")["n"] == 0
    assert notified == []
