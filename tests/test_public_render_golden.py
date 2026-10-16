"""Golden renders of the public marketing pages (plan R-1).

Every route renders under both themes (config.SCREENING_ROOM on/off), with a
seeded showcase and with an empty one, into a fresh database per render so the
output never depends on what other tests left behind. The normalised HTML is
compared byte-for-byte with tests/golden/site/, and each render's structure
metrics (scripts/site_metrics.py) with tests/golden/site/metrics.json.

A template or catalog change that alters a page fails here with a diff. When
the change is intended, regenerate inside the same PR and show the diff:

    GOLDEN_UPDATE=1 pytest tests/test_public_render_golden.py

An update run that changes nothing fails, so regenerating is always deliberate.
Regenerate against CURRENT main immediately before merge, not against the branch
base: a golden PR that lands after other HTML-changing merges carries stale goldens.
"""

import difflib
import importlib.util
import json
import math
import os
import re
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, jobs, scheduler, scheduling
from app.main import app
from app.public import site

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT / "tests" / "golden" / "site"
METRICS_PATH = GOLDEN_DIR / "metrics.json"
UPDATE = os.environ.get("GOLDEN_UPDATE") == "1"
MISSING = "golden missing; run with GOLDEN_UPDATE=1"
# Fixed at the fixture boundary so the case study's public datePublished is a literal.
CASE_STUDY_CREATED = "2026-01-10 12:00:00"
PRESS_OUTLET = "Golden Gazette"

ROUTES = [
    "/",
    "/real-estate",
    "/portraits",
    "/food-beverage",
    "/portfolio",
    "/work",
    "/work/golden-cs",
    "/services",
    "/about",
    "/contact",
    "/book",
    "/reels",
]
FLAGS = {"screening": True, "legacy": False}
CONTENT = ("seeded", "empty")
SEEDED_NOTE = (
    "3 ready photo assets (re/exteriors, pl/headshots, fb/plated) in case study golden-cs; "
    "1 published testimonial; 1 press row; video not seeded (only the admin upload flow "
    "creates one, see test_video_renditions_flow)"
)

_spec = importlib.util.spec_from_file_location("site_metrics", ROOT / "scripts" / "site_metrics.py")
site_metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site_metrics)


def route_slug(route):
    return "home" if route == "/" else route.strip("/").replace("/", "-")


def normalise(markup):
    """Remove only per-request and per-deploy churn: CSP nonces, static ?v= revisions,
    trailing space. Content (dates, prices, copy) is never rewritten: the seeded case
    study has a fixed created_at, so its Article datePublished is a stable literal. No
    route in the matrix renders a volatile date; if one ever does (a sitemap <lastmod>,
    say), normalise that named field only, never every YYYY-MM-DD on the page."""
    markup = re.sub(r' nonce="[^"]*"', "", markup)
    markup = re.sub(r"\?v=\d+", "?v=REV", markup)
    return "\n".join(line.rstrip() for line in markup.splitlines()) + "\n"


def compare_or_update(path, text, *, update, ledger):
    """Compare text with the golden at path, or (update) write it and count a change."""
    if update:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            ledger["changed"].append(path.name)
        return
    if not path.exists():
        pytest.fail(f"{MISSING}: {path.name}")
    expected = path.read_text(encoding="utf-8")
    if expected != text:
        diff = difflib.unified_diff(
            expected.splitlines(), text.splitlines(), "golden", "rendered", lineterm="", n=2
        )
        pytest.fail(f"{path.name} differs from its golden:\n" + "\n".join(list(diff)[:80]))


@pytest.fixture(scope="module")
def ledger():
    """Collects what an update run wrote; writes metrics.json and refuses a no-op update."""
    state = {"changed": [], "metrics": {}}
    yield state
    if not UPDATE:
        return
    current = json.loads(METRICS_PATH.read_text(encoding="utf-8")) if METRICS_PATH.exists() else {}
    merged = {**current, **state["metrics"]}
    if merged != current:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        METRICS_PATH.write_text(
            json.dumps(merged, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        state["changed"].append(METRICS_PATH.name)
    if not state["changed"]:
        pytest.fail("GOLDEN_UPDATE=1 changed nothing; run without it to compare")


def _seed():
    gallery = db.run(
        """INSERT INTO galleries
           (slug, title, pin, cs_published, cs_tagline, cs_brief, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "golden-cs",
            "Golden case study",
            "1234",
            1,
            "A focused campaign.",
            "The brief.",
            CASE_STUDY_CREATED,
        ),
    )
    for tag in ("re/exteriors", "pl/headshots", "fb/plated"):
        name = f"golden-{tag.replace('/', '-')}.jpg"
        db.run(
            """INSERT INTO assets
               (gallery_id, kind, filename, stored, status, portfolio, portfolio_tag)
               VALUES (?,?,?,?,?,?,?)""",
            (gallery, "photo", name, name, "ready", 1, tag),
        )
    db.run(
        """INSERT INTO testimonials (quote, attribution_name, business, published)
           VALUES (?,?,?,?)""",
        ("The photos did the selling for us.", "Golden client", "Golden Bistro", 1),
    )
    db.run(
        # show_on_site=1 is the public opt-in (migration 025); this temp DB only.
        "INSERT INTO press (outlet, title, url, publish_date, show_on_site) VALUES (?,?,?,?,?)",
        (PRESS_OUTLET, "A room worth the drive", "https://example.com/golden", "2026-01-15", 1),
    )


@pytest.fixture
def seeded_client(monkeypatch, tmp_path, request):
    flag, content = request.param
    # A fresh database per render: same schema as production, nothing left by other tests.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mise.db")
    db.migrate()
    # The suite's empty showcase (tests/conftest.py _strip_showcase_seed).
    db.run("DELETE FROM testimonials")
    db.run("UPDATE assets SET portfolio=0, portfolio_tag=NULL")
    db.run(
        "UPDATE galleries SET cs_published=0, cs_tagline=NULL, cs_brief=NULL, "
        "cs_credits=NULL, cs_location=NULL"
    )
    if content == "seeded":
        _seed()
    monkeypatch.setattr(config, "SCREENING_ROOM", FLAGS[flag])
    # Public rendering only, as tests/test_specialty_packages.py: no jobs, calendar or network.
    for module in (jobs, scheduler):
        monkeypatch.setattr(module, "start", lambda: None)
        monkeypatch.setattr(module, "stop", lambda: None)
    monkeypatch.setattr(scheduling, "days_with_slots", lambda *_args: [])
    monkeypatch.setattr(site, "_avail_cache", {})

    def no_network(*_args, **_kwargs):
        raise AssertionError("external network is outside golden render coverage")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with TestClient(app) as client:
        yield client


MATRIX = [(route, flag, content) for route in ROUTES for flag in FLAGS for content in CONTENT]


@pytest.mark.parametrize(
    ("route", "seeded_client"),
    [(route, (flag, content)) for route, flag, content in MATRIX],
    ids=[f"{route_slug(route)}__{flag}__{content}" for route, flag, content in MATRIX],
    indirect=["seeded_client"],
)
def test_public_page_matches_its_golden(route, seeded_client, request, ledger):
    flag, content = request.node.callspec.params["seeded_client"]
    name = f"{route_slug(route)}__{flag}__{content}"
    response = seeded_client.get(route)
    header = (
        f"<!-- golden R-1: GET {route} | screening_room={FLAGS[flag]} | content={content} | "
        f"status={response.status_code} | seeded: {SEEDED_NOTE if content == 'seeded' else 'none'} -->\n"
    )
    body = normalise(response.text)
    compare_or_update(GOLDEN_DIR / f"{name}.html", header + body, update=UPDATE, ledger=ledger)
    got = site_metrics.metrics(body)
    if UPDATE:
        ledger["metrics"][name] = got
        return
    recorded = json.loads(METRICS_PATH.read_text(encoding="utf-8")) if METRICS_PATH.exists() else {}
    if name not in recorded:
        pytest.fail(f"{MISSING}: metrics.json[{name}]")
    assert got == recorded[name], name


@pytest.mark.parametrize(
    ("route", "seeded_client"),
    [(route, (flag, content)) for route, flag, content in MATRIX],
    ids=[f"{route_slug(route)}__{flag}__{content}" for route, flag, content in MATRIX],
    indirect=["seeded_client"],
)
def test_public_page_rations_its_decorative_kickers_and_keeps_one_h1(route, seeded_client):
    """Plan D-4: at most ceil(sections/3) decorative kickers per page (data labels exempt,
    scripts/site_metrics.py decorative_kickers), and one h1 on every page that renders."""
    response = seeded_client.get(route)
    body = normalise(response.text)
    got = site_metrics.metrics(body)
    kickers = site_metrics.decorative_kickers(body)
    assert len(kickers) <= math.ceil(got["sections"] / 3), (route, got["sections"], kickers)
    assert got["h1"] == (1 if response.status_code == 200 else 0), (route, response.status_code)


def test_an_ordinary_sr_mono_inside_an_aside_is_a_counted_kicker():
    """P2-1 (W3A r1): the D-4 classifier exempts links, .sr-card-head labels and the named data
    label, never a whole tag. A plain sr-mono under <aside> is a decorative kicker like any other."""
    aside = '<aside><span class="sr-mono">Request a quote</span><h1>t</h1></aside>'
    assert site_metrics.decorative_kickers(aside) == ["Request a quote"]
    assert site_metrics.decorative_kickers(aside.replace("<aside>", "<aside class='x'>")) == ["Request a quote"]
    # the named data label stays exempt, in or out of an aside, and only that class
    assert site_metrics.decorative_kickers('<aside><p class="sr-mono sr-mono--data">Quoted separately</p></aside>') == []
    assert site_metrics.decorative_kickers('<p class="sr-mono sr-mono--data">x</p>') == []
    assert site_metrics.decorative_kickers('<aside><p class="sr-mono sr-mono--other">x</p></aside>') == ["x"]


@pytest.mark.parametrize("seeded_client", [("screening", "empty"), ("screening", "seeded")], indirect=True)
def test_three_extra_decorations_beside_request_a_quote_break_the_contact_ration(seeded_client):
    """Daybreak's counterexample (P2-1), as a test-time render: contact's own aside, three more plain
    sr-mono spans next to 'Request a quote'. The ration check must go red; contact.html is not edited."""
    body = normalise(seeded_client.get("/contact").text)
    marker = '<span class="sr-mono">Request a quote</span>'
    assert body.count(marker) == 1
    got = site_metrics.metrics(body)
    assert site_metrics.decorative_kickers(body) == ["Request a quote"]
    assert len(site_metrics.decorative_kickers(body)) <= math.ceil(got["sections"] / 3)  # the page as shipped holds
    noisy = body.replace(marker, marker + "".join(f'<span class="sr-mono">Extra {i}</span>' for i in range(3)))
    kickers = site_metrics.decorative_kickers(noisy)
    assert len(kickers) == 4
    assert len(kickers) > math.ceil(site_metrics.metrics(noisy)["sections"] / 3)


def test_a_missing_golden_names_the_update_command(tmp_path):
    ledger = {"changed": [], "metrics": {}}
    with pytest.raises(pytest.fail.Exception, match=re.escape(MISSING)):
        compare_or_update(tmp_path / "absent.html", "<p>x</p>\n", update=False, ledger=ledger)
    assert ledger["changed"] == []


def test_normalisation_removes_churn_and_keeps_content():
    page = '<script nonce="{n}"></script><link href="/static/a.css?v={v}">{body}  \n'
    body = '<time datetime="2026-01-15">2026-01-15</time><b>$450</b>'
    # Per-request nonces and per-deploy revisions normalise to the same bytes ...
    first = normalise(page.format(n="abc123", v="1729", body=body))
    assert first == normalise(page.format(n="zz9", v="1800", body=body))
    assert first == '<script></script><link href="/static/a.css?v=REV">' + body + "\n"
    # ... while a content date and a price stay distinguishable.
    for other in (body.replace("2026-01-15", "2001-02-03"), body.replace("$450", "$400")):
        assert normalise(page.format(n="abc123", v="1729", body=other)) != first


@pytest.mark.parametrize("flag", FLAGS)
def test_seeded_case_study_golden_keeps_its_published_date(flag):
    golden = (GOLDEN_DIR / f"work-golden-cs__{flag}__seeded.html").read_text(encoding="utf-8")
    assert f'"datePublished": "{CASE_STUDY_CREATED[:10]}"' in golden


@pytest.mark.parametrize("flag", FLAGS)
def test_home_goldens_carry_the_press_seed_only_when_seeded(flag):
    def home(content):
        return (GOLDEN_DIR / f"home__{flag}__{content}.html").read_text(encoding="utf-8")

    assert PRESS_OUTLET in home("seeded")
    assert PRESS_OUTLET not in home("empty")


@pytest.mark.parametrize(
    "seeded_client",
    [(flag, content) for flag in FLAGS for content in CONTENT],
    ids=[f"{flag}__{content}" for flag in FLAGS for content in CONTENT],
    indirect=True,
)
def test_home_renders_the_press_seed(seeded_client, request):
    _flag, content = request.node.callspec.params["seeded_client"]
    shown = PRESS_OUTLET in seeded_client.get("/").text
    assert shown is (content == "seeded")
