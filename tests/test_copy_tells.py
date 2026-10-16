"""Copy tells (plan D-9): no em dash (U+2014) in what a visitor or a crawler reads on the
public site, outside a pinned allow-list.

Every route the sitemap lists is rendered under both themes (config.SCREENING_ROOM on/off)
and with the aerial kill switch both ways (config.AERIALS_LIVE), each with the golden seed,
with that seed plus motion (a reel per spoke, a second F&B still, a testimonial tied to the
case study: so the reel chip, chapters and pull quote render) and empty, in a fresh
database per render. Checked: visible text, <title>, meta description, img alt, and the FAQ
JSON-LD questions and answers.

The allow-list is exact: a whole text segment, never a prefix. It holds
  * the aerial copy (every aerials_live() branch, spec_aerial, agents_cut_aerial, the Aerial
    Pass section, the ticker's Part 107 line), which the sweep excludes until Kevin says
    otherwise;
  * the brand devices kept like the credits line (ruling RULING-P2-D9-STOP, STOP 3 (c)):
    frame, stock and reel chips, the brand chip, the F&B slate, the premiere badge;
  * stored content, which is Kevin's content-supply rather than template or catalog copy.
Each entry must still render with its dash (the positive control), so the list cannot
outlive its strings or turn into a blanket bypass. R15's three proof strings carry no em dash.
"""

import html
import json
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import config, db, jobs, scheduler, scheduling
from app.main import app
from app.public import site
from tests.test_public_render_golden import _seed

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent

DASH = "—"
SITE = config.SITE_NAME
STATES = [
    (flag, aerials, motion)
    for flag in (True, False)
    for aerials in (True, False)
    for motion in ("seeded", "motion", "empty")
]
STATE_IDS = [
    f"{'screening' if f else 'legacy'}-aerials-{'on' if a else 'off'}-{m}" for f, a, m in STATES
]

# Exact normalised segments (kind, text) allowed to keep their dash, and why.
_AERIAL = "aerial copy (plan D-9: excluded until Kevin says otherwise)"
_DEVICE = "brand device kept like the credits line (STOP 3 (c))"
_RENDER_PY = "retirement-fence follow-up: alt fallback in app/render.py (review P2-1)"
ALLOWED = {
    ("text", "Aerial coverage is planned for launch with FAA Part 107 — not available to book today."): _AERIAL,
    ("text", "Aerial Pass +$150 — FAA Part 107"): _AERIAL,
    ("text", "The Aerial Pass — new"): _AERIAL,
    (
        "text",
        "Drone stills and orbit clips flown the same visit as the interiors — acreage, roof "
        "condition, and how the property sits in its neighborhood. Cut straight into the walkthrough film.",
    ): _AERIAL,
    ("text", "FAA Part 107 certified — drone for real estate"): _AERIAL,
    ("text", "real estate — stills, film & aerials"): _AERIAL,
    ("text", "FR·0001 — Exteriors"): _DEVICE + ": frame chip",
    ("text", "FR·0002 — Headshots"): _DEVICE + ": frame chip",
    ("text", "FR·0003 — Plated"): _DEVICE + ": frame chip",
    ("text", "FR·0007 — Plated"): _DEVICE + ": frame chip",
    ("text", "800T · FR·0002 — Golden hour · Asheville"): _DEVICE + ": hero frame chip",
    ("text", "800T — Golden hour · Asheville"): _DEVICE + ": hero frame chip, no still",
    ("text", "250D — Space"): _DEVICE + ": stock chip",
    ("text", "800T — Face"): _DEVICE + ": stock chip",
    ("text", "500T — Plate"): _DEVICE + ": stock chip",
    ("text", "Stock 500T — appetite grade"): _DEVICE + ": title-card stock chip",
    ("text", "Feature 01 — The Space"): _DEVICE + ": brand chip",
    ("text", "Feature 02 — The Face"): _DEVICE + ": brand chip",
    ("text", "Feature 03 — The Plate"): _DEVICE + ": brand chip",
    ("text", "Kevin Lee presents — Food & Beverage"): _DEVICE + ": slate",
    ("text", "● Premiere — Twilight exterior · Asheville"): _DEVICE + ": premiere badge",
    ("text", "▶ 0:06 — Plated"): _DEVICE + ": reel chip (sr-filmchip), same family as the frame chips",
    (
        "text",
        "A quick call to talk through your menu, goals, and timing — no pressure, no hard sell.",
    ): "stored content: event_types.description (migration 047 seed, edited in admin)",
    **{
        (kind, "You get a clear commercial usage license for your marketing and social. Licensing terms "
               "are spelled out in your gallery and proposal — no surprises."): "ruled legal wording: the "
        "ownership answer test_usage_rights_draft pins as the baseline (Kevin's call to change)"
        for kind in ("text", "faq")
    },
    # Retirement-fence follow-up (review P2-1): the portfolio alt fallback is built in app/render.py,
    # which PHASE2-PACKET.md:8-9 fences, so these three keep the dash.
    ("alt", "Exteriors — real estate photography by Kevin Lee Photography"): _RENDER_PY,
    ("alt", "Headshots — portrait & lifestyle photography by Kevin Lee Photography"): _RENDER_PY,
    ("alt", "Plated — food & beverage photography by Kevin Lee Photography"): _RENDER_PY,
    ("title", "A focused campaign. — Kevin Lee Photography"): "case-study separator kept: the admin share "
    "preview (app/admin/share.py, retirement-excluded) builds it with the dash (STOP-2 option c)",
}  # fmt: skip


class _Segments(HTMLParser):
    """Visible text nodes, <title>, meta description, img alt and JSON-LD blobs."""

    def __init__(self):
        super().__init__()
        self.out = []
        self.stack = []
        self.ld = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name") == "description":
            self.out.append(("meta", a.get("content") or ""))
        if tag == "img" and "alt" in a:
            self.out.append(("alt", a["alt"] or ""))
        if tag not in ("meta", "img", "link", "br", "source", "input", "hr", "wbr"):
            self.stack.append((tag, a.get("type")))

    def handle_endtag(self, tag):
        if self.stack and tag not in ("meta", "img", "link", "br", "source", "input", "hr", "wbr"):
            self.stack.pop()

    def handle_data(self, data):
        if not self.stack:
            return
        tag, typ = self.stack[-1]
        if tag == "script":
            if typ == "application/ld+json":
                self.ld.append(data)
            return
        if tag == "style" or not data.strip():
            return
        in_head = any(t == "head" for t, _ in self.stack)
        if tag == "title":
            self.out.append(("title", data))
        elif not in_head:
            self.out.append(("text", data))


def _faq_texts(blob):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("@type") == "Question":
                found.append(("faq", node.get("name") or ""))
                found.append(("faq", (node.get("acceptedAnswer") or {}).get("text") or ""))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(blob))
    return found


def segments(markup):
    parser = _Segments()
    parser.feed(markup)
    out = list(parser.out)
    for blob in parser.ld:
        out += _faq_texts(blob)
    return [(kind, " ".join(text.split())) for kind, text in out]


def _seed_motion():
    """A ready 6 s portfolio reel per spoke and a testimonial tied to the seeded case study,
    so the reel chips, chapters and the spoke pull quote render too."""
    gallery = db.one("SELECT id FROM galleries WHERE slug=?", ("golden-cs",))["id"]
    for tag in ("re/exteriors", "pl/headshots", "fb/plated"):
        name = f"golden-{tag.replace('/', '-')}.mp4"
        db.run(
            """INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio,
                                   portfolio_tag, duration)
               VALUES (?,?,?,?,?,?,?,?)""",
            (gallery, "video", name, name, "ready", 1, tag, 6.0),
        )
    # A second F&B still: the appetite grid shows its reel chip after the second frame.
    db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio, portfolio_tag)
           VALUES (?,?,?,?,?,?,?)""",
        (
            gallery,
            "photo",
            "golden-fb-plated-2.jpg",
            "golden-fb-plated-2.jpg",
            "ready",
            1,
            "fb/plated",
        ),
    )
    db.run(
        """INSERT INTO testimonials (quote, attribution_name, business, published, gallery_id)
           VALUES (?,?,?,?,?)""",
        ("They booked the week it went live.", "Study client", "Golden Realty", 1, gallery),
    )


def titles(markup):
    return [text for kind, text in segments(markup) if kind == "title"]


@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    """{(flag, aerials, motion): {route: html}} for every sitemap route, fresh DB each state."""
    patch = pytest.MonkeyPatch()
    for module in (jobs, scheduler):
        patch.setattr(module, "start", lambda: None)
        patch.setattr(module, "stop", lambda: None)
    patch.setattr(scheduling, "days_with_slots", lambda *_args: [])

    def no_network(*_args, **_kwargs):
        raise AssertionError("external network is outside copy-tell coverage")

    patch.setattr(socket.socket, "connect", no_network)
    patch.setattr(socket.socket, "connect_ex", no_network)
    patch.setattr(socket, "create_connection", no_network)
    out = {}
    try:
        for flag, aerials, motion in STATES:
            data = tmp_path_factory.mktemp("copy")
            patch.setattr(config, "DATA_DIR", data)
            patch.setattr(config, "DB_PATH", data / "mise.db")
            patch.setattr(config, "SCREENING_ROOM", flag)
            patch.setattr(config, "AERIALS_LIVE", aerials)
            patch.setattr(site, "_avail_cache", {})
            db.migrate()
            db.run("DELETE FROM testimonials")
            db.run("UPDATE assets SET portfolio=0, portfolio_tag=NULL")
            db.run("UPDATE galleries SET cs_published=0")
            if motion != "empty":
                _seed()
            if motion == "motion":
                _seed_motion()
            with TestClient(app) as client:
                sitemap = client.get("/sitemap.xml")
                assert sitemap.status_code == 200
                routes = [
                    urlsplit(u).path or "/" for u in re.findall(r"<loc>([^<]+)</loc>", sitemap.text)
                ]
                routes = [
                    r for r in dict.fromkeys(routes) if not r.startswith(("/site/", "/static/"))
                ]
                assert "/" in routes and "/press" in routes, routes
                assert ("/work/golden-cs" in routes) is (motion != "empty"), routes
                pages = {}
                for route in routes:
                    response = client.get(route)
                    assert response.status_code == 200, (route, response.status_code)
                    pages[route] = response.text
                out[(flag, aerials, motion)] = pages
    finally:
        patch.undo()
    return out


@pytest.mark.parametrize("state", STATES, ids=STATE_IDS)
def test_no_em_dash_outside_the_allow_list(renders, state):
    hits = sorted(
        {
            (route, kind, text)
            for route, markup in renders[state].items()
            for kind, text in segments(markup)
            if DASH in text and (kind, text) not in ALLOWED
        }
    )
    assert not hits, "\n".join(f"{route} | {kind} | {text}" for route, kind, text in hits)


def test_every_allow_list_entry_still_renders_with_its_dash(renders):
    seen = {
        seg for pages in renders.values() for markup in pages.values() for seg in segments(markup)
    }
    for (kind, text), why in ALLOWED.items():
        assert DASH in text, (text, why)
        assert (kind, text) in seen, f"allow-list entry no longer rendered: {text!r} ({why})"


def _og_titles(markup):
    return re.findall(r'<meta property="og:title" content="([^"]*)"', markup)


@pytest.mark.parametrize("state", STATES, ids=STATE_IDS)
def test_every_title_uses_the_pipe_separator(renders, state):
    for route, markup in renders[state].items():
        for title in titles(markup) + [html.unescape(t) for t in _og_titles(markup)]:
            if ("title", title) in ALLOWED:  # the one exact separator exception
                continue
            assert title.endswith(f" | {SITE}") or title.startswith(f"{SITE} | "), (route, title)


def test_the_four_named_rewrites_read_as_the_row_lists_them(renders):
    pages = renders[(True, True, "seeded")]
    text = {
        route: " ".join(t for k, t in segments(markup) if k == "text")
        for route, markup in pages.items()
    }
    assert "Look like yourself, on your best day." in text["/portraits"]
    assert "I'm Kevin Lee: photographer and filmmaker in Asheville." in text["/about"]
    assert "shot to sell, in three features" in text["/"]
    # The inquiry confirmation renders only after a POST; its source line is the pin.
    contact = (ROOT / "templates/site/contact.html").read_text()
    assert "<h2>Thanks, your inquiry is in.</h2>" in contact


class _Options(HTMLParser):
    def __init__(self):
        super().__init__()
        self.select = None
        self.values = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select":
            self.select = a.get("id")
            self.values[self.select] = []
        elif tag == "option" and self.select:
            self.values[self.select].append(a.get("value"))

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None


def test_display_only_changes_keep_every_submitted_and_tracked_value(renders):
    """Ruling STOP 1 (a): labels lose their dash, values do not (as at the base, 6c8dedf)."""
    pages = renders[(True, True, "seeded")]
    options = _Options()
    options.feed(pages["/contact"])
    assert options.values["contact-usage"] == [
        "",
        "Extended — advertising / paid media",
        "Exclusive / talent release",
        "Not sure",
    ]
    assert options.values["contact-service"] == [
        "",
        "Real Estate",
        "Portraits",
        "Food & Beverage",
        "Brand Partner (monthly retainer)",
    ]
    fb = pages["/food-beverage"]
    for group in ("Photography", "Videography"):
        for tier in ("Starter", "Standard", "Premium"):
            joined = f"Food & Beverage — {group} — {tier}"
            assert f'data-analytics-tier="{html.escape(joined, quote=True)}"' in fb, joined
            assert "tier=" + quote(joined) + '"' in fb, joined


def test_the_share_catalog_mirrors_the_pipe_separator():
    """app/admin/share.py previews what the socials scrape from marketing_page_catalog();
    a spoke title there must match the rendered one, separator included."""
    for page in site.marketing_page_catalog():
        title = page["title"]
        assert DASH not in title, page["path"]
        assert title.endswith(f" | {SITE}") or title.startswith(f"{SITE} | "), (page["path"], title)
