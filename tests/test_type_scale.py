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
    # any px inside a font shorthand, with or without a line-height (r1, review P3-1 / mutant B3)
    for m in re.finditer(r"(?<![-\w])font\s*:\s*([^;}]*)", body):
        for px in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)px", m.group(1)):
            yield float(px.group(1))
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
    # r1 (review P2-1): scoped to the marketing shell; admin and client forms keep their own size
    ".site-body.sr .stack-form input, .site-body.sr .stack-form select, .site-body.sr .stack-form textarea": (
        "var(--sr-t-base)"
    ),
    ".sr .sr-quote": "var(--sr-t-xl)",
    ".sr .sr-3up .sr-3up-q": "var(--sr-t-xl)",
    ".sr .sr-frameno": "var(--sr-t-xs)",  # D-7 supersedes D-1 for frame labels
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
    # D-1's four renamed section h2s, plus D-4's case-study kicker promoted to an h2 (W3A)
    assert len(re.findall(r'<h2[^>]*class="sr-h2"', html)) == 5
    assert '<h2 class="sr-h2">{{ sp.screen_name }}, in the wild</h2>' in html
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


# --- r1: the review's P2-1, P3-1 and P3-2 (receipts/REVIEW-P2-W2A-author-sonnet55.md) ------------------------


def decl_values(body: str) -> dict:
    """property -> declared value (the last one wins, as in the cascade within one rule)."""
    out = {}
    for chunk in re.sub(r"/\*.*?\*/", "", body, flags=re.S).split(";"):
        name, sep, value = chunk.partition(":")
        if sep:
            out[name.strip().lower()] = " ".join(value.split())
    return out


SPACING = {  # D-2 rows: selector -> (property, exact declared value); the token alone is not enough (mutant F)
    ".sr .sr-section": ("padding-block", "var(--sr-section-y)"),
    ".sr .site-footer": ("margin-top", "var(--sr-section-y)"),
    ".sr .sr-kickrow": ("margin", "0 0 var(--sr-s5)"),
    ".sr .sr-3up > div": ("padding", "var(--sr-s6)"),
    ".sr .sr-stats": ("margin-top", "var(--sr-s6)"),
    ".sr .sr-quote": ("margin", "var(--sr-s6) 0"),
}


@pytest.mark.parametrize("selector,pair", SPACING.items())
def test_spacing_rows_set_the_token_on_the_specified_property(selector, pair):
    prop, value = pair
    assert decl_values(rule("screening.css", selector).body).get(prop) == value


def test_both_has_pagehead_branches_declare_the_top_display_size():
    """Each branch's effective font-size, over every rule that names it (mutant G changes one)."""
    for page in ("portfolio", "services"):
        branch = f'.site-body.sr:has(.site-nav nav a[aria-current="page"][href="/{page}"]) .sr-pagehead h1'
        sizes = [
            decl_values(r.body)["font-size"]
            for r in sheet("screening.css").rules
            if r.kind == "style"
            and branch in [" ".join(p.split()) for p in lib.split_commas(r.prelude)]
            and "font-size" in decl_values(r.body)
        ]
        assert sizes and sizes[-1] == "var(--sr-d-1)" and set(sizes) == {"var(--sr-d-1)"}, (
            page,
            sizes,
        )


def test_the_floor_scan_reads_every_px_in_a_font_shorthand():
    """P3-1: a shorthand with no line-height (the review's mutant B3) must be seen, not only `size/lh`."""
    assert list(font_sizes_px("font: 500 9px var(--sr-mono);")) == [9.0]
    assert list(font_sizes_px("font: italic 400 10.5px/1.4 var(--sr-serif);"))[:1] == [10.5]
    assert list(font_sizes_px("font: 500 var(--sr-t-xs)/1 var(--sr-mono);")) == []


TEMPLATES = ROOT / "templates"
_EXTENDS = re.compile(r'{%-?\s*extends\s+"([^"]+)"')
_INCLUDE = re.compile(r'{%-?\s*include\s+"([^"]+)"')
_BODYCLASS = re.compile(r"{%-?\s*block bodyclass\s*-?%}(.*?){%-?\s*endblock", re.S)


def _bodyclass(name: str, seen=()) -> set:
    """Every bodyclass text a template can render under: its own block, else its layout's; a partial
    with no layout takes each includer's."""
    if name in seen:
        return set()
    text = (TEMPLATES / name).read_text()
    own = _BODYCLASS.search(text)
    if own:
        return {own.group(1)}
    parent = _EXTENDS.search(text)
    if parent:
        return _bodyclass(parent.group(1), seen + (name,))
    includers = [
        p.relative_to(TEMPLATES).as_posix()
        for p in TEMPLATES.rglob("*.html")
        if name in _INCLUDE.findall(p.read_text())
    ]
    return set().union(*(_bodyclass(i, seen + (name,)) for i in includers)) if includers else {""}


def test_the_form_field_size_is_scoped_to_the_marketing_shell():
    """P2-1: `.stack-form` is also the admin and client form class. Every rule in the public sheets that
    sizes its fields must require `.site-body` (the marketing shell), and the templates that wear
    `.stack-form` without that shell are exactly the admin and client ones."""
    users = sorted(
        p.relative_to(TEMPLATES).as_posix()
        for p in TEMPLATES.rglob("*.html")
        if "stack-form" in p.read_text()
    )
    unshelled = [u for u in users if not any("site-body" in b for b in _bodyclass(u))]
    assert [u for u in users if u not in unshelled] == ["site/contact.html", "site/form.html"]
    assert len(unshelled) == 14 and all(u.startswith(("admin/", "public/")) for u in unshelled), (
        unshelled
    )
    for name in ("screening.css", "screening-admin.css", "screening-room-tokens.css"):
        for r in sheet(name).rules:
            if r.kind != "style" or "stack-form" not in r.prelude:
                continue
            if not ({"font", "font-size"} & set(decl_values(r.body))):
                continue
            for part in lib.split_commas(r.prelude):
                # the field selectors only: the submit-button rule predates W2A and is out of P2-1's scope
                if re.search(r"\.stack-form\s+(?:input|select|textarea)\b", part):
                    assert part.split()[0].startswith(".site-body"), (name, r.line, part)
