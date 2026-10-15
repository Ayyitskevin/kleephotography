"""One action colour, image frames, touch targets (plan D-5, D-7, D-10).

Static tests over static/screening-room-tokens.css + static/screening.css (the sheets that own
the look), the three specialty.html empty-state CTAs, and a tiny cascade resolver that answers
"what background does this element end up with" without a browser.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import css_split_lib as lib  # noqa: E402

pytestmark = pytest.mark.unit

STATE = re.compile(r":(hover|focus|focus-visible|active|disabled|focus-within)\b")


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


def declared(body: str, prop: str):
    """Last declaration of prop in a rule body (value, important) or None."""
    found = None
    for chunk in body.split(";"):
        name, _, value = chunk.partition(":")
        if name.strip().lower() == prop:
            found = (" ".join(value.split()), "!important" in value)
    return found


def resolved(markup: str, prop: str) -> str:
    """The winning value of prop on the element with id=x: specificity, then source order.
    State selectors (hover, focus, disabled) are skipped: this is the resting style."""
    nodes = lib.parse_html(markup)
    node = next(n for n in nodes if n.id == "x")
    best, best_key = None, None
    order = 0
    for name in ("screening-room-tokens.css", "screening.css"):
        for r in sheet(name).rules:
            if r.kind != "style" or any("print" in c for c in r.ctx):
                continue
            hit = declared(r.body, prop)
            for sel in r.selectors:
                order += 1
                if not hit or STATE.search(sel) or "::" in sel or r.ctx:
                    continue
                if lib.match_selector(node, sel, set()):
                    key = (hit[1], lib.specificity(sel), order)
                    if best_key is None or key >= best_key:
                        best, best_key = hit[0], key
    return best


def page(inner: str, body_class: str = "site-body sr") -> str:
    return f'<html><body class="{body_class}">{inner}</body></html>'


# ── D-5 ──────────────────────────────────────────────────────────────────────
ACTION_BACKGROUNDS = {
    ".sr-btn": page('<a id="x" class="sr-btn" href="/">Book</a>'),
    ".sr-btn.btn-pill": page('<a id="x" class="sr-btn btn-pill" href="/">Book</a>'),
    ".btn-gold": page('<a id="x" class="btn-gold" href="/">Book</a>'),
    ".nav-cta (desktop)": page(
        '<header class="site-nav"><nav><a id="x" class="nav-cta" href="/book">Book</a></nav></header>'
    ),
    ".nav-cta (mobile overlay)": page(
        '<div class="nav-mobile"><a id="x" class="nav-cta" href="/">Book</a></div>'
    ),
    ".sticky-cta .btn-gold": page(
        '<div class="sticky-cta"><a id="x" class="sr-btn btn-gold" href="/">Book</a></div>'
    ),
    "form submit": page(
        '<form class="stack-form"><button id="x" type="submit">Send</button></form>'
    ),
}


@pytest.mark.parametrize("label", ACTION_BACKGROUNDS)
@pytest.mark.parametrize("stock", ["", " sr-re", " sr-pl", " sr-fb"])
def test_every_action_background_resolves_to_amber_on_every_spoke(label, stock):
    markup = ACTION_BACKGROUNDS[label].replace(
        'class="site-body sr"', f'class="site-body sr{stock}"'
    )
    assert resolved(markup, "background") == "var(--sr-amber)", label


def test_the_service_cta_is_amber_in_text_and_border():
    markup = page('<div class="svc-tier"><a id="x" class="svc-cta" href="/">Pick</a></div>')
    assert resolved(markup, "color") == "var(--sr-amber)"
    assert "var(--sr-amber)" in resolved(markup, "border") and "stock" not in resolved(
        markup, "border"
    )


@pytest.mark.parametrize(
    "selector,prop",
    [
        (".sr .sr-btn--ghost:hover", "border-color"),
        (".sr .sr-btn--ghost:hover", "color"),
        (".sr .btn-ghost:hover", "border-color"),
        (".sr .btn-ghost:hover", "color"),
        (".sr .sr-btn:disabled:hover", "background"),
    ],
)
def test_action_hover_states_are_amber_too(selector, prop):
    name = "screening-room-tokens.css" if "sr-btn--ghost" in selector else "screening.css"
    assert declared(rule(name, selector).body, prop)[0] == "var(--sr-amber)"


def test_stock_is_still_the_identity_colour_on_chips_cards_and_the_brand():
    scr = (ROOT / "static/screening.css").read_text()
    for needle in (
        ".sr .site-brand small",
        ".sr .pf-chip:hover { border-color: var(--sr-stock)",
        ".sr a.sr-card:hover, .sr a.sr-card:focus-visible { border-color: var(--sr-stock)",
        ".sr .sr-stat--hot small { color: var(--sr-stock)",
    ):
        assert needle in scr, needle
    assert "color: var(--sr-stock)" in (ROOT / "static/screening-room-tokens.css").read_text()


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_house_text_on_amber_clears_eight_to_one():
    tokens = rule("screening-room-tokens.css", "body.sr").body
    amber = re.search(r"--sr-amber:\s*(#[0-9a-fA-F]{6})", tokens).group(1)
    house = re.search(r"--sr-house:\s*(#[0-9a-fA-F]{6})", tokens).group(1)
    la, lh = _luminance(amber), _luminance(house)
    assert (la + 0.05) / (lh + 0.05) >= 8.3


def test_the_three_empty_state_ctas_are_square_sr_buttons_and_the_other_two_are_untouched():
    spec = (ROOT / "templates/site/specialty.html").read_text()
    assert "btn-pill" not in spec
    assert (
        spec.count(
            '<a class="sr-btn" href="{{ book_url }}">{{ page.sr.cta }} <span>&rarr;</span></a>'
        )
        == 3
    )
    for other in ("templates/site/_empty_portfolio.html", "templates/site/work_detail.html"):
        assert "btn-pill" in (ROOT / other).read_text()  # not in the D-5 row


# ── D-7 ──────────────────────────────────────────────────────────────────────
ALLOWED_RATIOS = {"3 / 2", "4 / 5", "16 / 9", "21 / 9", "9 / 16", "auto"}


def test_image_ratios_are_the_specified_set_and_the_tall_frame_is_four_by_five():
    ratios = set()
    for r in sheet("screening.css").rules:
        if r.kind == "style":
            for m in re.finditer(r"aspect-ratio\s*:\s*([^;}]+)", r.body):
                ratios.add(" ".join(m.group(1).split()))
    assert ratios <= ALLOWED_RATIOS, ratios - ALLOWED_RATIOS
    assert (
        "aspect-ratio: 4 / 5"
        in rule("screening.css", ".sr .sr-filmstrip figure.sr-frame-tall").body
    )


def test_marketing_tiles_carry_the_frame_outline():
    body = rule("screening.css", ".site-body.sr .tile").body
    assert "outline: 1px solid rgba(236, 228, 211, 0.08)" in body
    assert "outline-offset: -1px" in body


def test_tile_images_ease_on_the_specified_curve_and_stop_under_reduced_motion():
    img = rule("screening.css", ".sr .portfolio-masonry .tile img").body
    assert "transition: transform 700ms cubic-bezier(0.32, 0.72, 0, 1)" in img
    assert (
        "transform: scale(1.03)"
        in rule("screening.css", ".sr .portfolio-masonry .tile:hover img").body
    )
    off = rule(
        "screening.css",
        ".sr .sr-filmstrip figure img, .sr a.sr-card .sr-card-media img, .sr .portfolio-masonry .tile img",
        ("@media (prefers-reduced-motion: reduce)",),
    )
    assert "transition: none" in off.body


def test_frame_numbers_use_the_label_size_and_wider_tracking_but_keep_cream_and_the_amber_hover():
    body = rule("screening.css", ".sr .sr-frameno").body
    assert "var(--sr-t-xs)" in body and "letter-spacing: 0.08em" in body
    assert "rgba(236, 228, 211, 0.7)" in body
    assert (
        "var(--sr-amber)"
        in rule("screening.css", ".sr .sr-filmstrip figure:hover .sr-frameno").body
    )


def test_the_portrait_hero_still_crops_above_centre():
    assert "object-position: center 40%" in rule("screening.css", ".sr .sr-herosplit-fig img").body


# ── D-10 ─────────────────────────────────────────────────────────────────────
def test_buttons_and_inline_links_in_kickrows_and_availstrips_are_forty_four_tall():
    assert (
        resolved(page('<a id="x" class="sr-btn sr-btn--sm" href="/">x</a>'), "min-height") == "44px"
    )
    body = rule("screening.css", ".sr .sr-kickrow a, .sr .sr-availstrip a").body
    for want in ("display: inline-flex", "min-height: 44px", "align-items: center"):
        assert want in body


def test_nav_links_pad_twelve_pixels_vertically_and_the_cta_keeps_its_own_padding():
    assert "padding: var(--sr-s3) 0" in rule("screening.css", ".sr .site-nav nav a").body
    assert "padding: 11px var(--sr-s4)" in rule("screening.css", ".sr .site-nav nav a.nav-cta").body


TEMPLATES = ROOT / "templates"


def test_the_touch_target_floor_stops_at_the_marketing_shell():
    """P2-1: `.sr-btn` is also the admin and client button (the admin's compact `sr-btn--sm` among them).
    The 44px floor is a marketing-shell rule, so no rule in any public sheet may give `.sr-btn` a
    min-height without requiring `.site-body`, and the resting style on an admin or flow page has none."""
    for name in ("screening-room-tokens.css", "screening.css", "screening-admin.css"):
        for r in sheet(name).rules:
            if r.kind != "style" or "min-height" not in r.body:
                continue
            for part in lib.split_commas(r.prelude):
                if re.search(r"\.sr-btn\b(?!-)", part):
                    assert part.split()[0].startswith(".site-body"), (name, r.line, part)
    shell = page('<a id="x" class="sr-btn" href="/">Book</a>')
    assert resolved(shell, "min-height") == "44px"
    for body in ("sr sr-admin", "sr"):  # admin shell, booking/client flow shell
        for classes in ("sr-btn sr-btn--sm", "sr-btn"):
            assert (
                resolved(page(f'<a id="x" class="{classes}" href="/">x</a>', body), "min-height")
                is None
            )


def test_the_small_button_users_are_all_admin_templates():
    users = sorted(
        p.relative_to(TEMPLATES).as_posix()
        for p in TEMPLATES.rglob("*.html")
        if "sr-btn--sm" in p.read_text()
    )
    assert users and all(u.startswith("admin/") for u in users), users
