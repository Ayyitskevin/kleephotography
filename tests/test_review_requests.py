"""Review engine (revenue roadmap item 2).

The properties that matter: it never fires while dormant; the ask is one-shot
per gallery and cooled-down per client; expired and unpublished galleries are
never asked; and a failed send retries instead of silently marking done.
"""

import datetime as dt
import time

import pytest

from app import config, db, mailer, review_requests

pytestmark = pytest.mark.integration

_URL = "https://g.page/r/test-review-link/review"


def _mine(sent, *emails):
    """The suite shares one database, so the sweep legitimately also emails
    galleries other test files created — every assertion here scopes to this
    file's own recipients instead of counting the whole world."""
    wanted = emails or ("rev@example.com",)
    return [m for m in sent if m[0] in wanted]


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_REVIEW_URL", _URL)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    sent = []
    monkeypatch.setattr(
        mailer, "send", lambda to, subject, body, **kw: sent.append((to, subject, body))
    )
    return sent


def _gallery(
    client_email="rev@example.com", days_old=10, published=1, expires=None, client_id=None
):
    if client_id is None:
        db.run("INSERT INTO clients (name, email) VALUES (?,?)", ("Rev Client", client_email))
        client_id = db.one("SELECT id FROM clients ORDER BY id DESC LIMIT 1")["id"]
    slug = f"rev-{time.time_ns()}"
    db.run(
        """INSERT INTO galleries (slug, title, pin, published, client_id, expires_at,
                                  created_at)
           VALUES (?,?,?,?,?,?,datetime('now', ?))""",
        (slug, "Review Gallery", "1234", published, client_id, expires, f"-{days_old} days"),
    )
    g = db.one("SELECT * FROM galleries WHERE slug=?", (slug,))
    return g, client_id


@pytest.fixture(autouse=True)
def _clean():
    yield
    db.run("DELETE FROM galleries WHERE slug LIKE 'rev-%'")
    db.run("DELETE FROM clients WHERE name='Rev Client'")


def test_dormant_without_url(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_REVIEW_URL", "")
    called = []
    monkeypatch.setattr(mailer, "configured", lambda: called.append(1) or True)
    review_requests.sweep()
    assert called == [], "dormant engine still probed the mailer"


def test_asks_once_and_stamps(armed):
    g, _ = _gallery()
    review_requests.sweep()
    mine = _mine(armed)
    assert len(mine) == 1
    to, subject, body = mine[0]
    assert to == "rev@example.com"
    assert _URL in body
    assert "reply to this email" in body, "the private escape valve is the point"
    assert db.one("SELECT review_requested_at FROM galleries WHERE id=?", (g["id"],))[
        "review_requested_at"
    ]
    # one-shot: a second sweep sends nothing
    review_requests.sweep()
    assert len(_mine(armed)) == 1


def test_not_ripe_yet(armed, monkeypatch):
    monkeypatch.setattr(config, "REVIEW_ASK_DAYS", 3)
    _gallery(days_old=1)
    review_requests.sweep()
    assert _mine(armed) == []


def test_skips_unpublished_and_expired(armed):
    _gallery(published=0)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _gallery(expires=yesterday)
    review_requests.sweep()
    assert _mine(armed) == []


def test_client_cooldown_spans_galleries(armed):
    """A family with two galleries this year is one relationship, one ask."""
    g1, cid = _gallery()
    review_requests.sweep()
    assert len(_mine(armed)) == 1
    # second gallery for the SAME client, also ripe
    _gallery(client_id=cid)
    review_requests.sweep()
    assert len(_mine(armed)) == 1, "same client asked twice inside the cooldown"
    # ...but a DIFFERENT client is a different relationship
    _gallery(client_email="other@example.com")
    review_requests.sweep()
    assert len(_mine(armed, "other@example.com")) == 1
    # and once the first ask ages past the cooldown, the second gallery is fair
    db.run(
        "UPDATE galleries SET review_requested_at=datetime('now', ?) WHERE id=?",
        (f"-{config.REVIEW_COOLDOWN_DAYS + 5} days", g1["id"]),
    )
    review_requests.sweep()
    assert len(_mine(armed)) == 2


def test_send_failure_retries_next_sweep(armed, monkeypatch):
    g, _ = _gallery()

    def boom(*a, **k):
        raise OSError("smtp down")

    monkeypatch.setattr(mailer, "send", boom)
    review_requests.sweep()  # must not raise, must not stamp
    assert (
        db.one("SELECT review_requested_at FROM galleries WHERE id=?", (g["id"],))[
            "review_requested_at"
        ]
        is None
    )
    monkeypatch.setattr(mailer, "send", lambda *a, **k: armed.append(a))
    review_requests.sweep()
    assert len(_mine(armed)) == 1, "recovered send did not go out on the next sweep"
