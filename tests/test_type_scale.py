"""Type scale and spacing rhythm (plan D-1, D-2): the public sheets read tokens, nothing is below 11px.

Static tests over static/screening-room-tokens.css and static/screening.css (the sheets that own the
scale), the markup rename that goes with D-1, and the mise-site.css canary: R-2 pins mise-site.css as a
verbatim subset of mise.css, so the floor has to hold there without editing it.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import css_split_lib as lib  # noqa: E402

pytestmark = pytest.mark.unit

OWNED = ("screening-room-tokens.css", "screening.css")
FLOOR_PX = 11


def sheet(name: str) -> lib.Sheet:
    return lib.Sheet(name, (ROOT / "static" / name).read_text())


def rule(name: str, prelude: str, ctx: tuple = ()) -> lib.Rule:
    hits = [
        r
        for r in sheet(name).rules
        if r.kind == "style" and " ".join(r.prelude.split()) == prelude and r.ctx == ctx
    ]
    assert len(hits) == 1, (name, prelude, len(hits))
    return hits[0]


def font_sizes_px(body: str):
    """Every px font size a rule declares (font-size and the font shorthand)."""
    for m in re.finditer(r"(?<![-\w])font-size\s*:\s*(\d+(?:\.\d+)?)px", body):
        yield float(m.group(1))
    for m in re.finditer(r"(?<![-\w])font\s*:\s*[^;}]*?\s(\d+(?:\.\d+)?)px(?=\s*/)", body):
        yield float(m.group(1))
    for m in re.finditer(r"(?<![-\w])font-size\s*:\s*(\d*\.?\d+)(?:rem|em)", body):
        yield float(m.group(1)) * 16


def small_rules(name: str):
    return [
        (r.line, " ".join(r.prelude.split())[:60], s)
        for r in sheet(name).rules
        if r.kind == "style"
        for s in font_sizes_px(r.body)
        if s < FLOOR_PX
    ]


@pytest.mark.parametrize("name", OWNED)
def test_nothing_in_the_sheets_this_slice_owns_is_below_eleven_pixels(name):
    assert not small_rules(name)


def test_mise_site_has_one_sub_eleven_rule_and_screening_overrides_it_under_sr():
    """mise-site.css is pinned as a verbatim subset of mise.css (R-2), so the floor cannot be applied
    there. The one offender is a relative size (0.62em) on the client proof badge; under body.sr the
    11px rule in screening.css wins, so only the kill-switch palette ever paints it. Reported, not edited."""
    assert [(sel, round(s, 2)) for _l, sel, s in small_rules("mise-site.css")] == [
        (".proof-progress", 9.92)
    ]
    assert "var(--sr-t-xs)" in rule("screening.css", ".sr .proof-progress").body


def test_the_scale_tokens_have_the_specified_values():
    body = rule("screening-room-tokens.css", "body.sr").body
    want = {
        "--sr-t-xs": "11px", "--sr-t-sm": "13px", "--sr-t-base": "15px", "--sr-t-lg": "17px",
        "--sr-t-xl": "20px", "--sr-d-1": "clamp(36px, 5vw, 68px)", "--sr-d-2": "clamp(30px, 3.6vw, 48px)",
        "--sr-d-3": "clamp(22px, 2.4vw, 32px)", "--sr-d-4": "20px", "--sr-track-label": "0.18em",
        "--sr-section-y": "clamp(48px, 7vw, 96px)",
    }  # fmt: skip
    for name, value in want.items():
        m = re.search(re.escape(name) + r":\s*([^;]+);", body)
        assert m and m.group(1).strip() == value, (name, m and m.group(1))


NAMED = {  # selector -> the token its declarations must read
    ".sr .sr-lede": "var(--sr-t-lg)",
    ".sr .sr-3up p": "var(--sr-t-base)",
    ".sr .sr-aerialpass-copy p": "var(--sr-t-base)",
    ".sr .faq-item p": "var(--sr-t-base)",
    ".sr .svc-tier ul li": "var(--sr-t-base)",
    ".sr .stack-form input, .sr .stack-form select, .sr .stack-form textarea": "var(--sr-t-base)",
    ".sr .sr-quote": "var(--sr-t-xl)",
    ".sr .sr-3up .sr-3up-q": "var(--sr-t-xl)",
    ".sr .sr-frameno": "var(--sr-t-sm)",
    ".sr .svc-tier .svc-usage": "var(--sr-t-sm)",
    ".sr .sr-stat small": "var(--sr-t-xs)",
    ".sr .sr-screen-title h1, .sr .sr-screen-title h2": "var(--sr-d-1)",
    ".sr .sr-titlecard-copy h1": "var(--sr-d-1)",
    ".sr .sr-herosplit-copy h1": "var(--sr-d-2)",
    ".sr .sr-featurehead h1": "var(--sr-d-2)",
    ".sr .sr-contact-h1": "var(--sr-d-2)",
    ".sr .svc-head h2": "var(--sr-d-3)",
    ".sr .sr-3up h3": "var(--sr-d-4)",
    ".sr .svc-tier h3": "var(--sr-d-4)",
    ".sr .sr-section": "var(--sr-section-y)",
    ".sr .site-footer": "var(--sr-section-y)",
    ".sr .sr-kickrow": "var(--sr-s5)",
    ".sr .sr-3up > div": "var(--sr-s6)",
    ".sr .sr-stats": "var(--sr-s6)",
    ".sr .sr-quote ": "var(--sr-s6)",
}


@pytest.mark.parametrize("selector,token", NAMED.items())
def test_named_selectors_read_the_scale(selector, token):
    assert token in rule("screening.css", selector.strip()).body


def test_section_heading_class_is_d3_and_the_title_class_is_d1():
    tokens = "screening-room-tokens.css"
    assert "var(--sr-d-3)/1.1" in rule(tokens, ".sr .sr-h2").body
    assert "var(--sr-d-1)/1.02" in rule(tokens, ".sr .sr-title").body


def test_the_four_section_h2s_use_sr_h2_and_the_orphaned_override_is_gone():
    html = (ROOT / "templates/site/specialty.html").read_text()
    assert 'class="sr-title"' not in html
    assert len(re.findall(r'<h2[^>]*class="sr-h2"', html)) == 4
    for name in ("screening.css", "screening-admin.css"):
        assert not re.search(
            r"\.sr-cta-final \.sr-title\s*\{", (ROOT / "static" / name).read_text()
        )


def test_only_portfolio_and_services_pageheads_take_the_top_display_size():
    css = (ROOT / "static/screening.css").read_text()
    for page in ("portfolio", "services"):
        assert f'a[aria-current="page"][href="/{page}"]) .sr-pagehead h1' in css
    base = rule("screening.css", ".sr .sr-pagehead h1").body
    assert "clamp(30px, 3.8vw, 54px)" in base  # every other pagehead keeps its size


def test_label_tracking_is_normalised_but_hero_eyebrows_keep_theirs():
    scr = sheet("screening.css")
    wide = {
        " ".join(r.prelude.split())
        for r in scr.rules
        if r.kind == "style"
        and re.search(r"letter-spacing:\s*0\.(?:2[1-9]|30)em", r.body)
        and "sr-mono" in r.prelude
    }
    assert not wide, wide
    assert "var(--sr-track-label)" in rule("screening-room-tokens.css", ".sr .sr-mono").body
    assert "0.34em" in rule("screening.css", ".sr .sr-pagehead .sr-mono").body


def test_nav_links_are_thirteen_pixels_only_where_the_one_line_nav_fits():
    css = (ROOT / "static/screening.css").read_text()
    assert "var(--sr-t-xs)" in rule("screening.css", ".sr .site-nav nav a").body
    assert (
        "@media (min-width: 1366px) { .sr .site-nav nav a { font-size: var(--sr-t-sm); } }" in css
    )
    assert (
        "@media (max-width: 1080px) {\n  .sr .site-nav nav { display: none; }" in css
    )  # pinned by the smoke test


def test_masonry_and_wrap_geometry_keep_their_literal_pixels():
    """tests/test_public_site.py derives the <img sizes> from these values; they are not on the scale."""
    assert "column-gap: 10px" in rule("screening.css", ".sr .portfolio-masonry").body
    assert "padding: 0 48px" in rule("screening.css", ".sr .sr-wrap").body
    assert (
        "padding: 0 16px"
        in rule("screening.css", ".sr .sr-wrap", ("@media (max-width: 700px)",)).body
    )
