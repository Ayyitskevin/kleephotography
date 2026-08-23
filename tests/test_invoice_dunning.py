"""Invoice dunning (enhancement brief R1).

The properties that matter: nothing reaches a client until Kevin arms the
feature; stages open exactly at due+3 / due+7 / due+14 on the studio wall
clock; each (invoice, stage) sends once no matter how often the sweep runs; a
failed send retries instead of silently marking done; only genuinely owed
invoices (sent/viewed/deposit_paid — the schema has no 'void' status) are ever
chased; and the sweep is notification-only — its writes land in the
invoice_dunning tracker and nowhere else, so payments stay single-writer
through the Stripe webhook.
"""

import datetime as dt
import sqlite3
import time

import pytest

from app import config, db, features, invoice_dunning, mailer
from app.admin import studio

pytestmark = pytest.mark.integration

TODAY = dt.date(2026, 6, 15)


def _mine(sent, *emails):
    """The suite shares one database, so an armed sweep may legitimately also
    dun overdue invoices other test files created — every assertion here scopes
    to this file's own recipients instead of counting the whole world."""
    wanted = emails or ("dun@example.com",)
    return [m for m in sent if m[0] in wanted]


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "INVOICE_DUNNING", True)
    monkeypatch.setattr(studio, "_today", lambda: TODAY)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    sent = []
    monkeypatch.setattr(
        mailer, "send", lambda to, subject, body, **kw: sent.append((to, subject, body))
    )
    return sent


def _invoice(
    days_overdue,
    *,
    status="sent",
    email="dun@example.com",
    total_cents=90000,
    deposit_cents=0,
):
    """Client + project + invoice due `days_overdue` days before TODAY."""
    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)", ("Dun Client", email))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?, 'Dun Proj')", (cid,))
    due_date = (TODAY - dt.timedelta(days=days_overdue)).isoformat()
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, deposit_cents,
                                 status, due_date)
           VALUES (?,?,?,?,?,?,?)""",
        (pid, f"dun-{time.time_ns()}", "Dun Invoice", total_cents, deposit_cents, status, due_date),
    )
    return db.one("SELECT * FROM invoices WHERE id=?", (iid,))


@pytest.fixture(autouse=True)
def _clean():
    yield
    db.run(
        """DELETE FROM invoice_dunning
           WHERE invoice_id IN (SELECT id FROM invoices WHERE slug LIKE 'dun-%')"""
    )
    db.run("DELETE FROM invoices WHERE slug LIKE 'dun-%'")
    db.run("DELETE FROM projects WHERE title='Dun Proj'")
    db.run("DELETE FROM clients WHERE name='Dun Client'")


def _stamps(invoice_id):
    return db.all_(
        "SELECT stage, due_date, sent_at FROM invoice_dunning WHERE invoice_id=? ORDER BY id",
        (invoice_id,),
    )


# --- migration: the tracker arrived via the runner ---------------------------


def test_migration_applied_via_runner():
    """conftest's db.migrate() (the real transactional runner) must have applied
    082 into the shared test DB: recorded in schema_migrations, table present,
    and the one-shot UNIQUE actually enforced."""
    assert db.one("SELECT name FROM schema_migrations WHERE name='082_invoice_dunning.sql'")
    assert db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='invoice_dunning'")
    inv = _invoice(3)
    db.run(
        "INSERT INTO invoice_dunning (invoice_id, stage, due_date) VALUES (?,?,?)",
        (inv["id"], "due3", inv["due_date"]),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.run(
            "INSERT INTO invoice_dunning (invoice_id, stage, due_date) VALUES (?,?,?)",
            (inv["id"], "due3", inv["due_date"]),
        )


# --- the gate: off by default, nothing moves until armed ---------------------


def test_unarmed_gate_sends_nothing(monkeypatch):
    """MISE_INVOICE_DUNNING unset (the shipped default) must mean zero client
    contact — the sweep bails before even probing the mailer."""
    monkeypatch.setattr(config, "INVOICE_DUNNING", False)
    monkeypatch.setattr(studio, "_today", lambda: TODAY)
    inv = _invoice(5)
    probed = []
    monkeypatch.setattr(mailer, "configured", lambda: probed.append(1) or True)
    monkeypatch.setattr(mailer, "send", lambda *a, **k: probed.append("sent"))
    invoice_dunning.sweep()
    assert probed == [], "unarmed sweep still probed the mailer"
    assert _stamps(inv["id"]) == []


def test_ships_dark_by_default():
    """No MISE_INVOICE_DUNNING in the environment (CI never sets it) must mean
    the gate reads OFF — arming is an explicit owner act, never a deploy side
    effect."""
    assert config.INVOICE_DUNNING is False
    assert features.invoice_dunning_enabled() is False


# --- stage selection at the boundaries ---------------------------------------


def test_nothing_before_due_plus_3(armed):
    _invoice(2)
    invoice_dunning.sweep()
    assert _mine(armed) == []


def test_polite_at_due_plus_3(armed):
    inv = _invoice(3)
    invoice_dunning.sweep()
    mine = _mine(armed)
    assert len(mine) == 1
    to, subject, body = mine[0]
    assert to == "dun@example.com"
    assert "gentle reminder" in subject
    assert f"/i/{inv['slug']}" in body
    assert "$900.00" in body
    assert "reply to this email" in body, "the escape valve is part of the tone"
    assert [(s["stage"], s["due_date"]) for s in _stamps(inv["id"])] == [("due3", inv["due_date"])]


def test_firmer_at_due_plus_7_boundary(armed):
    """due+6 is still the polite window; due+7 opens the firmer stage."""
    still_polite = _invoice(6)
    firmer = _invoice(7)
    invoice_dunning.sweep()
    assert len(_mine(armed)) == 2
    assert [s["stage"] for s in _stamps(still_polite["id"])] == ["due3"]
    assert [s["stage"] for s in _stamps(firmer["id"])] == ["due7"]
    firm_mail = [m for m in _mine(armed) if "past due" in m[1]]
    assert len(firm_mail) == 1


def test_final_at_due_plus_14_boundary(armed):
    late = _invoice(13)
    final = _invoice(14)
    invoice_dunning.sweep()
    assert [s["stage"] for s in _stamps(late["id"])] == ["due7"]
    assert [s["stage"] for s in _stamps(final["id"])] == ["due14"]
    assert any("Final reminder" in m[1] for m in _mine(armed))


def test_escalates_across_sweeps(armed, monkeypatch):
    """One invoice left unpaid walks the whole ladder: polite, firmer, final —
    three sends, three stamps, in order."""
    inv = _invoice(3)
    invoice_dunning.sweep()
    monkeypatch.setattr(studio, "_today", lambda: TODAY + dt.timedelta(days=4))  # due+7
    invoice_dunning.sweep()
    monkeypatch.setattr(studio, "_today", lambda: TODAY + dt.timedelta(days=11))  # due+14
    invoice_dunning.sweep()
    assert [s["stage"] for s in _stamps(inv["id"])] == ["due3", "due7", "due14"]
    assert len(_mine(armed)) == 3


def test_no_barrage_when_armed_late(armed):
    """An invoice already 20 days overdue when the feature is armed gets ONE
    email — the final notice — never a three-stage backlog at once."""
    inv = _invoice(20)
    invoice_dunning.sweep()
    invoice_dunning.sweep()
    assert len(_mine(armed)) == 1
    assert [s["stage"] for s in _stamps(inv["id"])] == ["due14"]


# --- idempotency and the stamp-after-send discipline -------------------------


def test_idempotent_per_invoice_stage(armed):
    inv = _invoice(4)
    invoice_dunning.sweep()
    invoice_dunning.sweep()
    invoice_dunning.sweep()
    assert len(_mine(armed)) == 1, "same (invoice, stage) sent more than once"
    assert len(_stamps(inv["id"])) == 1


def test_smtp_failure_leaves_no_stamp_and_retries(armed, monkeypatch):
    inv = _invoice(3)

    def boom(*a, **k):
        raise OSError("smtp down")

    monkeypatch.setattr(mailer, "send", boom)
    invoice_dunning.sweep()  # must not raise, must not stamp
    assert _stamps(inv["id"]) == [], "failed send was stamped as sent"
    monkeypatch.setattr(mailer, "send", lambda to, *a, **k: armed.append((to,)))
    invoice_dunning.sweep()
    assert len(_mine(armed)) == 1, "recovered send did not go out on the next sweep"
    assert [s["stage"] for s in _stamps(inv["id"])] == ["due3"]


def test_due_date_change_rearms_for_new_date(armed, monkeypatch):
    """Extending the due date ends the current chase; a fresh ladder starts
    from the NEW date (the stamp records which due_date it chased)."""
    inv = _invoice(3)
    invoice_dunning.sweep()
    assert len(_mine(armed)) == 1
    new_due = (TODAY + dt.timedelta(days=10)).isoformat()
    db.run("UPDATE invoices SET due_date=? WHERE id=?", (new_due, inv["id"]))
    invoice_dunning.sweep()
    assert len(_mine(armed)) == 1, "invoice no longer overdue but still chased"
    monkeypatch.setattr(studio, "_today", lambda: TODAY + dt.timedelta(days=13))  # new due+3
    invoice_dunning.sweep()
    assert len(_mine(armed)) == 2
    assert [(s["stage"], s["due_date"]) for s in _stamps(inv["id"])] == [
        ("due3", inv["due_date"]),
        ("due3", new_due),
    ]


# --- only genuinely owed invoices --------------------------------------------


def test_paid_and_draft_never_dunned(armed):
    paid = _invoice(10, status="paid")
    draft = _invoice(10, status="draft")
    invoice_dunning.sweep()
    assert _mine(armed) == []
    assert _stamps(paid["id"]) == [] and _stamps(draft["id"]) == []


def test_deposit_paid_is_chased_for_the_balance(armed):
    inv = _invoice(3, status="deposit_paid", total_cents=90000, deposit_cents=30000)
    invoice_dunning.sweep()
    mine = _mine(armed)
    assert len(mine) == 1
    assert "$600.00" in mine[0][2], "balance owed, never the already-paid total"
    assert [s["stage"] for s in _stamps(inv["id"])] == ["due3"]


def test_no_due_date_or_no_email_never_dunned(armed):
    undated = _invoice(5)
    db.run("UPDATE invoices SET due_date=NULL WHERE id=?", (undated["id"],))
    unreachable = _invoice(5, email=None)
    invoice_dunning.sweep()
    assert _mine(armed) == []
    assert _stamps(undated["id"]) == [] and _stamps(unreachable["id"]) == []


# --- notification-only: zero writes to money state ---------------------------


def test_sweep_writes_only_the_dunning_tracker(armed):
    """The red line from the brief: dunning may never touch payments state.
    After a successful send, the invoice row is byte-identical and payments is
    untouched — the tracker row is the only evidence."""
    inv = _invoice(7)
    before = dict(db.one("SELECT * FROM invoices WHERE id=?", (inv["id"],)))
    payments_before = db.one("SELECT COUNT(*) AS n FROM payments")["n"]
    invoice_dunning.sweep()
    assert len(_mine(armed)) == 1
    after = dict(db.one("SELECT * FROM invoices WHERE id=?", (inv["id"],)))
    assert after == before, "dunning sweep mutated the invoice row"
    assert db.one("SELECT COUNT(*) AS n FROM payments")["n"] == payments_before
    assert len(_stamps(inv["id"])) == 1
