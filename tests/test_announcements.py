"""List announcements (revenue roadmap item 4).

What must hold: the audience is deduped and honours the unsubscribe ledger;
the ledger is reachable by the emailed token and idempotent; the send stages
one durable job per recipient in the same transaction as its paper-trail row;
delivery re-checks the ledger at send time and carries the CAN-SPAM footer;
and a misconfigured mailer refuses before staging anything.
"""

import time as _t

import pytest
from fastapi.testclient import TestClient

from app import announcements, config, db, jobs, mailer, security
from app.main import app

pytestmark = pytest.mark.integration

_STAMP = str(_t.time_ns())
_C1 = f"ann-c1-{_STAMP}@example.com"
_C2 = f"ann-c2-{_STAMP}@example.com"


@pytest.fixture(autouse=True)
def _clean():
    yield
    db.run("DELETE FROM unsubscribes WHERE email LIKE 'ann-%'")
    db.run("DELETE FROM announcements WHERE subject LIKE 'ann-test%'")
    db.run("DELETE FROM clients WHERE email LIKE 'ann-%'")
    db.run("DELETE FROM visitors WHERE email LIKE 'ann-%'")
    db.run("DELETE FROM jobs WHERE kind='announcement_email' AND payload LIKE '%ann-%'")


def _mine(people):
    return [p for p in people if p["email"].startswith("ann-")]


def _seed():
    db.run("INSERT INTO clients (name, email) VALUES ('Ann Client', ?)", (_C1,))
    db.run("INSERT INTO galleries (slug,title,pin) VALUES (?, 'AnnG', '1')", (f"ann-{_STAMP}",))
    gid = db.one("SELECT id FROM galleries WHERE slug=?", (f"ann-{_STAMP}",))["id"]
    # same person captured at the gate with different case — must dedup to one,
    # keeping the client's name; plus a gate-only address with no client row
    db.run(
        "INSERT INTO visitors (gallery_id, token, email) VALUES (?,?,?)",
        (gid, f"tok-{_STAMP}-1", _C1.upper()),
    )
    db.run(
        "INSERT INTO visitors (gallery_id, token, email) VALUES (?,?,?)",
        (gid, f"tok-{_STAMP}-2", _C2),
    )
    return gid


def test_audience_dedups_and_honours_the_ledger():
    _seed()
    mine = _mine(announcements.audience())
    assert [p["email"] for p in mine] == sorted([_C1, _C2])
    assert next(p for p in mine if p["email"] == _C1)["name"] == "Ann Client"
    announcements.record_unsubscribe(_C2)
    mine = _mine(announcements.audience())
    assert [p["email"] for p in mine] == [_C1]
    db.run("DELETE FROM galleries WHERE slug LIKE 'ann-%'")


def test_unsubscribe_flow_and_token_safety():
    tok = announcements.unsubscribe_token(_C1)
    with TestClient(app) as c:
        r = c.get(f"/u/{tok}")
        assert r.status_code == 200 and "Unsubscribe" in r.text
        assert not announcements.is_unsubscribed(_C1), "GET must not mutate — clients prefetch"
        r = c.post(f"/u/{tok}")
        assert r.status_code == 200 and announcements.is_unsubscribed(_C1)
        # idempotent, and the page now says done
        assert "off the list" in c.post(f"/u/{tok}").text
        # garbage and non-email signed values are a plain 404
        assert c.get("/u/not-a-token").status_code == 404
        assert c.get(f"/u/{security.sign('just-a-random-visitor-token')}").status_code == 404


def test_enqueue_stages_one_job_per_recipient(monkeypatch):
    _seed()
    dispatched = []
    monkeypatch.setattr(jobs, "dispatch", lambda ids: dispatched.extend(ids))
    watermark = db.one("SELECT COALESCE(MAX(id),0) AS m FROM jobs")["m"]
    aid, n = announcements.enqueue_send(f"ann-test {_STAMP}", "hello you")
    a = db.one("SELECT * FROM announcements WHERE id=?", (aid,))
    mine_jobs = db.all_(
        "SELECT * FROM jobs WHERE kind='announcement_email' AND id > ? AND payload LIKE '%ann-%'",
        (watermark,),
    )
    assert a["audience_count"] == n and n >= 2
    assert len(mine_jobs) == 2  # our two fixture addresses
    assert sorted(dispatched) == sorted(
        r["id"]
        for r in db.all_(
            "SELECT id FROM jobs WHERE kind='announcement_email' AND id > ?", (watermark,)
        )
    )
    db.run("DELETE FROM galleries WHERE slug LIKE 'ann-%'")


def test_deliver_sends_footer_and_respects_sendtime_unsubscribe(monkeypatch):
    db.run(
        "INSERT INTO announcements (subject, body, audience_count) VALUES (?,?,2)",
        (f"ann-test {_STAMP}", "the news"),
    )
    aid = db.one("SELECT id FROM announcements ORDER BY id DESC LIMIT 1")["id"]
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subj, body, **kw: sent.append((to, subj, body)))
    monkeypatch.setattr(announcements.time, "sleep", lambda s: None)

    announcements.deliver(aid, _C1, "Ann Client")
    assert len(sent) == 1
    to, subj, body = sent[0]
    assert body.startswith("Hi Ann Client,")
    assert "/u/" in body
    # the footer token must round-trip back to this exact address
    tok = body.split("/u/")[1].split()[0]
    assert announcements.email_from_token(tok) == _C1
    assert db.one("SELECT sent_count FROM announcements WHERE id=?", (aid,))["sent_count"] == 1

    # unsubscribed between staging and delivery -> skipped, not sent
    announcements.record_unsubscribe(_C2)
    announcements.deliver(aid, _C2, "")
    assert len(sent) == 1
    row = db.one("SELECT sent_count, skipped_count FROM announcements WHERE id=?", (aid,))
    assert (row["sent_count"], row["skipped_count"]) == (1, 1)


def test_send_route_refuses_without_mailer(monkeypatch):
    monkeypatch.setattr(mailer, "configured", lambda: False)
    watermark = db.one("SELECT COALESCE(MAX(id),0) AS m FROM jobs")["m"]
    with TestClient(app) as c:
        c.post("/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False)
        r = c.post("/admin/announcements/send", data={"subject": f"ann-test {_STAMP}", "body": "x"})
        assert r.status_code == 503
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE id > ?", (watermark,))["n"] == 0
