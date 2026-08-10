"""Inbox list-window invariants: a selected thread renders exactly once.

`_inbox_ctx` bounds the thread list to 100 rows and, when the requested `sel`
falls outside that window, fetches it separately and prepends it. The prepend is
gated on the row being absent from the window — drop that gate and an in-window
selection renders twice, once prepended and once in its natural position.

These pin the row-identity contract for both sides of that gate so any future
windowing change (an offset carried on the row link, a page size move) has to
keep the selected thread single.
"""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

pytestmark = pytest.mark.integration

_MARKER = "inbox-window-once.test"


@pytest.fixture()
def admin_client():
    with TestClient(app) as c:
        r = c.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


@pytest.fixture()
def cleanup_leads():
    yield
    db.run("DELETE FROM inquiries WHERE email LIKE ?", (f"%@{_MARKER}",))


def _row_count(html: str, inquiry_id: int) -> int:
    """Occurrences of one thread's row element in the rendered list."""
    return html.count(f'id="ib-row-{inquiry_id}"')


def test_in_window_selection_renders_one_row(admin_client, cleanup_leads):
    """The common case: `sel` is already visible, so nothing may be prepended."""
    with db.tx() as con:
        target_id = con.execute(
            """INSERT INTO inquiries (name, email, message, kind, created_at)
               VALUES (?, ?, ?, 'contact', '2098-01-01 00:00:00')""",
            ("Visible Lead", f"visible@{_MARKER}", "Visible thread body"),
        ).lastrowid

    window = db.all_(
        "SELECT id FROM inquiries WHERE converted_at IS NULL AND dismissed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 100"
    )
    assert target_id in [r["id"] for r in window], "precondition: target must be in-window"

    page = admin_client.get(f"/admin/inbox?tab=all&sel={target_id}")
    assert page.status_code == 200
    assert _row_count(page.text, target_id) == 1
    assert f'href="/admin/inbox?tab=all&sel={target_id}" class="ib-row is-active"' in page.text


def test_out_of_window_selection_renders_one_row(admin_client, cleanup_leads):
    """The prepend path: `sel` is past the 100-row window and rides in once."""
    with db.tx() as con:
        target_id = con.execute(
            """INSERT INTO inquiries (name, email, message, kind, created_at)
               VALUES (?, ?, ?, 'contact', '2000-01-01 00:00:00')""",
            ("Buried Lead", f"buried@{_MARKER}", "Buried thread body"),
        ).lastrowid
        con.executemany(
            """INSERT INTO inquiries (name, email, message, kind, created_at)
               VALUES (?, ?, 'decoy body', 'contact', '2099-01-01 00:00:00')""",
            [(f"Newer Decoy {i}", f"decoy-{i}@{_MARKER}") for i in range(100)],
        )

    window = db.all_(
        "SELECT id FROM inquiries WHERE converted_at IS NULL AND dismissed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 100"
    )
    assert target_id not in [r["id"] for r in window], "precondition: target must be out-of-window"

    page = admin_client.get(f"/admin/inbox?tab=all&sel={target_id}")
    assert page.status_code == 200
    assert _row_count(page.text, target_id) == 1
    assert "Buried thread body" in page.text
    # Prepending trims the tail rather than growing the list past its bound.
    assert page.text.count('class="ib-row ') == 100
