"""Smoke domain slice — see tests/smoke/conftest.py for fixtures."""

import io
import os
import re
import tempfile
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db, platekit
from app.main import app
from tests.smoke._helpers import (
    _checkout_event,
    _cleanup_money_chain,
    _close,
    _jpeg_bytes,
    _logo_png,
    _mp4_bytes,
    _post_signed,
    _quo_sig,
    _ready_photo_gallery,
    _ready_video,
    _seam_license_with_gallery,
    _seed_money_chain,
    _spark_rect_count,
    _stripe_sig,
)

pytestmark = pytest.mark.smoke


def test_studio_clients_projects(admin):
    # client
    r = admin.post(
        "/admin/studio/clients",
        data={
            "name": "Dana Chef",
            "company": "Test Bistro",
            "email": "dana@bistro.com",
            "phone": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    assert c["name"] == "Dana Chef" and c["company"] == "Test Bistro"

    # project
    r = admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Spring menu shoot"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    assert p["client_id"] == c["id"] and p["status"] == "inquiry_received"

    # status advances and pages render
    r = admin.post(
        f"/admin/studio/projects/{p['id']}",
        data={
            "title": p["title"],
            "status": "proposal_sent",
            "notes": "",
            "gallery_id": "",
            "notion_page_id": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.one("SELECT status FROM projects WHERE id=?", (p["id"],))["status"] == "proposal_sent"
    for url in (
        "/admin/studio",
        f"/admin/studio/clients/{c['id']}",
        f"/admin/studio/projects/{p['id']}",
    ):
        assert admin.get(url).status_code == 200

    # bad status rejected
    r = admin.post(
        f"/admin/studio/projects/{p['id']}",
        data={"title": p["title"], "status": "bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_client_activity_timeline(admin):
    # The client page must narrate document history across ALL of a client's
    # sessions in one reverse-chron feed — the gap the per-project timeline left
    # open (you had to open each session to see what happened). If a sent
    # proposal's event never reaches the client page, this view is broken.
    r = admin.post(
        "/admin/studio/clients",
        data={
            "name": "Marco Feed",
            "company": "Trattoria",
            "email": "marco@trattoria.com",
            "phone": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")

    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Autumn menu shoot"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # empty state before any document activity
    page = admin.get(f"/admin/studio/clients/{c['id']}").text
    assert "Recent activity" in page
    assert "No document activity yet" in page

    # a sent proposal produces drafted + sent events that must surface here,
    # not only on the project page
    admin.post(
        f"/admin/studio/projects/{p['id']}/proposals",
        data={"preset": "photo_starter"},
        follow_redirects=False,
    )
    d = db.one("SELECT * FROM proposals ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/proposals/{d['id']}/send", follow_redirects=False)

    page = admin.get(f"/admin/studio/clients/{c['id']}").text
    assert "No document activity yet" not in page
    assert 'class="timeline"' in page
    assert f"Proposal “{d['title']}” sent" in page

    # Clean up: force-delete this client so the "latest client/project" rows the
    # downstream studio lifecycle tests depend on revert to their fixtures.
    r = admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_proposal_lifecycle(admin):

    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create from preset
    r = admin.post(
        f"/admin/studio/projects/{p['id']}/proposals",
        data={"preset": "photo_starter"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    d = db.one("SELECT * FROM proposals ORDER BY id DESC LIMIT 1")
    assert d["status"] == "draft" and d["total_cents"] == 75000
    page = admin.get(f"/admin/studio/proposals/{d['id']}")
    assert page.status_code == 200
    # copy-link macro emits a "Copy link" button (no PIN) carrying the public URL
    assert "Copy link</button>" in page.text  # no "+ PIN" because pin=None
    assert f'data-copy="{config.BASE_URL}/p/{d["slug"]}"' in page.text

    # draft is hidden from the public link
    with TestClient(app) as pub:
        assert pub.get(f"/p/{d['slug']}").status_code == 404

    # edit draft items (recalculates total)
    r = admin.post(
        f"/admin/studio/proposals/{d['id']}",
        data={
            "title": d["title"],
            "intro": "Hi Dana",
            "item_label_0": "Half-day session",
            "item_qty_0": "1",
            "item_price_0": "1000",
            "item_label_1": "Extra dishes",
            "item_qty_1": "2",
            "item_price_1": "75.50",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    d = db.one("SELECT * FROM proposals WHERE id=?", (d["id"],))
    assert d["total_cents"] == 100000 + 15100

    # mark sent — locks editing, advances project
    db.run("UPDATE projects SET status='inquiry_received' WHERE id=?", (p["id"],))
    r = admin.post(f"/admin/studio/proposals/{d['id']}/send", follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM proposals WHERE id=?", (d["id"],))
    assert d["status"] == "sent" and d["sent_at"]
    assert db.one("SELECT status FROM projects WHERE id=?", (p["id"],))["status"] == "proposal_sent"
    r = admin.post(
        f"/admin/studio/proposals/{d['id']}", data={"title": "nope"}, follow_redirects=False
    )
    assert r.status_code == 400

    # public view flips sent → viewed; accept records acceptance but does NOT
    # advance the project (the pipeline advances on contract SIGN, not proposal
    # accept — there is no proposal_accepted stage in the 8-stage funnel)
    with TestClient(app) as pub:
        assert pub.get(f"/p/{d['slug']}").status_code == 200
        d = db.one("SELECT * FROM proposals WHERE id=?", (d["id"],))
        assert d["status"] == "viewed" and d["viewed_at"]
        r = pub.post(f"/p/{d['slug']}/accept", follow_redirects=False)
        assert r.status_code == 303
        d = db.one("SELECT * FROM proposals WHERE id=?", (d["id"],))
        assert d["status"] == "accepted" and d["accepted_at"]
        assert (
            db.one("SELECT status FROM projects WHERE id=?", (p["id"],))["status"]
            == "proposal_sent"
        )
        # accepted proposals can't be re-actioned
        assert pub.post(f"/p/{d['slug']}/decline", follow_redirects=False).status_code == 400


def test_contract_lifecycle(admin):

    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create — merge fields pull from project + accepted proposal total
    r = admin.post(f"/admin/studio/projects/{p['id']}/contracts", follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM contracts ORDER BY id DESC LIMIT 1")
    assert d["status"] == "draft" and "Dana Chef" in d["body"]
    assert "$1151.00" in d["body"]  # accepted proposal total merged in
    page = admin.get(f"/admin/studio/contracts/{d['id']}")
    assert page.status_code == 200
    assert "Copy link</button>" in page.text  # macro w/ pin=None
    assert f'data-copy="{config.BASE_URL}/c/{d["slug"]}"' in page.text

    # draft hidden from public; editable
    with TestClient(app) as pub:
        assert pub.get(f"/c/{d['slug']}").status_code == 404
    r = admin.post(
        f"/admin/studio/contracts/{d['id']}",
        data={"title": d["title"], "body": d["body"] + "\n8. EXTRA — Test clause."},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # send locks body and records hash
    r = admin.post(f"/admin/studio/contracts/{d['id']}/send", follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM contracts WHERE id=?", (d["id"],))
    assert d["status"] == "sent" and len(d["body_sha256"]) == 64
    r = admin.post(
        f"/admin/studio/contracts/{d['id']}",
        data={"title": "x", "body": "tampered"},
        follow_redirects=False,
    )
    assert r.status_code == 400

    with TestClient(app) as pub:
        # view flips sent → viewed; the e-sign surface NEVER opts into the
        # Screening Room scope (legal document — stays on the cream theme)
        page = pub.get(f"/c/{d['slug']}")
        assert page.status_code == 200
        assert 'class="cream-theme"' in page.text
        assert db.one("SELECT status FROM contracts WHERE id=?", (d["id"],))["status"] == "viewed"

        # tampered body refuses signature (integrity check)
        db.run("UPDATE contracts SET body=body||' ' WHERE id=?", (d["id"],))
        r = pub.post(
            f"/c/{d['slug']}/sign",
            data={"signer_name": "Dana Chef", "agree": "yes"},
            follow_redirects=False,
        )
        assert r.status_code == 409
        db.run("UPDATE contracts SET body=rtrim(body,' ') WHERE id=?", (d["id"],))

        # sign records name/ip/timestamp, advances project to contract_signed
        r = pub.post(
            f"/c/{d['slug']}/sign",
            data={"signer_name": "Dana Chef", "agree": "yes"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        d = db.one("SELECT * FROM contracts WHERE id=?", (d["id"],))
        assert (
            d["status"] == "signed"
            and d["signer_name"] == "Dana Chef"
            and d["signed_at"]
            and d["signer_ip"]
        )
        assert (
            db.one("SELECT status FROM projects WHERE id=?", (p["id"],))["status"]
            == "contract_signed"
        )
        # signed contract renders the signature record, can't be re-signed
        assert "Signed by Dana Chef" in pub.get(f"/c/{d['slug']}").text
        assert (
            pub.post(
                f"/c/{d['slug']}/sign",
                data={"signer_name": "X", "agree": "yes"},
                follow_redirects=False,
            ).status_code
            == 400
        )


def test_invoice_lifecycle(admin, monkeypatch):

    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create — seeds items/total from the accepted proposal
    r = admin.post(f"/admin/studio/projects/{p['id']}/invoices", follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
    assert d["status"] == "draft" and d["total_cents"] == 115100
    page = admin.get(f"/admin/studio/invoices/{d['id']}")
    assert page.status_code == 200
    assert "Copy link</button>" in page.text  # macro w/ pin=None
    assert f'data-copy="{config.BASE_URL}/i/{d["slug"]}"' in page.text
    with TestClient(app) as pub:
        assert pub.get(f"/i/{d['slug']}").status_code == 404

    # deposit above total rejected; valid deposit + due date saved
    base = {
        "title": d["title"],
        "item_label_0": "Shoot package",
        "item_qty_0": "1",
        "item_price_0": "1151",
    }
    r = admin.post(
        f"/admin/studio/invoices/{d['id']}", data=base | {"deposit": "2000"}, follow_redirects=False
    )
    assert r.status_code == 400
    r = admin.post(
        f"/admin/studio/invoices/{d['id']}",
        data=base | {"deposit": "500", "due_date": "2026-07-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    d = db.one("SELECT * FROM invoices WHERE id=?", (d["id"],))
    assert d["deposit_cents"] == 50000 and d["due_date"] == "2026-07-01"

    # send locks it; public view flips sent → viewed
    r = admin.post(f"/admin/studio/invoices/{d['id']}/send", follow_redirects=False)
    assert r.status_code == 303
    with TestClient(app) as pub:
        page = pub.get(f"/i/{d['slug']}")
        assert page.status_code == 200 and "$500.00" in page.text
        assert db.one("SELECT status FROM invoices WHERE id=?", (d["id"],))["status"] == "viewed"
        # payments not configured → pay degrades, webhook refuses
        assert pub.post(f"/i/{d['slug']}/pay", follow_redirects=False).status_code == 503
        assert pub.post("/webhooks/stripe", content=b"{}").status_code == 503

        # webhook with signature verification
        monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        body = _checkout_event("evt_dep_1", d["id"], "deposit", 50000)
        assert (
            pub.post(
                "/webhooks/stripe", content=body, headers={"stripe-signature": "t=1,v1=bad"}
            ).status_code
            == 400
        )
        r = pub.post(
            "/webhooks/stripe",
            content=body,
            headers={"stripe-signature": _stripe_sig(body, "whsec_test")},
        )
        assert r.status_code == 200
        assert (
            db.one("SELECT status FROM invoices WHERE id=?", (d["id"],))["status"] == "deposit_paid"
        )
        # retried event is idempotent
        r = pub.post(
            "/webhooks/stripe",
            content=body,
            headers={"stripe-signature": _stripe_sig(body, "whsec_test")},
        )
        assert r.json().get("duplicate") is True
        assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (d["id"],))["n"] == 1

        # balance payment settles the invoice
        body = _checkout_event("evt_bal_1", d["id"], "balance", 65100)
        r = pub.post(
            "/webhooks/stripe",
            content=body,
            headers={"stripe-signature": _stripe_sig(body, "whsec_test")},
        )
        assert r.status_code == 200
        d = db.one("SELECT * FROM invoices WHERE id=?", (d["id"],))
        assert d["status"] == "paid" and d["paid_at"]
        assert "Paid in full" in pub.get(f"/i/{d['slug']}").text


def test_reports_top_clients(admin):
    # Reports must rank clients by cash actually collected (Stripe payments are the
    # truth), not by invoiced/booked value — a client who never pays shouldn't top
    # the list. A repeat payer (>=2 paid projects) must be flagged. If the collected
    # total or the repeat signal is wrong here, the value leaderboard is misleading.
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Lucia Vega",
            "company": "Osteria Vega",
            "email": "lucia@osteriavega.com",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    # Two separate projects, each with a paid invoice → repeat booker.
    pids, iids = [], []
    for n, cents in (("Spring tasting", 600000), ("Autumn tasting", 400000)):
        admin.post(
            f"/admin/studio/clients/{c['id']}/projects", data={"title": n}, follow_redirects=False
        )
        p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        pids.append(p["id"])
        iid = db.run(
            """INSERT INTO invoices (project_id, slug, title, line_items, total_cents)
                        VALUES (?,?,?,?,?)""",
            (p["id"], f"inv-{p['id']}", "Invoice", "[]", cents),
        )
        iids.append(iid)
        db.run(
            """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
                  amount_cents, kind) VALUES (?,?,?,?,?)""",
            (iid, f"evt_{iid}", f"cs_{iid}", cents, "full"),
        )

    page = admin.get("/admin/reports").text
    assert "Top clients" in page
    assert "Osteria Vega" in page
    assert "$10,000" in page  # 600000 + 400000 cents collected
    # Two paid projects → repeat badge.
    block = page.split("Osteria Vega", 1)[1][:200]
    assert "repeat" in block

    # Clean up everything created so the latest-invoice / aggregate counts the
    # downstream lifecycle tests depend on revert to their fixtures.
    for iid in iids:
        db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
        db.run("DELETE FROM invoices WHERE id=?", (iid,))
    r = admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_reports_range_toggle(admin):
    # Reports headline numbers must scope to the selected range (month/quarter/
    # YTD/last-year) — the same pills the Income page uses. An unknown range must
    # fall back to YTD, not 500. If the toggle silently ignores ?range=, the page
    # would always show YTD and the pills would be decorative lies.
    for key, label in (
        ("month", "This month"),
        ("quarter", "Quarter"),
        ("ytd", "YTD"),
        ("lastyear", "Last year"),
    ):
        page = admin.get(f"/admin/reports?range={key}").text
        assert "fin-range-pill" in page
        assert f'href="/admin/reports?range={key}"' in page
        # the active pill carries fin-range-on next to its own key
        on = page.split(f"?range={key}", 1)[1][:60]
        assert "fin-range-on" in on
    # garbage range → YTD fallback, still 200
    bad = admin.get("/admin/reports?range=bogus")
    assert bad.status_code == 200
    assert "fin-range-on" in bad.text


def test_tasks_board_view(admin):
    # The strict-1:1 Tasks page is a 3-column board (Today / This week / Done),
    # bucketed server-side from due_date. Encodes WHY the buckets matter: a task
    # due today (or overdue) belongs in Today; an open task with no/later due
    # date belongs in This week. Guard the column labels and the bucketing.
    import datetime as _dt

    today_iso = _dt.date.today().isoformat()
    admin.post(
        "/admin/tasks",
        data={"title": "Due-today board task", "due_date": today_iso},
        follow_redirects=False,
    )
    admin.post("/admin/tasks", data={"title": "Undated board task"}, follow_redirects=False)
    page = admin.get("/admin/tasks").text
    assert "tk-grid" in page
    for label in (">Today<", ">This week<", ">Done<"):
        assert label in page
    # the due-today task sorts ahead of the undated one (Today column renders
    # before This week) — both present, the urgent one first in document order.
    assert page.index("Due-today board task") < page.index("Undated board task")
    db.run("DELETE FROM tasks WHERE title IN ('Due-today board task','Undated board task')")


def test_manage_nav_financials_expenses(admin, monkeypatch):
    # Deck rail (Screening Room): Money is a first-class rail stop, lit on every
    # financials path (income AND expenses/mileage — one Ledger, tabs inside).
    # The legacy sidebar behind the kill switch keeps the old split guards:
    # Financials lights on income only, Expenses on expenses/mileage only.

    inc = admin.get("/admin/financials").text
    assert 'href="/admin/financials" title="Money" class="is-active"' in inc
    exp = admin.get("/admin/financials/expenses").text
    assert 'href="/admin/financials" title="Money" class="is-active"' in exp

    monkeypatch.setattr(config, "SCREENING_ROOM", False)
    inc = admin.get("/admin/financials").text
    assert 'href="/admin/financials" title="Financials" class="is-active"' in inc
    assert 'href="/admin/financials/expenses" title="Expenses"><' in inc
    exp = admin.get("/admin/financials/expenses").text
    assert 'href="/admin/financials/expenses" title="Expenses" class="is-active"' in exp
    assert 'href="/admin/financials" title="Financials"><' in exp


def test_invoice_receipt(admin):
    # A paid invoice must offer a printable receipt that lists the recorded
    # payments and totals — for the client's accountant. The receipt is a pure
    # read of the payments table (the source Stripe writes), so it can never
    # disagree with what was charged. No payments → no receipt (404).
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Priya Anand",
            "company": "Saffron Counter",
            "email": "priya@saffron.test",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Menu refresh"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    # Deposit then balance, both recorded → receipt shows two lines, paid in full.
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items,
                                          total_cents, status, paid_at)
                    VALUES (?,?,?,?,?,?,datetime('now'))""",
        (p["id"], "rcpt-test", "Menu refresh shoot", "[]", 500000, "paid"),
    )
    inv = db.one("SELECT * FROM invoices WHERE id=?", (iid,))
    for kind, cents in (("deposit", 200000), ("balance", 300000)):
        db.run(
            """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
                  amount_cents, kind) VALUES (?,?,?,?,?)""",
            (iid, f"evt_{kind}_{iid}", f"cs_{kind}_{iid}", cents, kind),
        )

    r = admin.get(f"/i/{inv['slug']}/receipt")
    assert r.status_code == 200
    assert "Deposit" in r.text and "Balance" in r.text
    assert "$2000.00" in r.text and "$3000.00" in r.text
    assert "$5000.00" in r.text  # total paid
    assert "Paid in full" in r.text
    # The invoice page links to the receipt once a payment exists.
    assert f"/i/{inv['slug']}/receipt" in admin.get(f"/i/{inv['slug']}").text

    # An invoice with no payments has no receipt.
    jid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items,
                                          total_cents, status)
                    VALUES (?,?,?,?,?,?)""",
        (p["id"], "rcpt-empty", "Unpaid", "[]", 100000, "sent"),
    )
    assert admin.get("/i/rcpt-empty/receipt").status_code == 404

    db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
    db.run("DELETE FROM invoices WHERE id IN (?,?)", (iid, jid))
    admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_proposal_convert(admin):
    # Once a client accepts a proposal, one click should spawn the matching draft
    # contract + draft invoice instead of rebuilding both by hand. Both land as
    # drafts (Kevin still reviews/sends — nothing is charged); the invoice copies
    # the proposal's line items + total verbatim and the contract body carries the
    # accepted total. A proposal that isn't accepted yet can't be converted.
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Marco Reyes",
            "company": "Ember Room",
            "email": "marco@ember.test",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Dinner service shoot"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    items = '[{"label": "Full-day shoot", "qty": 1, "unit_cents": 180000}]'
    pid = db.run(
        """INSERT INTO proposals (project_id, slug, title, line_items,
                                           total_cents, status, accepted_at)
                    VALUES (?,?,?,?,?,?,datetime('now'))""",
        (p["id"], "conv-test", "Dinner proposal", items, 180000, "accepted"),
    )

    # the accepted proposal page offers the convert action
    page = admin.get(f"/admin/studio/proposals/{pid}")
    assert page.status_code == 200
    assert f"/admin/studio/proposals/{pid}/convert" in page.text

    r = admin.post(f"/admin/studio/proposals/{pid}/convert", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/studio/projects/{p['id']}"

    ct = db.one("SELECT * FROM contracts WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],))
    inv = db.one("SELECT * FROM invoices WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],))
    assert ct and ct["status"] == "draft"
    assert "$1800.00" in ct["body"]  # accepted total merged into the contract body
    assert inv and inv["status"] == "draft"
    assert inv["total_cents"] == 180000 and inv["line_items"] == items

    # a proposal that hasn't been accepted can't be converted
    qid = db.run(
        """INSERT INTO proposals (project_id, slug, title, line_items,
                                           total_cents, status)
                    VALUES (?,?,?,?,?,?)""",
        (p["id"], "conv-draft", "Draft proposal", items, 180000, "sent"),
    )
    assert (
        admin.post(f"/admin/studio/proposals/{qid}/convert", follow_redirects=False).status_code
        == 400
    )

    db.run("DELETE FROM contracts WHERE id=?", (ct["id"],))
    db.run("DELETE FROM invoices WHERE id=?", (inv["id"],))
    db.run("DELETE FROM proposals WHERE id IN (?,?)", (pid, qid))
    admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_proposal_duplicate(admin):
    # A locked proposal (sent/accepted/declined) can be cloned into a fresh
    # editable draft — the revise-and-re-send path. The copy carries the same
    # title/intro/line items but its own slug and status='draft'; the original
    # is left untouched.
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Lena Voss",
            "company": "Copper Pot",
            "email": "lena@copper.test",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Brunch menu shoot"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    items = '[{"label": "Half-day shoot", "qty": 1, "unit_cents": 90000}]'
    src = db.run(
        """INSERT INTO proposals (project_id, slug, title, intro, line_items,
                    total_cents, status, sent_at)
                    VALUES (?,?,?,?,?,?,?,datetime('now'))""",
        (p["id"], "dup-src", "Brunch proposal", "Hi Lena", items, 90000, "declined"),
    )

    # the locked proposal page offers the duplicate action
    page = admin.get(f"/admin/studio/proposals/{src}")
    assert f"/admin/studio/proposals/{src}/duplicate" in page.text

    r = admin.post(f"/admin/studio/proposals/{src}/duplicate", follow_redirects=False)
    assert r.status_code == 303
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert new_id != src
    new = db.one("SELECT * FROM proposals WHERE id=?", (new_id,))
    assert new["status"] == "draft" and new["slug"] != "dup-src"
    assert (
        new["title"] == "Brunch proposal"
        and new["intro"] == "Hi Lena"
        and new["line_items"] == items
        and new["total_cents"] == 90000
    )
    # original is untouched
    assert db.one("SELECT status FROM proposals WHERE id=?", (src,))["status"] == "declined"

    db.run("DELETE FROM proposals WHERE id IN (?,?)", (src, new_id))
    admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_contract_duplicate(admin):
    # A locked/signed contract clones into a fresh editable draft: same body + title,
    # new slug, no hash or signature carried over. Original untouched.
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Nadia Okafor",
            "company": "Olive & Ash",
            "email": "nadia@oliveash.test",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Cookbook shoot"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    src = db.run(
        """INSERT INTO contracts (project_id, slug, title, body, body_sha256,
                    status, signer_name, signer_ip, signed_at)
                    VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
        (
            p["id"],
            "cdup-src",
            "Services Agreement",
            "BODY TEXT",
            "a" * 64,
            "signed",
            "Nadia Okafor",
            "1.2.3.4",
        ),
    )

    page = admin.get(f"/admin/studio/contracts/{src}")
    assert f"/admin/studio/contracts/{src}/duplicate" in page.text
    r = admin.post(f"/admin/studio/contracts/{src}/duplicate", follow_redirects=False)
    assert r.status_code == 303
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    new = db.one("SELECT * FROM contracts WHERE id=?", (new_id,))
    assert new["status"] == "draft" and new["slug"] != "cdup-src"
    assert new["title"] == "Services Agreement" and new["body"] == "BODY TEXT"
    assert new["body_sha256"] is None and new["signer_name"] is None
    assert db.one("SELECT status FROM contracts WHERE id=?", (src,))["status"] == "signed"

    db.run("DELETE FROM contracts WHERE id IN (?,?)", (src, new_id))
    admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )


def test_invoice_duplicate(admin):
    # A paid invoice clones into a fresh draft copying line items/total/deposit/due/
    # terms — but no payments, Stripe session, or paid status. The original and the
    # payments recorded against it are left intact.
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Ravi Shah",
            "company": "Tiffin Box",
            "email": "ravi@tiffin.test",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Lunch service shoot"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    items = '[{"label": "Half-day shoot", "qty": 1, "unit_cents": 90000}]'
    src = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents,
                    deposit_cents, due_date, terms, status, paid_at)
                    VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (
            p["id"],
            "idup-src",
            "Lunch invoice",
            items,
            90000,
            30000,
            "2026-07-01",
            "50% deposit, balance on delivery",
            "paid",
        ),
    )
    db.run(
        """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
              amount_cents, kind) VALUES (?,?,?,?,?)""",
        (src, "evt_idup", "cs_idup", 90000, "full"),
    )

    page = admin.get(f"/admin/studio/invoices/{src}")
    assert f"/admin/studio/invoices/{src}/duplicate" in page.text
    r = admin.post(f"/admin/studio/invoices/{src}/duplicate", follow_redirects=False)
    assert r.status_code == 303
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    new = db.one("SELECT * FROM invoices WHERE id=?", (new_id,))
    assert new["status"] == "draft" and new["slug"] != "idup-src"
    assert (
        new["title"] == "Lunch invoice"
        and new["line_items"] == items
        and new["total_cents"] == 90000
        and new["deposit_cents"] == 30000
        and new["due_date"] == "2026-07-01"
        and new["terms"] == "50% deposit, balance on delivery"
    )
    assert new["paid_at"] is None and new["stripe_session_id"] is None
    # the copy has no payments; the original keeps its one
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (new_id,))["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (src,))["n"] == 1

    db.run("DELETE FROM payments WHERE invoice_id=?", (src,))
    db.run("DELETE FROM invoices WHERE id IN (?,?)", (src, new_id))
    admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )


def test_contract_countersign(admin):
    # A client-signed contract can be countersigned once by the studio: typed name +
    # timestamp recorded alongside the client's, making the record bilateral. The
    # client's signature is untouched; a second countersign attempt is rejected.
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Lena Brandt",
            "company": "Copper Spoon",
            "email": "lena@copperspoon.test",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Tasting menu shoot"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    src = db.run(
        """INSERT INTO contracts (project_id, slug, title, body, body_sha256,
                    status, signer_name, signer_ip, signed_at)
                    VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
        (
            p["id"],
            "csign-src",
            "Services Agreement",
            "BODY TEXT",
            "b" * 64,
            "signed",
            "Lena Brandt",
            "5.6.7.8",
        ),
    )

    # the countersign form shows on a signed-but-not-countersigned contract
    page = admin.get(f"/admin/studio/contracts/{src}")
    assert f"/admin/studio/contracts/{src}/countersign" in page.text

    # blank name is rejected
    bad = admin.post(
        f"/admin/studio/contracts/{src}/countersign",
        data={"countersigner_name": "   "},
        follow_redirects=False,
    )
    assert bad.status_code == 400

    r = admin.post(
        f"/admin/studio/contracts/{src}/countersign",
        data={"countersigner_name": "Kevin Lee"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = db.one("SELECT * FROM contracts WHERE id=?", (src,))
    assert row["countersigner_name"] == "Kevin Lee" and row["countersigned_at"]
    assert row["status"] == "signed" and row["signer_name"] == "Lena Brandt"

    # second countersign is refused
    dup = admin.post(
        f"/admin/studio/contracts/{src}/countersign",
        data={"countersigner_name": "Kevin Lee"},
        follow_redirects=False,
    )
    assert dup.status_code == 400

    # a draft contract cannot be countersigned
    draft = db.run(
        """INSERT INTO contracts (project_id, slug, title, body, status)
                      VALUES (?,?,?,?, 'draft')""",
        (p["id"], "csign-draft", "Draft", "X"),
    )
    nope = admin.post(
        f"/admin/studio/contracts/{draft}/countersign",
        data={"countersigner_name": "Kevin Lee"},
        follow_redirects=False,
    )
    assert nope.status_code == 400

    db.run("DELETE FROM contracts WHERE id IN (?,?)", (src, draft))
    admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )


def test_admin_global_search(admin):
    # The search box is the jump-to across the admin. It must find a client by
    # business name and its project by title, and link straight to each. It must
    # also escape LIKE wildcards in the query — a bare "%" must NOT match every
    # record (that would make the box useless and leak the whole table).
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Quinella Ostrowski",
            "company": "Zarzuela Cantina",
            "email": "q@zarzuela.test",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Zarzuela tasting menu shoot"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    page = admin.get("/admin/search", params={"q": "zarzuela"}).text
    assert "Zarzuela Cantina" in page
    assert f"/admin/studio/clients/{c['id']}" in page
    assert "Zarzuela tasting menu shoot" in page
    assert f"/admin/studio/projects/{p['id']}" in page

    # Nonsense query → no matches, not a 500.
    miss = admin.get("/admin/search", params={"q": "qzxnomatchqzx"})
    assert miss.status_code == 200 and "No matches" in miss.text

    # Wildcard escape: "%" is a literal here, so the just-made client must NOT show.
    wild = admin.get("/admin/search", params={"q": "%"}).text
    assert "Zarzuela Cantina" not in wild

    admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_testimonial_self_submit(admin):
    # A client must be able to write their own testimonial via a tokened /t/{slug}
    # link, and it MUST land unpublished — the marketing site only shows moderated
    # quotes. If a self-submission published itself, an unreviewed quote could go
    # live. Also: the link is one-shot (re-POST is an idempotent thank-you, never a
    # second testimonial row).
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Marco Rossi",
            "company": "Trattoria Rossi",
            "email": "marco@trattoriarossi.com",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Dinner menu shoot"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # Admin raises the request link; the project page surfaces it.
    admin.post(
        f"/admin/studio/projects/{p['id']}/testimonial-request",
        data={"gallery_id": ""},
        follow_redirects=False,
    )
    req = db.one("SELECT * FROM testimonial_requests ORDER BY id DESC LIMIT 1")
    assert req["project_id"] == p["id"] and req["submitted_at"] is None
    page = admin.get(f"/admin/studio/projects/{p['id']}").text
    assert f"/t/{req['slug']}" in page and "awaiting client" in page

    # Client opens the form (greeted by name) and submits.
    form = admin.get(f"/t/{req['slug']}").text
    assert "Trattoria Rossi" in form and "Share your experience" in form
    r = admin.post(
        f"/t/{req['slug']}",
        data={
            "quote": "Marco's plates have never looked better.",
            "attribution_name": "Marco Rossi",
            "business": "Trattoria Rossi",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = db.one("SELECT * FROM testimonials ORDER BY id DESC LIMIT 1")
    assert t["published"] == 0  # lands unpublished for moderation
    assert t["quote"].startswith("Marco's plates")
    req = db.one("SELECT * FROM testimonial_requests WHERE id=?", (req["id"],))
    assert req["submitted_at"] and req["testimonial_id"] == t["id"]

    # Thank-you state, and a re-POST does not create a second testimonial.
    assert "your words have been received" in admin.get(f"/t/{req['slug']}").text.lower()
    n_before = db.one("SELECT COUNT(*) AS n FROM testimonials")["n"]
    admin.post(
        f"/t/{req['slug']}",
        data={"quote": "second", "attribution_name": "x", "business": ""},
        follow_redirects=False,
    )
    assert db.one("SELECT COUNT(*) AS n FROM testimonials")["n"] == n_before

    # Moderation surfacing: while it's unpublished, the admin home nudges to review
    # it and the testimonials list flags it as client-submitted. Without this the
    # self-submission has no inbox and would go unnoticed.
    assert "awaiting publish" in admin.get("/admin/home").text
    tlist = admin.get("/admin/studio/testimonials").text
    assert "from client" in tlist
    # Publishing clears the nudge (it only fires on unpublished client quotes).
    db.run("UPDATE testimonials SET published=1 WHERE id=?", (t["id"],))
    assert "awaiting publish" not in admin.get("/admin/home").text

    # Clean up so downstream order-coupled tests keep their fixtures.
    db.run("DELETE FROM testimonials WHERE id=?", (t["id"],))
    db.run("DELETE FROM testimonial_requests WHERE id=?", (req["id"],))
    admin.post(
        f"/admin/studio/clients/{c['id']}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_email_send(admin, monkeypatch):
    from app import mailer

    inv = db.one("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
    data = {
        "to": "dana@bistro.com",
        "subject": "Invoice — Spring menu shoot",
        "message": "Hi Dana, link inside.",
    }

    # not configured → 503, nothing logged
    r = admin.post(f"/admin/studio/invoices/{inv['id']}/email", data=data, follow_redirects=False)
    assert r.status_code == 503

    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))

    # drafts can't be emailed (client link would 404)
    r = admin.post(f"/admin/studio/projects/{inv['project_id']}/invoices", follow_redirects=False)
    draft = db.one("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
    r = admin.post(f"/admin/studio/invoices/{draft['id']}/email", data=data, follow_redirects=False)
    assert r.status_code == 400 and not sent

    # real send is logged with project linkage
    r = admin.post(f"/admin/studio/invoices/{inv['id']}/email", data=data, follow_redirects=False)
    assert r.status_code == 303 and len(sent) == 1
    assert sent[0][0] == "dana@bistro.com"
    e = db.one("SELECT * FROM emails_log ORDER BY id DESC LIMIT 1")
    assert (
        e["doc_kind"] == "invoice"
        and e["doc_id"] == inv["id"]
        and e["project_id"] == inv["project_id"]
        and e["to_email"] == "dana@bistro.com"
    )

    # bogus kind 404s, SMTP failure surfaces as 502 and is not logged
    assert (
        admin.post(
            f"/admin/studio/payments/{inv['id']}/email", data=data, follow_redirects=False
        ).status_code
        == 404
    )
    n_before = db.one("SELECT COUNT(*) AS n FROM emails_log")["n"]

    def boom(*a):
        raise OSError("smtp down")

    monkeypatch.setattr(mailer, "send", boom)
    r = admin.post(f"/admin/studio/invoices/{inv['id']}/email", data=data, follow_redirects=False)
    assert r.status_code == 502
    assert db.one("SELECT COUNT(*) AS n FROM emails_log")["n"] == n_before


def test_notion_sync(monkeypatch):
    from app import notion_sync

    inv = db.one("""SELECT i.* FROM invoices i WHERE i.status='paid'
                    ORDER BY i.id DESC LIMIT 1""")
    assert inv, "earlier test left a paid invoice"

    # send + webhook enqueued sync jobs
    assert (
        db.one("""SELECT COUNT(*) AS n FROM jobs
                     WHERE kind='notion_sync_invoice'""")["n"]
        >= 3
    )

    # no token / no page id → clean skip, no HTTP
    calls = []
    monkeypatch.setattr(notion_sync, "_patch_page", lambda pid, props: calls.append((pid, props)))
    notion_sync.sync_invoice(inv["id"])
    assert not calls

    # with token + page id → exact property payload (Odysseus contract)
    monkeypatch.setattr(config, "NOTION_TOKEN", "secret_test")
    db.run("UPDATE projects SET notion_page_id='abc123' WHERE id=?", (inv["project_id"],))
    notion_sync.sync_invoice(inv["id"])
    pid, props = calls[0]
    assert pid == "abc123"
    assert props == {
        "Invoice Amount": {"number": 1151.0},
        "Deposit Amount": {"number": 500.0},
        "Invoice Paid": {"checkbox": True},
        "Deposit Paid": {"checkbox": True},
    }


def test_gallery_delivery_email(admin, monkeypatch):
    from app import mailer

    gid = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        ("DeliveryMail01", "Tasting Menu", "5678"),
    )
    data = {
        "to": "owner@bistro.com",
        "subject": "Your photos are ready — Tasting Menu",
        "message": f"link {config.BASE_URL}/g/DeliveryMail01 PIN 5678",
    }

    # unpublished: no form on the page, send refused (link would 404)
    assert "Send delivery email" not in admin.get(f"/admin/galleries/{gid}").text
    assert admin.post(f"/admin/galleries/{gid}/email", data=data).status_code == 400

    db.run("UPDATE galleries SET published=1 WHERE id=?", (gid,))
    page = admin.get(f"/admin/galleries/{gid}").text
    assert "Send delivery email" in page
    assert "/g/DeliveryMail01" in page and "PIN: 5678" in page
    # "Copy link + PIN" button carries the URL + PIN as a data-copy payload
    # (newline encoded as &#10;) so a tiny inline JS can shove it into the clipboard
    assert "Copy link + PIN" in page
    assert 'data-copy="' in page
    assert "/g/DeliveryMail01&#10;PIN: 5678" in page
    assert 'class="copy-feedback' in page
    # template kind selector with 3 prefilled options
    assert 'id="email-kind"' in page
    assert 'value="delivery"' in page and 'value="proofing"' in page and 'value="final"' in page
    # each carries the prefilled subject + body in data-* attrs
    assert "Time to pick your selects" in page  # proofing subject
    assert "Final edits delivered" in page  # final subject
    # the proofing body explains the tap-heart flow
    assert "Tap the heart on each photo" in page

    # not configured → 503, nothing logged
    monkeypatch.setattr(mailer, "configured", lambda: False)
    assert admin.post(f"/admin/galleries/{gid}/email", data=data).status_code == 503

    # configured → sends and logs (doc_kind 'other' — the schema's catch-all)
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(
        mailer, "send", lambda to, subject, body, reply_to="": sent.append((to, subject, body))
    )
    r = admin.post(f"/admin/galleries/{gid}/email", data=data, follow_redirects=False)
    assert r.status_code == 303
    assert sent[0][0] == "owner@bistro.com" and "PIN 5678" in sent[0][2]
    row = db.one("SELECT * FROM emails_log WHERE doc_kind='other' AND doc_id=?", (gid,))
    assert row["to_email"] == "owner@bistro.com"

    # SMTP failure → 502, no second log row
    def boom(*a, **kw):
        raise OSError("smtp down")

    monkeypatch.setattr(mailer, "send", boom)
    assert admin.post(f"/admin/galleries/{gid}/email", data=data).status_code == 502
    assert (
        db.one("SELECT COUNT(*) AS n FROM emails_log WHERE doc_kind='other' AND doc_id=?", (gid,))[
            "n"
        ]
        == 1
    )


def test_gallery_expiry_reminder(client, monkeypatch):
    # As a published gallery nears expiry, the client gets a one-shot download
    # reminder — but only if there's a client email on file, only inside the lead
    # window, and only once. Asserted via the reminded_expiry flag so a shared-DB
    # neighbour gallery can't make this flaky.
    import datetime as dt

    from app import gallery_reminders, mailer

    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(
        mailer, "send", lambda to, subject, body, reply_to="": sent.append((to, subject, body))
    )
    today = dt.date.today()
    cid = db.run(
        "INSERT INTO clients (name, email) VALUES (?,?)", ("Expiry Co", "expiry@bistro.com")
    )
    soon = (today + dt.timedelta(days=2)).isoformat()
    far = (today + dt.timedelta(days=30)).isoformat()
    g_soon = db.run(
        "INSERT INTO galleries (slug,title,pin,type,published,client_id,expires_at)"
        " VALUES (?,?,?,'gallery',1,?,?)",
        ("ExpSoon01", "Soon Gallery", "1111", cid, soon),
    )
    g_far = db.run(
        "INSERT INTO galleries (slug,title,pin,type,published,client_id,expires_at)"
        " VALUES (?,?,?,'gallery',1,?,?)",
        ("ExpFar01", "Far Gallery", "2222", cid, far),
    )
    # only a free-text client_name, no linked client → no address → skipped
    g_orphan = db.run(
        "INSERT INTO galleries (slug,title,pin,type,published,client_name,expires_at)"
        " VALUES (?,?,?,'gallery',1,?,?)",
        ("ExpOrphan01", "Orphan", "3333", "Walk In", soon),
    )

    gallery_reminders.sweep()
    assert db.one("SELECT reminded_expiry r FROM galleries WHERE id=?", (g_soon,))["r"] == 1
    assert db.one("SELECT reminded_expiry r FROM galleries WHERE id=?", (g_far,))["r"] == 0
    assert db.one("SELECT reminded_expiry r FROM galleries WHERE id=?", (g_orphan,))["r"] == 0
    bodies = " ".join(s[2] for s in sent)
    assert "/g/ExpSoon01" in bodies and "/g/ExpFar01" not in bodies
    assert any(t[0] == "expiry@bistro.com" and "Soon Gallery" in t[1] for t in sent)

    # idempotent — a second sweep does not re-send for the already-reminded gallery
    sent.clear()
    gallery_reminders.sweep()
    assert all("/g/ExpSoon01" not in s[2] for s in sent)


def test_gallery_proofing_nudge(client, monkeypatch):
    # A published gallery that still has unmet proof targets and has been waiting
    # past the nudge threshold gets one proofing reminder; a fresh one or one with
    # no proof target does not.
    import datetime as dt

    from app import gallery_reminders, mailer

    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(
        mailer, "send", lambda to, subject, body, reply_to="": sent.append((to, subject, body))
    )
    today = dt.date.today()
    old = (today - dt.timedelta(days=10)).isoformat() + " 12:00:00"
    fresh = today.isoformat() + " 12:00:00"
    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)", ("Proof Co", "proof@bistro.com"))
    g_old = db.run(
        "INSERT INTO galleries (slug,title,pin,type,published,client_id,created_at)"
        " VALUES (?,?,?,'gallery',1,?,?)",
        ("ProofOld01", "Old Proo", "1212", cid, old),
    )
    db.run(
        "INSERT INTO sections (gallery_id,name,position,proof_target) VALUES (?,?,?,?)",
        (g_old, "Picks", 0, 2),
    )
    g_fresh = db.run(
        "INSERT INTO galleries (slug,title,pin,type,published,client_id,created_at)"
        " VALUES (?,?,?,'gallery',1,?,?)",
        ("ProofFresh01", "Fresh Proo", "1313", cid, fresh),
    )
    db.run(
        "INSERT INTO sections (gallery_id,name,position,proof_target) VALUES (?,?,?,?)",
        (g_fresh, "Picks", 0, 2),
    )
    g_noproof = db.run(
        "INSERT INTO galleries (slug,title,pin,type,published,client_id,created_at)"
        " VALUES (?,?,?,'gallery',1,?,?)",
        ("ProofNone01", "No Target", "1414", cid, old),
    )
    db.run(
        "INSERT INTO sections (gallery_id,name,position) VALUES (?,?,?)", (g_noproof, "Freeform", 0)
    )

    gallery_reminders.sweep()
    assert db.one("SELECT reminded_proofing r FROM galleries WHERE id=?", (g_old,))["r"] == 1
    assert db.one("SELECT reminded_proofing r FROM galleries WHERE id=?", (g_fresh,))["r"] == 0
    assert db.one("SELECT reminded_proofing r FROM galleries WHERE id=?", (g_noproof,))["r"] == 0
    assert any("/g/ProofOld01" in s[2] for s in sent)

    sent.clear()
    gallery_reminders.sweep()
    assert all("/g/ProofOld01" not in s[2] for s in sent)


def test_gallery_expiry_reminder_rearms_on_date_change(admin):
    # Editing a gallery's expiry date clears the one-shot flag so the new date
    # re-reminds; an edit that leaves the date unchanged must NOT clear it.
    import datetime as dt

    today = dt.date.today()
    gid = db.run(
        "INSERT INTO galleries (slug,title,pin,type,published,expires_at,reminded_expiry)"
        " VALUES (?,?,?,'gallery',1,?,1)",
        ("ReArm01", "Rearm", "4444", (today + dt.timedelta(days=20)).isoformat()),
    )
    new_exp = (today + dt.timedelta(days=40)).isoformat()
    base = {"title": "Rearm", "pin": "4444", "published": "true"}
    r = admin.post(
        f"/admin/galleries/{gid}/settings",
        data={**base, "expires_at": new_exp},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.one("SELECT reminded_expiry r FROM galleries WHERE id=?", (gid,))["r"] == 0

    db.run("UPDATE galleries SET reminded_expiry=1 WHERE id=?", (gid,))
    admin.post(
        f"/admin/galleries/{gid}/settings",
        data={**base, "expires_at": new_exp},
        follow_redirects=False,
    )
    assert db.one("SELECT reminded_expiry r FROM galleries WHERE id=?", (gid,))["r"] == 1


def test_contract_unsigned_nudge(client, monkeypatch):
    # A contract sent past the threshold and still unsigned earns ONE internal
    # Telegram nudge to Kevin — never the client. A fresh send, a signed contract,
    # and a draft are all left alone; the nudge is one-shot via nudged_unsigned.
    from app import alerts, contract_reminders

    sent = []
    monkeypatch.setattr(alerts, "is_enabled", lambda: True)
    monkeypatch.setattr(alerts, "notify", lambda text: sent.append(text))
    monkeypatch.setattr(config, "CONTRACT_NUDGE_DAYS", 3)

    cid = db.run("INSERT INTO clients (name, company) VALUES (?,?)", ("Ana", "Bistro Verde"))
    pid = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,'contract_signed')",
        (cid, "Verde Fall Menu"),
    )

    def mk(slug, status, days_ago, nudged=0):
        return db.run(
            """INSERT INTO contracts (project_id, slug, title, body, status, sent_at,
                                      nudged_unsigned)
               VALUES (?,?,?,?,?,datetime('now', ?),?)""",
            (pid, slug, "Services Agreement", "body", status, f"-{days_ago} days", nudged),
        )

    overdue = mk("CtOverdue1", "sent", 5)
    viewed = mk("CtViewed1", "viewed", 9)  # viewed-not-signed still counts
    fresh = mk("CtFresh1", "sent", 1)  # under the threshold
    signed = mk("CtSigned1", "signed", 9)  # already signed
    already = mk("CtAlready1", "sent", 9, nudged=1)  # already nudged

    contract_reminders.sweep()
    flag = lambda i: db.one("SELECT nudged_unsigned n FROM contracts WHERE id=?", (i,))["n"]
    assert flag(overdue) == 1 and flag(viewed) == 1
    assert flag(fresh) == 0 and flag(signed) == 0
    joined = " ".join(sent)
    assert "/admin/studio/contracts/" in joined and "Bistro Verde" in joined
    assert f"/admin/studio/contracts/{fresh}" not in joined
    assert len(sent) == 2  # overdue + viewed only

    # idempotent: a second sweep nudges nothing new
    sent.clear()
    contract_reminders.sweep()
    assert sent == []

    # guard: when Telegram is unconfigured the sweep no-ops and sets no flags,
    # so enabling alerts later still catches an already-overdue contract.
    monkeypatch.setattr(alerts, "is_enabled", lambda: False)
    later = mk("CtLater1", "sent", 5)
    contract_reminders.sweep()
    assert flag(later) == 0


def test_ops_monitor_heartbeat(monkeypatch, tmp_path):
    # The ops heartbeat pushes a throttled Telegram alert on low disk or a stale/
    # missing backup — the active-push twin of the Settings storage panel. Throttle
    # collapses a persistent condition to at most one alert per window.
    import time as _time

    from app import alerts, config, db, ops_monitor

    sent = []

    class _Inline:
        def __init__(self, target, args=(), **kw):
            self._t, self._a = target, args

        def start(self):
            self._t(*self._a)

    monkeypatch.setattr(alerts, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(alerts.threading, "Thread", _Inline)
    monkeypatch.setattr(alerts.config, "TELEGRAM_TOKEN", "t")
    monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    alerts._ops_last.clear()
    failed_job = db.run(
        "INSERT INTO jobs (kind, payload, status, error) VALUES (?,?,?,?)",
        ("healthcheck-test", "{}", "failed", "simulated"),
    )

    # Disk under the floor, no backup, and a failed job → distinct alerts.
    monkeypatch.setattr(config, "MIN_FREE_GB", 10**9)
    ops_monitor.sweep()
    assert any("Low disk" in s for s in sent)
    assert any("No database backup" in s for s in sent)
    assert any("background job has failed" in s for s in sent)

    # Persistent conditions are throttled → a second sweep sends nothing new.
    sent.clear()
    ops_monitor.sweep()
    assert sent == []
    db.run("DELETE FROM jobs WHERE id=?", (failed_job,))

    # disk healthy now; a backup that exists but is stale → backup_stale fires
    sent.clear()
    alerts._ops_last.clear()
    monkeypatch.setattr(config, "MIN_FREE_GB", 0)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    snap = bdir / "mise-old.db.gz"
    snap.write_bytes(b"x")
    old = _time.time() - (config.BACKUP_STALE_HOURS + 5) * 3600
    os.utime(snap, (old, old))
    ops_monitor.sweep()
    assert any("backup is" in s and "old" in s for s in sent)

    # a fresh backup → silence (positive evidence, not a swallowed error)
    sent.clear()
    alerts._ops_last.clear()
    (bdir / "mise-new.db.gz").write_bytes(b"x")
    ops_monitor.sweep()
    assert sent == []


def test_postshoot_reminder_sweep(monkeypatch):
    # A confirmed booking whose end just passed arms ONE deferred post-shoot
    # "cull & back up" owner nudge via Hermes. One-shot via armed_postshoot,
    # retries when Hermes is down, ignores old/future/cancelled shoots, and the
    # whole sweep no-ops when the reminder net isn't configured.
    import datetime as dt

    from app import db, hermes_arm, postshoot_reminders

    eid = db.run(
        """INSERT INTO event_types (slug, name, duration_min, active)
                    VALUES (?,?,?,1)""",
        ("ps-shoot", "Plated Shoot", 90),
    )

    def mk(token, end_delta_h, status="confirmed"):
        now = dt.datetime.now(dt.timezone.utc)
        start = now + dt.timedelta(hours=end_delta_h - 1)
        end = now + dt.timedelta(hours=end_delta_h)
        return db.run(
            """INSERT INTO bookings (token, event_type_id, name, email, start_utc,
                                     end_utc, status)
               VALUES (?,?,?,?,?,?,?)""",
            (
                token,
                eid,
                "Lena",
                "lena@x.com",
                start.strftime("%Y-%m-%d %H:%M:%S"),
                end.strftime("%Y-%m-%d %H:%M:%S"),
                status,
            ),
        )

    just_done = mk("PsDone1", -2)  # ended 2h ago → eligible
    future = mk("PsFuture1", 5)  # hasn't happened yet
    ancient = mk("PsAncient1", -48)  # outside the lookback window
    cancelled = mk("PsCancel1", -2, status="cancelled")

    armed = []
    monkeypatch.setattr(hermes_arm, "is_enabled", lambda: True)
    monkeypatch.setattr(hermes_arm, "arm", lambda key, text, when: armed.append(key) or True)

    postshoot_reminders.sweep()
    flag = lambda i: db.one("SELECT armed_postshoot a FROM bookings WHERE id=?", (i,))["a"]
    assert armed == [f"postshoot:{just_done}"]
    assert flag(just_done) == 1
    assert flag(future) == 0 and flag(ancient) == 0 and flag(cancelled) == 0

    # idempotent: a second sweep arms nothing new
    armed.clear()
    postshoot_reminders.sweep()
    assert armed == []

    # a down Hermes (arm returns False) leaves the flag unset → next sweep retries
    armed.clear()
    retry = mk("PsRetry1", -2)
    monkeypatch.setattr(hermes_arm, "arm", lambda key, text, when: False)
    postshoot_reminders.sweep()
    assert flag(retry) == 0
    monkeypatch.setattr(hermes_arm, "arm", lambda key, text, when: armed.append(key) or True)
    postshoot_reminders.sweep()
    assert armed == [f"postshoot:{retry}"] and flag(retry) == 1

    # net unconfigured → whole sweep no-ops, sets no flags
    armed.clear()
    dormant = mk("PsDormant1", -2)
    monkeypatch.setattr(hermes_arm, "is_enabled", lambda: False)
    postshoot_reminders.sweep()
    assert flag(dormant) == 0 and armed == []

    db.run("DELETE FROM bookings WHERE event_type_id=?", (eid,))


def test_hermes_arm_disabled_is_noop(monkeypatch):
    # The arm client is dormant unless MISE_HERMES_ARM_URL is set: no URL → arm
    # returns False and makes no HTTP call (no exception, no leak).
    from app import hermes_arm

    monkeypatch.setattr(config, "HERMES_ARM_URL", "")
    assert hermes_arm.is_enabled() is False
    assert hermes_arm.arm("k", "t", "2099-01-01T09:00:00-05:00") is False


def test_final_email_auto_advances_project(admin, monkeypatch):
    from app import mailer

    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, reply_to="": None)

    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)", ("Mara Sun", "mara@cafe.com"))
    pid = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
        (cid, "Spring shoot", "session_planning"),
    )
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, project_id, published) VALUES (?,?,?,?,1)",
        ("FinalEmail0001", "Spring shoot", "1234", pid),
    )
    data = {"to": "mara@cafe.com", "subject": "x", "message": "y"}

    # kind=delivery (default) → status unchanged
    r = admin.post(
        f"/admin/galleries/{gid}/email",
        data={**data, "email_kind": "delivery"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "session_planning"

    # kind=proofing → status unchanged (proofing is a prompt, not a hand-off)
    admin.post(
        f"/admin/galleries/{gid}/email",
        data={**data, "email_kind": "proofing"},
        follow_redirects=False,
    )
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "session_planning"

    # kind=final + project in pre-delivered state → auto-advance to 'project_closed'
    admin.post(
        f"/admin/galleries/{gid}/email",
        data={**data, "email_kind": "final"},
        follow_redirects=False,
    )
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "project_closed"

    # already-'project_closed' → no churn (idempotent re-sends are fine)
    admin.post(
        f"/admin/galleries/{gid}/email",
        data={**data, "email_kind": "final"},
        follow_redirects=False,
    )
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "project_closed"

    # 'archived' is NEVER auto-overwritten by a final-email — Kevin's archive
    # signal is intentional and should survive a re-fire of the hand-off email.
    db.run("UPDATE projects SET status='archived' WHERE id=?", (pid,))
    admin.post(
        f"/admin/galleries/{gid}/email",
        data={**data, "email_kind": "final"},
        follow_redirects=False,
    )
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "archived"

    # final email on a gallery with NO linked project → just sends, no crash
    gid2 = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("FinalNoProj001", "Loose", "1234"),
    )
    r = admin.post(
        f"/admin/galleries/{gid2}/email",
        data={**data, "email_kind": "final"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_gallery_notion_writeback(admin, monkeypatch):
    from app import notion_sync

    # Self-contained: own client + project so the test doesn't depend on one an
    # earlier test happened to leave (which failed it under -k subsets).
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Writeback Co",))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Writeback Project"))
    project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        ("WritebackSlug1", "Writeback", "1234"),
    )

    def save(published=True, project_id=project["id"]):
        return admin.post(
            f"/admin/galleries/{gid}/settings",
            data={
                "title": "Writeback",
                "pin": "1234",
                "published": "true" if published else "",
                "project_id": project_id or "",
            },
            follow_redirects=False,
        )

    def n_jobs():
        return db.one(
            """SELECT COUNT(*) AS n FROM jobs WHERE kind='notion_sync_gallery'
                         AND json_extract(payload,'$.gallery_id')=?""",
            (gid,),
        )["n"]

    # unpublished or unlinked saves enqueue nothing
    assert save(published=False).status_code == 303 and n_jobs() == 0
    assert save(project_id=None).status_code == 303 and n_jobs() == 0

    # publish flip with a project → one job; re-saving published is quiet
    save()
    assert n_jobs() == 1
    save()
    assert n_jobs() == 1

    # the job patches Gallery URL on the project's Notion session page
    calls = []
    monkeypatch.setattr(notion_sync, "_patch_page", lambda pid, props: calls.append((pid, props)))
    monkeypatch.setattr(config, "NOTION_TOKEN", "secret_test")
    db.run("UPDATE projects SET notion_page_id='sess42' WHERE id=?", (project["id"],))
    notion_sync.sync_gallery(gid)
    assert calls == [
        (
            "sess42",
            {
                "Gallery URL": {"url": f"{config.BASE_URL}/g/WritebackSlug1"},
                "Status": {"select": {"name": "Delivered"}},
            },
        )
    ]

    # delivery also arms Hermes's +Nd "did the review land?" owner check (the gap
    # Odysseus post_delivery leaves — it sends the ask but never verifies the outcome)
    armed = []
    monkeypatch.setattr(
        notion_sync.hermes_arm,
        "arm",
        lambda key, text, when: armed.append((key, text, when)) or True,
    )
    calls.clear()
    notion_sync.sync_gallery(gid)
    assert len(armed) == 1 and armed[0][0] == f"review-check:{gid}"

    # unpublishing later → clean skip, no HTTP and no arm
    calls.clear()
    armed.clear()
    db.run("UPDATE galleries SET published=0 WHERE id=?", (gid,))
    notion_sync.sync_gallery(gid)
    assert not calls and not armed

    # tidy up this test's own rows (gallery, its notion_sync jobs, project, client)
    db.run("DELETE FROM jobs WHERE json_extract(payload,'$.gallery_id')=?", (gid,))
    db.run("DELETE FROM galleries WHERE id=?", (gid,))
    db.run("DELETE FROM projects WHERE id=?", (pid,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))
