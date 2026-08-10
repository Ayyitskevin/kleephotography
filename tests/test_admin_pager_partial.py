"""The Newer/Older pager shared by the offset-paginated admin ledgers.

The route-level tests pin the pager as the two ledgers render it today, so
moving the markup into ``templates/admin/_pager.html`` is provably a rename and
not a redesign. The macro-level tests cover what a *shared* pager can quietly
break: dropping the surface's active query state (tab/range/filter/sel) on a
page link, and the boundary pages where one arrow must not render.

The inbox tests pin the neighbouring invariant: the deep-link that prepends an
out-of-window `sel` must never show a thread the window already holds.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.render import ROOT, templates

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


@pytest.fixture
def crowded_inbox():
    """One old lead pushed out of the inbox window by 100 newer ones."""
    with db.tx() as con:
        target_id = con.execute(
            """INSERT INTO inquiries (name, email, message, kind, created_at)
               VALUES (?,?,?,'contact','2000-01-01 00:00:00')""",
            ("Out Of Window", f"target@{MARKER}.test", "body"),
        ).lastrowid
        con.executemany(
            """INSERT INTO inquiries (name, email, message, kind, created_at)
               VALUES (?,?,'body','contact',?)""",
            [(f"Decoy {i}", f"d{i}@{MARKER}.test", f"{FUTURE} 00:00:00") for i in range(100)],
        )
    in_window_id = db.one("SELECT id FROM inquiries WHERE email=?", (f"d50@{MARKER}.test",))["id"]
    try:
        yield target_id, in_window_id
    finally:
        db.run("DELETE FROM inquiries WHERE email LIKE ?", (f"%@{MARKER}.test",))


@pytest.mark.integration
@pytest.mark.parametrize("which", ["out_of_window", "in_window"])
def test_deep_linked_thread_renders_exactly_once(admin_client, crowded_inbox, which):
    """A thread link carries `sel`, and an out-of-window `sel` is fetched and
    prepended to the list. Prepending a thread the window already holds would
    show it twice — once at the top, once in its own place — so the window
    lookup, not the caller, has to decide.
    """
    target_id, in_window_id = crowded_inbox
    sel = target_id if which == "out_of_window" else in_window_id

    page = admin_client.get(f"/admin/inbox?tab=all&sel={sel}")

    assert page.status_code == 200
    assert page.text.count(f'id="ib-row-{sel}"') == 1
    assert page.text.count(f'href="/admin/inbox?tab=all&sel={sel}" class="ib-row is-active"') == 1
    # The prepend must not grow the window either.
    assert page.text.count('class="ib-row ') == 100


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
def test_pager_owns_a_class_and_keeps_the_kill_switch_one():
    html = _macro()("/admin/sent", 0, 50, 50)

    assert 'class="sr-pager muted"' in html


@pytest.mark.unit
def test_only_the_shared_pager_spells_out_a_page_link():
    """One pager, one place. Every offset-paginated admin surface calls the
    macro; a template that grows its own Newer/Older block is how the six
    copy-pasted ones happened, and how they drifted apart.

    inbox.html is the one holdout: its pager sits between two panes of the
    three-pane layout and carries its gutter as an inline `style`, which has no
    home in either stylesheet — screening.css is body.sr-scoped (the macro's
    own class lives there) and mise-admin.css is the kill-switch stack that
    must not learn `sr-*` names. Converting it would silently drop the gutter
    under MISE_SCREENING_ROOM=false."""
    admin_templates = ROOT / "templates" / "admin"
    hand_rolled = sorted(
        path.name
        for path in admin_templates.glob("*.html")
        if path.name != "_pager.html" and "Older &rarr;" in path.read_text()
    )

    assert hand_rolled == ["inbox.html"]


@pytest.mark.unit
def test_pager_styling_cannot_reach_the_kill_switch_stack():
    """screening.css always loads, so an unscoped .sr-pager rule would restyle
    the MISE_SCREENING_ROOM=false admin, which has no body.sr to opt in with."""
    css = (ROOT / "static" / "screening.css").read_text()
    rules = [line for line in css.splitlines() if ".sr-pager" in line]

    assert rules
    for rule in rules:
        assert rule.startswith(".sr-admin .sr-pager"), rule


@pytest.mark.unit
def test_newer_link_never_walks_past_the_first_page():
    """page_size can shrink between requests; the offset must not go negative."""
    html = _macro()("/admin/sent", 20, 50, 20)

    assert 'href="/admin/sent?offset=0"' in html


# ── the counted surfaces (`total`) ───────────────────────────────────────────
#
# The ledgers that CAN count themselves say so, and get a range line and an
# exact "is there a next page". The two email ledgers cannot — their total is
# taken before a join that can still drop rows — so they stay on the full-page
# heuristic. One macro, both answers.


@pytest.mark.unit
def test_a_counted_pager_prints_the_window_it_is_showing():
    html = _macro()("/admin/studio/press", 50, 50, 1, total=51)

    assert "Showing 51&ndash;51 of 51" in html
    assert 'href="/admin/studio/press?offset=0"' in html


@pytest.mark.unit
def test_a_counted_ledger_that_fits_one_page_renders_no_pager_at_all():
    """The hand-rolled blocks each hid themselves behind `total > page_size`;
    losing that would put a dead pager under every short ledger."""
    assert _macro()("/admin/studio/press", 0, 50, 12, total=12).strip() == ""


@pytest.mark.unit
def test_a_counted_pager_trusts_the_total_over_a_full_page():
    """The last page of a ledger that divides exactly by the page size is full.
    The uncounted heuristic has to offer Older there; a counted surface knows
    better and must not send the operator to an empty page."""
    counted = _macro()("/admin/studio/press", 50, 50, 50, total=100)
    uncounted = _macro()("/admin/emails", 50, 50, 50)

    assert "Older" not in counted
    assert "Older" in uncounted


@pytest.mark.unit
def test_a_surface_can_name_the_query_key_it_pages_on():
    """Bookings pages the past list with `past_offset` — the page also holds an
    unpaginated upcoming list, so `offset` alone would be ambiguous."""
    html = _macro()(
        "/admin/scheduling/bookings", 100, 100, 1, total=101, offset_param="past_offset"
    )

    assert 'href="/admin/scheduling/bookings?past_offset=0"' in html
    assert "?offset=" not in html


@pytest.mark.unit
def test_a_counted_pager_can_qualify_its_own_count():
    html = _macro()(
        "/admin/forms/3/submissions", 0, 50, 50, total=51, note="— search filters this page"
    )

    assert "of 51 — search filters this page" in html


@pytest.mark.unit
def test_a_query_value_carrying_an_ampersand_survives_the_page_link():
    """ "Props & supplies" is a real expense category. Spelled into the href raw
    it ends the `cat` pair early, and the filter is gone on the way back."""
    html = _macro()("/admin/financials/expenses", 0, 50, 50, "cat=Props%20%26%20supplies", total=99)

    href = re.search(r'href="([^"]*)"', html).group(1)
    assert href == "/admin/financials/expenses?cat=Props%20%26%20supplies&amp;offset=50"
