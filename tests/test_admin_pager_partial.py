"""The Newer/Older pager shared by the offset-paginated admin ledgers.

The route-level tests pin the pager as the two ledgers render it today, so
moving the markup into ``templates/admin/_pager.html`` is provably a rename and
not a redesign. The macro-level tests cover what a *shared* pager can quietly
break: dropping the surface's active query state (tab/range/filter/sel) on a
page link, and the boundary pages where one arrow must not render.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.render import templates

MARKER = "pager-partial-test"
# Both ledgers sort newest-first, so future timestamps pin the seeded rows to
# page 1 regardless of what other tests left in the shared session DB.
FUTURE = "2099-01-01"


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


def _pager(html: str) -> str:
    """The one <p> carrying the offset links, whitespace-normalized."""
    match = re.search(r"<p [^>]*>(?:(?!</p>).)*?offset=(?:(?!</p>).)*</p>", html, re.S)
    assert match, "no pager rendered"
    return " ".join(match.group(0).split())


def _skeleton(pager: str) -> str:
    """The pager with its hrefs blanked — what must match across surfaces."""
    return re.sub(r'href="[^"]*"', 'href="?"', pager)


def _macro():
    return templates.env.get_template("admin/_pager.html").module.pager


@pytest.fixture
def seeded_ledgers():
    """150 captured emails and 75 sent emails — both past one page size."""
    with db.tx() as con:
        gallery_id = con.execute(
            "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
            (f"{MARKER}-gallery", "Pager fixture", "0000"),
        ).lastrowid
        con.executemany(
            """INSERT INTO visitors (gallery_id, token, email, first_seen)
               VALUES (?,?,?,?)""",
            [
                (gallery_id, f"{MARKER}-{i}", f"v{i}@{MARKER}.test", f"{FUTURE} 00:00:00")
                for i in range(150)
            ],
        )
        con.executemany(
            """INSERT INTO emails_log (doc_kind, to_email, subject, created_at)
               VALUES ('other',?,?,?)""",
            [(f"s{i}@{MARKER}.test", f"Subject {i}", f"{FUTURE} 00:00:00") for i in range(75)],
        )
    try:
        yield
    finally:
        db.run("DELETE FROM visitors WHERE email LIKE ?", (f"%@{MARKER}.test",))
        db.run("DELETE FROM emails_log WHERE to_email LIKE ?", (f"%@{MARKER}.test",))
        db.run("DELETE FROM galleries WHERE slug=?", (f"{MARKER}-gallery",))


@pytest.mark.integration
def test_pager_renders_identically_on_the_ledgers_that_share_it(admin_client, seeded_ledgers):
    emails = _pager(admin_client.get("/admin/emails").text)
    sent = _pager(admin_client.get("/admin/sent").text)

    assert _skeleton(emails) == _skeleton(sent)
    # Page 1 of a full ledger: forward only, and each surface keeps its own
    # path and page size.
    assert 'href="/admin/emails?offset=100"' in emails
    assert 'href="/admin/sent?offset=50"' in sent
    assert "Newer" not in emails and "Newer" not in sent


@pytest.mark.integration
def test_pager_walks_back_from_page_two_on_both_ledgers(admin_client, seeded_ledgers):
    emails = _pager(admin_client.get("/admin/emails?offset=100").text)
    sent = _pager(admin_client.get("/admin/sent?offset=50").text)

    assert 'href="/admin/emails?offset=0"' in emails
    assert 'href="/admin/sent?offset=0"' in sent


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "page_size", "query"),
    [
        # The two live callers, then the query state the growing ledgers carry:
        # a financials range, an inbox tab + selection, a forms filter.
        ("/admin/emails", 100, ""),
        ("/admin/sent", 50, ""),
        ("/admin/financials/expenses", 50, "range=ytd"),
        ("/admin/inbox", 50, "tab=archived&sel=41"),
        ("/admin/forms/submissions", 50, "form=intake&status=new"),
    ],
)
def test_page_links_carry_the_surface_query_state(path, page_size, query):
    """A page link that drops `range`/`tab`/`sel` silently resets the filter."""
    html = _macro()(path, page_size, page_size, page_size, query)
    links = re.findall(r'href="([^"]*)"', html)

    assert len(links) == 2
    for href in links:
        assert href.startswith(f"{path}?")
        for pair in filter(None, query.split("&")):
            assert pair in href
    assert links[0].endswith("offset=0")
    assert links[1].endswith(f"offset={page_size * 2}")


@pytest.mark.unit
def test_first_page_offers_no_newer_link():
    html = _macro()("/admin/sent", 0, 50, 50)

    assert "Newer" not in html
    assert 'href="/admin/sent?offset=50"' in html


@pytest.mark.unit
def test_short_page_offers_no_older_link():
    html = _macro()("/admin/sent", 50, 50, 12)

    assert "Older" not in html
    assert 'href="/admin/sent?offset=0"' in html


@pytest.mark.unit
def test_newer_link_never_walks_past_the_first_page():
    """page_size can shrink between requests; the offset must not go negative."""
    html = _macro()("/admin/sent", 20, 50, 20)

    assert 'href="/admin/sent?offset=0"' in html
