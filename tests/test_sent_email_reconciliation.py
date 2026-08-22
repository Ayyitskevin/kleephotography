"""Reconcile "marked sent" against "was it ever emailed" (ENHANCEMENT-BRIEF G3).

"Mark sent" locks a doc and makes it visible at the client link; "Email to
client" is a separate act that logs into emails_log. Nothing used to join the
two, so a proposal could sit `sent` that no client ever received — and it
cannot convert. These tests pin the surfacing (display-only, never a send):

- the doc detail pages flag `sent` docs with no emails_log row
  (`data-no-send-recorded`), and clear the flag once a send is recorded or the
  client opens the link (status leaves `sent`);
- Home nudges the same discrepancy after a day of grace, keyed
  `no_send:<kind>:<id>`, and the snooze endpoint accepts the key.

The wording is "no send recorded", never "never sent": emails_log only knows
what MISE sent — a copy mailed from Kevin's own client leaves no row.
"""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

BADGE = "data-no-send-recorded"


@pytest.fixture
def admin_client():
    with TestClient(app) as client:
        response = client.post(
            "/admin/login",
            data={"password": "test-pw"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield client


@pytest.fixture
def studio_docs():
    """One project whose proposal, contract and invoice are all marked `sent`
    long ago with no emails_log row — the exact discrepancy G3 surfaces. The
    age is deliberately extreme so the rows survive the Home nudge's
    ORDER BY sent_at … LIMIT against whatever else the shared suite DB holds."""
    client_id = db.run("INSERT INTO clients (name) VALUES (?)", ("NoSend Diner",))
    project_id = db.run(
        "INSERT INTO projects (client_id, title) VALUES (?,?)", (client_id, "NoSend Shoot")
    )
    prop_id = db.run(
        """INSERT INTO proposals (project_id, slug, title, status, sent_at)
           VALUES (?,?,?, 'sent', datetime('now', '-600 days'))""",
        (project_id, "g3-nosend-prop", "NoSend proposal"),
    )
    ct_id = db.run(
        """INSERT INTO contracts (project_id, slug, title, body, status, sent_at)
           VALUES (?,?,?,?, 'sent', datetime('now', '-600 days'))""",
        (project_id, "g3-nosend-ct", "NoSend contract", "Agreement body."),
    )
    inv_id = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status, sent_at)
           VALUES (?,?,?,?, 'sent', datetime('now', '-600 days'))""",
        (project_id, "g3-nosend-inv", "NoSend invoice", 50_000),
    )
    try:
        yield {
            "project_id": project_id,
            "proposal": prop_id,
            "contract": ct_id,
            "invoice": inv_id,
        }
    finally:
        db.run("DELETE FROM dismissed_nudges WHERE nudge_key LIKE 'no_send:%'")
        db.run("DELETE FROM emails_log WHERE project_id=?", (project_id,))
        db.run("DELETE FROM proposals WHERE id=?", (prop_id,))
        db.run("DELETE FROM contracts WHERE id=?", (ct_id,))
        db.run("DELETE FROM invoices WHERE id=?", (inv_id,))
        db.run("DELETE FROM projects WHERE id=?", (project_id,))
        db.run("DELETE FROM clients WHERE id=?", (client_id,))


def _doc_pages(docs) -> list[str]:
    return [
        f"/admin/studio/proposals/{docs['proposal']}",
        f"/admin/studio/contracts/{docs['contract']}",
        f"/admin/studio/invoices/{docs['invoice']}",
    ]


def _record_send(docs, kind: str) -> None:
    """The row app.admin.emails.email_doc writes after a successful SMTP send."""
    db.run(
        """INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, subject)
           VALUES (?,?,?,?,?)""",
        (docs["project_id"], kind, docs[kind], "nosend@example.com", f"Your {kind}"),
    )


# --- doc detail pages --------------------------------------------------------


@pytest.mark.integration
def test_doc_pages_flag_a_sent_doc_with_no_recorded_email(admin_client, studio_docs):
    """All three doc kinds: marked sent, never emailed from Mise → the page
    says so. The hook is the pinned markup the dashboards and this suite share."""
    for url in _doc_pages(studio_docs):
        page = admin_client.get(url)
        assert page.status_code == 200, url
        assert BADGE in page.text, f"{url} hides the discrepancy"
        assert "no send recorded" in page.text, url


@pytest.mark.integration
def test_the_flag_clears_once_a_send_is_recorded(admin_client, studio_docs):
    """Recording the email (the emails_log row the send endpoint writes) is
    exactly what reconciles the doc — the flag must drop, not linger."""
    for kind in ("proposal", "contract", "invoice"):
        _record_send(studio_docs, kind)
    for url in _doc_pages(studio_docs):
        assert BADGE not in admin_client.get(url).text, url


@pytest.mark.integration
def test_a_viewed_doc_is_not_flagged(admin_client, studio_docs):
    """`viewed` is set only by the client opening the link (public/docs.py), so
    a viewed doc demonstrably reached the client even with no emails_log row —
    flagging it would be a false alarm."""
    db.run(
        "UPDATE proposals SET status='viewed', viewed_at=datetime('now') WHERE id=?",
        (studio_docs["proposal"],),
    )
    page = admin_client.get(f"/admin/studio/proposals/{studio_docs['proposal']}")
    assert BADGE not in page.text


@pytest.mark.integration
def test_a_draft_doc_is_not_flagged(admin_client, studio_docs):
    """A draft was never marked sent — there is no discrepancy to surface."""
    db.run(
        "UPDATE proposals SET status='draft', sent_at=NULL WHERE id=?",
        (studio_docs["proposal"],),
    )
    page = admin_client.get(f"/admin/studio/proposals/{studio_docs['proposal']}")
    assert BADGE not in page.text


# --- Home nudge --------------------------------------------------------------


def _no_send_keys(ctx) -> set[str]:
    return {n["key"] for n in ctx["next_steps"] if n["key"].startswith("no_send:")}


@pytest.mark.integration
def test_home_nudges_docs_marked_sent_but_never_emailed(admin_client, studio_docs):
    """All three kinds reach the Needs-you list, honestly worded and linking to
    the doc page where the email form lives."""
    ctx = admin_client.get("/admin/home").context
    keys = _no_send_keys(ctx)
    assert f"no_send:proposal:{studio_docs['proposal']}" in keys
    assert f"no_send:contract:{studio_docs['contract']}" in keys
    assert f"no_send:invoice:{studio_docs['invoice']}" in keys

    nudge = next(
        n for n in ctx["next_steps"] if n["key"] == f"no_send:proposal:{studio_docs['proposal']}"
    )
    assert nudge["tone"] == "warn"
    assert "No send recorded" in nudge["text"]
    assert "NoSend proposal" in nudge["text"]
    assert nudge["url"] == f"/admin/studio/proposals/{studio_docs['proposal']}"


@pytest.mark.integration
def test_home_reconciles_against_the_send_log_and_the_view(admin_client, studio_docs):
    """A recorded send OR a client view clears the nudge — it flags only docs
    with neither, or it is noise."""
    _record_send(studio_docs, "proposal")
    db.run(
        "UPDATE contracts SET status='viewed', viewed_at=datetime('now') WHERE id=?",
        (studio_docs["contract"],),
    )
    keys = _no_send_keys(admin_client.get("/admin/home").context)
    assert f"no_send:proposal:{studio_docs['proposal']}" not in keys, "emailed — reconciled"
    assert f"no_send:contract:{studio_docs['contract']}" not in keys, "viewed — client has it"
    assert f"no_send:invoice:{studio_docs['invoice']}" in keys, "still unreconciled"


@pytest.mark.integration
def test_a_fresh_sent_mark_gets_a_day_of_grace_on_home_but_not_on_the_doc(
    admin_client, studio_docs
):
    """The normal flow is mark-sent then email minutes later from the same
    page: Home must not nag inside that window. The doc page — where the email
    form sits — flags immediately, so the state is never actually hidden."""
    db.run("UPDATE invoices SET sent_at=datetime('now') WHERE id=?", (studio_docs["invoice"],))
    keys = _no_send_keys(admin_client.get("/admin/home").context)
    assert f"no_send:invoice:{studio_docs['invoice']}" not in keys
    page = admin_client.get(f"/admin/studio/invoices/{studio_docs['invoice']}")
    assert BADGE in page.text


@pytest.mark.integration
def test_the_snooze_endpoint_accepts_the_no_send_key(admin_client, studio_docs):
    """The nudge is dismissible like every other — the allowlist knows the new
    prefix, the dismissal clears just that doc for today."""
    key = f"no_send:proposal:{studio_docs['proposal']}"
    response = admin_client.post(
        "/admin/home/nudge/dismiss", data={"key": key}, follow_redirects=False
    )
    assert response.status_code == 303
    keys = _no_send_keys(admin_client.get("/admin/home").context)
    assert key not in keys
    assert f"no_send:invoice:{studio_docs['invoice']}" in keys, "only the dismissed doc clears"
