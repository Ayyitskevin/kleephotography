"""G6 — write relief on the hottest read path.

`visitors.last_seen` was rewritten on EVERY request that resolves a visitor, and
the busiest of those is the per-image media route. Opening a 60-photo gallery
meant 60 serialized writes — each taking SQLite's single write lock — on the one
path deliberately exempt from rate limiting, for a column no code reads.
"""

import time

import pytest

from app import config, db, security

pytestmark = pytest.mark.integration


def _visitor():
    db.run(
        "INSERT INTO galleries (slug,title,pin,published) VALUES (?,?,?,1)",
        (f"hp-{time.time_ns()}", "Hot Path", "1234"),
    )
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    vid, cookie = security.create_visitor(g["id"])
    return g, vid, cookie


class _Req:
    def __init__(self, gid, cookie):
        self.cookies = {security.visitor_cookie_name(gid): cookie}


def _count_visitor_updates(fn):
    """Count UPDATE visitors statements that actually reach the database."""
    seen = {"n": 0}
    real = db.run

    def counting(sql, params=()):
        if sql.strip().upper().startswith("UPDATE VISITORS"):
            seen["n"] += 1
        return real(sql, params)

    db.run = counting
    try:
        fn()
    finally:
        db.run = real
    return seen["n"]


def test_gallery_load_does_not_write_once_per_thumbnail():
    g, vid, cookie = _visitor()
    n = _count_visitor_updates(
        lambda: [security.get_visitor(_Req(g["id"], cookie), g["id"]) for _ in range(60)]
    )
    assert n == 1, f"60 thumbnail requests issued {n} writes"
    # a second load inside the same window writes nothing at all
    again = _count_visitor_updates(
        lambda: [security.get_visitor(_Req(g["id"], cookie), g["id"]) for _ in range(60)]
    )
    assert again == 0


def test_last_seen_still_refreshes_after_the_window():
    g, vid, cookie = _visitor()
    security.get_visitor(_Req(g["id"], cookie), g["id"])
    stale = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 3600))
    db.run("UPDATE visitors SET last_seen=? WHERE id=?", (stale, vid))
    security.get_visitor(_Req(g["id"], cookie), g["id"])
    assert db.one("SELECT last_seen FROM visitors WHERE id=?", (vid,))["last_seen"] > stale


def test_first_visit_records_last_seen():
    """A NULL last_seen must still be written — the debounce is not a skip."""
    g, vid, cookie = _visitor()
    assert db.one("SELECT last_seen FROM visitors WHERE id=?", (vid,))["last_seen"] is None
    security.get_visitor(_Req(g["id"], cookie), g["id"])
    assert db.one("SELECT last_seen FROM visitors WHERE id=?", (vid,))["last_seen"] is not None


def test_concurrent_stale_readers_do_not_each_write():
    """The race the WHERE clause exists for.

    A browser opens several connections at once. They all read the row before any
    of them commits, so every one of them sees a stale last_seen and the Python
    guard lets them all through. Without the WHERE clause each would dirty a page
    and fsync; with it, only the first commit writes.
    """
    g, vid, cookie = _visitor()
    security.get_visitor(_Req(g["id"], cookie), g["id"])  # establishes a fresh value
    fresh = db.one("SELECT last_seen FROM visitors WHERE id=?", (vid,))["last_seen"]

    # A second request that read the row BEFORE the first one committed.
    stale_view = {"id": vid, "last_seen": "2000-01-01 00:00:00"}
    security._touch_last_seen(stale_view)

    assert db.one("SELECT last_seen FROM visitors WHERE id=?", (vid,))["last_seen"] == fresh


@pytest.mark.unit
def test_sqlite_synchronous_is_normal_and_misconfiguration_fails_safe(monkeypatch):
    con = db.connect()
    try:
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 1  # 1 = NORMAL
    finally:
        con.close()

    # An unrecognised value must fall back toward MORE durability, never less —
    # and never silently to OFF.
    monkeypatch.setattr(config, "SQLITE_SYNCHRONOUS", "OFF")
    con = db.connect()
    try:
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 2  # 2 = FULL
    finally:
        con.close()
