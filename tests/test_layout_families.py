"""Layout families (plan D-6): the bordered three-up stays once per page, and the other
blocks wear the family their content is.

  * `.sr-ledger`: true sequences, numbered I / II / III by a CSS counter (no text in the
    markup): the portrait process, the food & beverage courses, the about Acts.
  * `.sr-ledger.sr-ledger--plain`: the short-term-rental points, which are not a sequence,
    so the number column stays empty (ruling RULING-P2-D6-STOP.md).
  * `.sr-qa`: the portrait nerves, question beside answer.
  * `.sr-tierrow`: the steady-feed tiers, one hairline-separated row.
Every family sits in screening.css ahead of the tail screening-admin.css repeats, and
collapses to one column at 700px and below.
"""

import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler, scheduling
from app.main import app
from app.public import site
from app.public.site_catalog import SERVICES

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import css_split_lib as lib  # noqa: E402

pytestmark = pytest.mark.integration

TAIL_MARK = "moved to static/screening-admin.css"
MOBILE = ("@media (max-width: 700px)",)


@pytest.fixture(scope="module")
def client():
    patch = pytest.MonkeyPatch()
    for module in (jobs, scheduler):
        patch.setattr(module, "start", lambda: None)
        patch.setattr(module, "stop", lambda: None)
    patch.setattr(scheduling, "days_with_slots", lambda *_args: [])
    patch.setattr(site, "_avail_cache", {})

    def no_network(*_args, **_kwargs):
        raise AssertionError("external network is outside layout-family coverage")

    patch.setattr(socket.socket, "connect", no_network)
    patch.setattr(socket.socket, "connect_ex", no_network)
    patch.setattr(socket, "create_connection", no_network)
    with TestClient(app) as test_client:
        yield test_client
    patch.undo()


def block(client, route, marker, end="</section>"):
    page = client.get(route)
    assert page.status_code == 200, (route, page.status_code)
    start = page.text.index(marker)
    return page.text[start : page.text.index(end, start)]


@pytest.mark.parametrize(
    ("route", "marker"),
    [
        ("/portraits", 'aria-label="How a session runs"'),
        ("/food-beverage", 'aria-label="How a shoot runs"'),
        ("/about", 'class="ab-proc'),
    ],
    ids=["portrait-process", "fb-courses", "about-acts"],
)
def test_true_sequences_are_numbered_ledgers(client, route, marker):
    seq = block(client, route, marker)
    assert '<div class="sr-ledger">' in seq
    assert seq.count('<p class="sr-ledger-kick">') == 3
    assert "sr-3up" not in seq


def test_short_term_rentals_are_a_plain_ledger_and_twilight_keeps_the_three_up(client):
    rentals = block(client, "/real-estate", 'id="short-term-rentals"')
    assert '<div class="sr-ledger sr-ledger--plain">' in rentals  # not a sequence: no numerals
    assert rentals.count('<p class="sr-ledger-kick">') == 3
    assert "sr-3up" not in rentals
    assert '<div class="sr-3up">' in block(client, "/real-estate", 'id="twilight"')


def test_the_nerves_are_a_question_and_answer_list(client):
    nerves = block(client, "/portraits", 'aria-label="Common worries, answered"')
    assert '<div class="sr-qa">' in nerves
    assert nerves.count('<p class="sr-qa-q">') == 3
    assert "sr-3up" not in nerves


def test_the_steady_feed_tiers_are_one_tier_row(client):
    steady = block(client, "/food-beverage", 'class="sp-steady"', end="sr-kickrow--mt12")
    brand_partner = next(g for g in SERVICES if g["key"] == "brand_partner")
    assert '<div class="sr-tierrow">' in steady
    assert steady.count('<p class="sr-tierrow-name">') == len(brand_partner["tiers"])
    assert "sr-3up" not in steady


def screening():
    return lib.Sheet("screening.css", (ROOT / "static/screening.css").read_text())


def one(sheet, prelude, ctx=()):
    hits = [
        r
        for r in sheet.rules
        if r.kind == "style" and " ".join(r.prelude.split()) == prelude and r.ctx == ctx
    ]
    assert len(hits) == 1, (prelude, ctx, len(hits))
    return hits[0]


def test_the_families_read_the_row_and_sit_ahead_of_the_admin_tail():
    scr = screening()
    cut = scr.text.index(TAIL_MARK)
    row = {
        ".sr .sr-ledger > div": ("grid-template-columns: 56px 1fr", "border-top: 1px solid var(--sr-line)"),
        ".sr .sr-ledger > div::before": ("var(--sr-t-xs)", "var(--sr-mono)", "color: var(--sr-stock)"),
        ".sr .sr-qa > div": ("grid-template-columns: minmax(220px, 1fr) 2fr", "column-gap: var(--sr-s7)"),
        ".sr .sr-qa .sr-qa-q": ("var(--sr-t-lg)", "color: var(--sr-cream)"),
        ".sr .sr-qa p": ("var(--sr-t-base)", "color: var(--sr-ink-mute)"),
        ".sr .sr-tierrow": ("display: flex",),
    }  # fmt: skip
    for prelude, decls in row.items():
        r = one(scr, prelude)
        assert r.start < cut, prelude
        for decl in decls:
            assert decl in " ".join(r.body.split()), (prelude, decl)


def test_ledger_numerals_come_from_a_counter_and_the_plain_ledger_shows_none():
    scr = screening()
    assert "counter-reset: sr-ledger" in one(scr, ".sr .sr-ledger").body
    assert "counter-increment: sr-ledger" in one(scr, ".sr .sr-ledger > div").body
    assert (
        "content: counter(sr-ledger, upper-roman)" in one(scr, ".sr .sr-ledger > div::before").body
    )
    assert "content: none" in one(scr, ".sr .sr-ledger--plain > div::before").body


def test_every_family_collapses_to_one_column_at_700px():
    scr = screening()
    cut = scr.text.index(TAIL_MARK)
    one_column = {
        ".sr .sr-ledger > div": "grid-template-columns: 1fr",
        ".sr .sr-qa > div": "grid-template-columns: 1fr",
        ".sr .sr-tierrow": "flex-direction: column",
    }
    for prelude, decl in one_column.items():
        r = one(scr, prelude, MOBILE)
        assert r.start < cut and decl in r.body, prelude


def test_the_families_live_only_in_the_public_screening_sheet():
    for name in ("mise-site.css", "mise.css", "screening-admin.css"):
        text = (ROOT / "static" / name).read_text()
        # (the admin deck's own `.sr-ledger-act` badge is a different class)
        for selector in (
            ".sr-ledger ",
            ".sr-ledger--plain",
            ".sr-ledger-kick",
            ".sr-qa",
            ".sr-tierrow",
        ):
            assert selector not in text, (name, selector)
