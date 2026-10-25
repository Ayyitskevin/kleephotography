"""R9a: /w/{slug} ordered by what is outstanding, rendered for real.

Each test publishes a project through the admin route, unlocks it with the PIN
the publish minted, and reads the page a client would see. The rule under test
is emphasis only: one amber primary, outstanding sections first, finished
documents collapsed to a line that still links to them — and nothing hidden,
disabled, recomputed or written along the way.
"""

import secrets
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.workspace_rows import ROWS

HEADINGS = {
    "proposal": "Proposal",
    "contract": "Agreement",
    "invoice": "Invoice",
    "gallery": "Gallery",
}
PREFIX = {"proposal": "/p/", "contract": "/c/", "invoice": "/i/", "gallery": "/g/"}
KIND_OF = {"p1": "proposal", "c1": "contract", "i1": "invoice", "g1": "gallery"}


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app import ratelimit

    ratelimit._hits.clear()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        r = c.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


class _Page(HTMLParser):
    """Just enough structure to ask: which links, which primaries, which sections."""

    def __init__(self, html: str):
        super().__init__()
        self.h1 = 0
        self.sections: list[dict] = []
        self.links: list[dict] = []
        self.primaries: list[str | None] = []
        self.spans: list[dict] = []
        self._open_a: list[dict] = []
        self._open_span: list[dict] = []
        self._in_section = False
        self._in_h2 = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "h1":
            self.h1 += 1
        elif tag == "section":
            self._in_section = True
            self.sections.append({"headings": [], "hrefs": []})
        elif tag == "h2":
            self._in_h2 = True
            if self._in_section:
                self.sections[-1]["headings"].append("")
        elif tag == "a":
            link = {"href": a.get("href"), "attrs": a, "classes": classes, "text": ""}
            self.links.append(link)
            self._open_a.append(link)
            if self._in_section and a.get("href"):
                self.sections[-1]["hrefs"].append(a["href"])
        elif tag == "span":
            span = {"classes": classes, "text": ""}
            self.spans.append(span)
            self._open_span.append(span)
        if "sr-btn" in classes and "sr-btn--ghost" not in classes:
            here = a.get("href") if tag == "a" else None
            self.primaries.append(here or (self._open_a[-1]["href"] if self._open_a else None))

    def handle_endtag(self, tag):
        if tag == "section":
            self._in_section = False
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "a" and self._open_a:
            self._open_a.pop()
        elif tag == "span" and self._open_span:
            self._open_span.pop()

    def handle_data(self, data):
        if self._in_h2 and self._in_section:
            self.sections[-1]["headings"][-1] += data.strip()
        for link in self._open_a:
            link["text"] += data
        for span in self._open_span:
            span["text"] += data

    def headings(self) -> list[str]:
        return [s["headings"][0] if s["headings"] else None for s in self.sections]

    def span_texts(self, cls: str, without: str | None = None) -> list[str]:
        return [
            " ".join(s["text"].split())
            for s in self.spans
            if cls in s["classes"] and (without is None or without not in s["classes"])
        ]

    def link_text(self, href: str) -> str:
        texts = [" ".join(link["text"].split()) for link in self.links if link["href"] == href]
        assert len(texts) == 1, f"expected one link to {href}, got {len(texts)}"
        return texts[0]


def _seed(proposal=None, contract=None, invoice=None, gallery=None):
    """One project with at most one document of each kind; returns (pid, slugs)."""
    tag = secrets.token_hex(4)
    slugs = {k: f"{k}-{tag}" for k in KIND_OF}
    cid = db.run(
        "INSERT INTO clients (name, email) VALUES (?,?)", ("WS Client", f"ws-{tag}@example.test")
    )
    gid = None
    if gallery:
        gid = db.run(
            "INSERT INTO galleries (slug, title, pin, published, expires_at) VALUES (?,?,?,1,?)",
            (slugs["g1"], f"Gallery {tag}", "4321", "2000-01-01" if gallery == "expired" else None),
        )
    pid = db.run(
        "INSERT INTO projects (client_id, title, gallery_id) VALUES (?,?,?)",
        (cid, f"WS project {tag}", gid),
    )
    if proposal:
        db.run(
            "INSERT INTO proposals (project_id, slug, title, total_cents, status) VALUES (?,?,?,?,?)",
            (pid, slugs["p1"], f"Proposal {tag}", 123456, proposal),
        )
    if contract:
        db.run(
            "INSERT INTO contracts (project_id, slug, title, body, status) VALUES (?,?,?,?,?)",
            (pid, slugs["c1"], f"Contract {tag}", "Body.", contract),
        )
    if invoice:
        status, total, deposit = invoice
        db.run(
            """INSERT INTO invoices (project_id, slug, title, total_cents, deposit_cents,
                                     due_date, status) VALUES (?,?,?,?,?,?,?)""",
            (pid, slugs["i1"], f"Invoice {tag}", total, deposit, "2026-12-01", status),
        )
    return pid, slugs


def _publish(client, pid) -> str:
    r = client.post(f"/admin/studio/projects/{pid}/workspace/publish", follow_redirects=False)
    assert r.status_code == 303
    return db.one("SELECT workspace_slug FROM projects WHERE id=?", (pid,))["workspace_slug"]


def _unlock(client, pid, ws_slug):
    pin = db.one("SELECT workspace_pin FROM projects WHERE id=?", (pid,))["workspace_pin"]
    r = client.post(f"/w/{ws_slug}/pin", data={"pin": pin}, follow_redirects=False)
    assert r.status_code == 303


def _open(client, pid) -> str:
    ws_slug = _publish(client, pid)
    _unlock(client, pid, ws_slug)
    r = client.get(f"/w/{ws_slug}")
    assert r.status_code == 200
    return r.text


def _href(slugs, key):
    return PREFIX[KIND_OF[key]] + slugs[key]


# T2 — one amber primary per combination, on the right document, outstanding first.
@pytest.mark.integration
@pytest.mark.parametrize(
    ("proposal", "contract", "invoice", "gallery", "primary", "states", "order"),
    [r[1:] for r in ROWS],
    ids=[r[0] for r in ROWS],
)
def test_one_primary_per_page(client, proposal, contract, invoice, gallery, primary, states, order):
    pid, slugs = _seed(proposal, contract, invoice, gallery)
    page = _Page(_open(client, pid))
    if primary is None:
        assert page.primaries == []
    else:
        kind, key = primary
        assert page.primaries == [PREFIX[kind] + slugs[key]]
    assert page.headings() == [HEADINGS[k] for k in order]


# T3 — drafts are private work in progress and never reach the client.
@pytest.mark.integration
def test_draft_documents_never_appear(client):
    pid, slugs = _seed(proposal="sent", contract="draft", invoice=("draft", 90000, 0))
    html = _open(client, pid)
    page = _Page(html)
    assert page.headings() == ["Proposal"]
    assert slugs["c1"] not in html
    assert slugs["i1"] not in html


# T4 — finished documents collapse to a mono line that still opens the document.
@pytest.mark.integration
@pytest.mark.parametrize(
    ("proposal", "contract", "invoice", "done"),
    [
        ("accepted", "signed", ("paid", 150000, 0), ["p1", "c1", "i1"]),
        ("declined", None, None, ["p1"]),
    ],
    ids=["accepted-signed-paid", "declined-proposal"],
)
def test_done_items_still_link_to_their_document(client, proposal, contract, invoice, done):
    pid, slugs = _seed(proposal, contract, invoice)
    page = _Page(_open(client, pid))
    for key in done:
        collapsed = [
            link
            for link in page.links
            if link["href"] == _href(slugs, key) and "sr-mono" in link["classes"]
        ]
        assert len(collapsed) == 1, f"{key} is not a collapsed linked line"
        assert slugs[key].split("-")[1] in collapsed[0]["text"]  # the document's own title


# T5 — no implied gate: an outstanding earlier step never greys out a later one.
@pytest.mark.integration
def test_later_steps_stay_linked_while_an_earlier_one_is_outstanding(client):
    pid, slugs = _seed("accepted", "sent", ("sent", 150000, 0), "live")
    page = _Page(_open(client, pid))
    assert page.primaries == [_href(slugs, "c1")]
    for key in ("i1", "g1"):
        links = [link for link in page.links if link["href"] == _href(slugs, key)]
        assert len(links) == 1, f"{key} link missing"
        attrs = links[0]["attrs"]
        for blocked in ("aria-disabled", "disabled", "hidden", "inert"):
            assert blocked not in attrs, f"{key} carries {blocked}"
        assert attrs.get("tabindex") != "-1"
        assert not any("disabled" in c for c in links[0]["classes"])
    assert page.headings() == ["Agreement", "Invoice", "Gallery", "Proposal"]


# T6 — amounts are the stored cents, never a figure recomputed for the page.
@pytest.mark.integration
def test_amounts_are_the_stored_cents(client):
    pid, _ = _seed(proposal="sent", invoice=("deposit_paid", 150000, 50000))
    html = _open(client, pid)
    assert "$1234.56" in html  # proposal total_cents
    assert "$1500.00" in html  # invoice total_cents
    assert "$1000.00" not in html  # the outstanding balance is pay.py's, not this page's
    assert "$500.00" not in html


# T7 — the PIN gate and the published switch are unchanged.
@pytest.mark.integration
def test_unpublished_404s_and_wrong_pin_shows_nothing(client):
    pid, _ = _seed(proposal="sent")
    ws_slug = _publish(client, pid)
    title = db.one("SELECT title FROM proposals WHERE project_id=?", (pid,))["title"]

    no_cookie = client.get(f"/w/{ws_slug}")
    assert no_cookie.status_code == 200 and title not in no_cookie.text
    wrong = client.post(f"/w/{ws_slug}/pin", data={"pin": "not-the-pin"}, follow_redirects=False)
    assert wrong.status_code == 401 and title not in wrong.text

    r = client.post(f"/admin/studio/projects/{pid}/workspace/unpublish", follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/w/{ws_slug}").status_code == 404


# T8 — the view is a pure read: no status, no viewed flag, no row moves.
@pytest.mark.integration
def test_view_performs_no_writes(client):
    pid, _ = _seed("sent", "sent", ("sent", 150000, 0), "live")
    ws_slug = _publish(client, pid)
    _unlock(client, pid, ws_slug)
    gid = db.one("SELECT gallery_id FROM projects WHERE id=?", (pid,))["gallery_id"]

    def snapshot():
        return {
            table: [
                tuple(r)
                for r in db.all_(f"SELECT * FROM {table} WHERE {col}=? ORDER BY id", (key,))
            ]
            for table, col, key in (
                ("proposals", "project_id", pid),
                ("contracts", "project_id", pid),
                ("invoices", "project_id", pid),
                ("projects", "id", pid),
                ("galleries", "id", gid),
            )
        }

    before = snapshot()
    assert client.get(f"/w/{ws_slug}").status_code == 200
    assert snapshot() == before


# T9 — the a11y floor: one page title, each document group a headed section.
@pytest.mark.integration
def test_page_structure(client):
    pid, _ = _seed("accepted", "signed", ("sent", 150000, 0), "live")
    page = _Page(_open(client, pid))
    assert page.h1 == 1
    assert len(page.sections) == 4
    for section in page.sections:
        assert len(section["headings"]) == 1 and section["headings"][0]


# R9a-COPY: the conductor-delegated copy picks (DELEGATED-DECISIONS-20261025, R9a
# rows) pinned as the client reads them. Each string is exact; the document's own
# title stays visible on its card.
LABELS = {
    "proposal": "Review and accept",
    "contract": "Review and sign",
    "invoice": "Review invoice",
    "gallery": "Open your gallery",
}
BADGES = {
    "proposal": "Awaiting your reply",
    "contract": "Awaiting your signature",
    "invoice": "Payment due",
}
ALL_DONE = "Nothing needs your attention right now."
EMPTY = "Nothing here yet. Your documents and gallery will appear as they're ready."


def _title(slugs, key):
    noun = {"p1": "Proposal", "c1": "Contract", "i1": "Invoice", "g1": "Gallery"}[key]
    return f"{noun} {slugs[key].split('-')[1]}"


# T10 (R9a-1..4) — the primary says what to do; the title stays on the card.
@pytest.mark.integration
@pytest.mark.parametrize(
    ("proposal", "contract", "invoice", "gallery", "primary", "states", "order"),
    [r[1:] for r in ROWS if r[5] is not None],
    ids=[r[0] for r in ROWS if r[5] is not None],
)
def test_primary_label_names_the_next_step(
    client, proposal, contract, invoice, gallery, primary, states, order
):
    pid, slugs = _seed(proposal, contract, invoice, gallery)
    page = _Page(_open(client, pid))
    kind, key = primary
    assert page.span_texts("sr-btn", without="sr-btn--ghost") == [LABELS[kind]]
    assert _title(slugs, key) in page.link_text(_href(slugs, key))


# T11 (R9a-5/6/7) — finished lines carry a capitalised human word, no dates, and
# an invoice that was never paid never reads "Paid".
@pytest.mark.integration
@pytest.mark.parametrize(
    ("seed", "key", "word"),
    [
        ({"proposal": "accepted"}, "p1", "Accepted"),
        ({"proposal": "declined"}, "p1", "Declined"),
        ({"contract": "signed"}, "c1", "Signed"),
        ({"invoice": ("paid", 150000, 50000)}, "i1", "Paid"),
        ({"invoice": ("sent", 0, 0)}, "i1", "Nothing due"),
        ({"invoice": ("viewed", 0, 0)}, "i1", "Nothing due"),
        ({"invoice": ("deposit_paid", 150000, 150000)}, "i1", "Nothing due"),
    ],
    ids=[
        "accepted",
        "declined",
        "signed",
        "paid",
        "sent-zero-invoice",
        "viewed-zero-invoice",
        "deposit-covers-total",
    ],
)
def test_done_line_wording(client, seed, key, word):
    pid, slugs = _seed(**seed)
    page = _Page(_open(client, pid))
    assert page.link_text(_href(slugs, key)) == f"{_title(slugs, key)} · {word}"


# T12 (R9a-10) — open cards say whose move it is, never the raw sent/viewed word.
@pytest.mark.integration
@pytest.mark.parametrize("status", ["sent", "viewed"])
def test_open_card_badges(client, status):
    pid, _ = _seed(status, status, (status, 150000, 0), "live")
    page = _Page(_open(client, pid))
    assert page.span_texts("kind-badge") == [
        BADGES["proposal"],
        BADGES["contract"],
        BADGES["invoice"],
    ]


# T12b (R9a-10) — one invoice badge string, also once the deposit is in.
@pytest.mark.integration
def test_invoice_badge_after_deposit(client):
    pid, _ = _seed(invoice=("deposit_paid", 150000, 50000))
    page = _Page(_open(client, pid))
    assert page.span_texts("kind-badge") == [BADGES["invoice"]]


# T13 (R9a-8) — the all-done line shows only when the page holds something and
# nothing is outstanding; a live gallery stays the primary instead.
@pytest.mark.integration
@pytest.mark.parametrize(
    ("proposal", "contract", "invoice", "gallery", "primary", "states", "order"),
    [r[1:] for r in ROWS],
    ids=[r[0] for r in ROWS],
)
def test_all_done_line(client, proposal, contract, invoice, gallery, primary, states, order):
    pid, _ = _seed(proposal, contract, invoice, gallery)
    html = _open(client, pid)
    shows = primary is None and bool(order)
    assert html.count(ALL_DONE) == (1 if shows else 0)


# T14 (R9a-9) — the expired card: no dash, a capitalised sentence, the same link.
@pytest.mark.integration
def test_expired_gallery_card_wording(client):
    pid, slugs = _seed(gallery="expired")
    html = _open(client, pid)
    page = _Page(html)
    title = _title(slugs, "g1")
    contact = f"/contact?prefill=gallery_question&gallery={title.replace(' ', '%20')}"
    assert page.link_text(contact) == "Get in touch"
    meta = page.span_texts("ws-card-meta")
    assert meta == ["Expired 2000-01-01. Get in touch to re-open it."]


# T15 (D-9) — the empty state, dash-free, and no em dash anywhere in the page body.
@pytest.mark.integration
def test_empty_state_wording(client):
    pid, _ = _seed()
    html = _open(client, pid)
    assert EMPTY in html
    assert ALL_DONE not in html


@pytest.mark.integration
def test_workspace_body_has_no_em_dash(client):
    pid, _ = _seed("declined", "sent", ("sent", 0, 0), "expired")
    html = _open(client, pid)
    main = html[html.index("<main") : html.index("</main>")]
    assert "\u2014" not in main
