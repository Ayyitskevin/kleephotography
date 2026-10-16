"""The R-2 stylesheet split: marketing loads the subset of mise.css it can use, and the admin
deck rules ride in their own sheet without changing the admin cascade.

Contract (ops/CSS-SPLIT-R2-MEASUREMENT.md, scripts/css_split_build.py):
  * site/base_site.html replaces base.html's legacy_css block, so marketing pages fetch
    mise-site.css instead of mise.css; every other surface keeps the full mise.css;
  * mise-site.css is a verbatim, ordered subset of mise.css;
  * screening.css keeps no admin-only rule from the COMMAND DECK block, and
    screening-admin.css repeats the old tail (public deck rules + shared primitives) after the
    admin rules, so last-occurrence order on admin pages is the old single-sheet order;
  * every class and id the rendered marketing goldens use still has a rule in the public sheets.
"""

import hashlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import css_split_lib as lib  # noqa: E402
import css_split_measure as measure  # noqa: E402

pytestmark = pytest.mark.unit

TAIL_MARK = "SHARED UI PRIMITIVES"
# Classes/ids the goldens carry that no stylesheet mentions: anchors, label targets and JS hooks.
# A new entry here is a decision (style it, or it is a hook); a styled one must leave the list.
UNSTYLED = {
    *{f"#contact-{x}" for x in "budget business email message name phone referral scope service".split()},
    "#contact-shoot-date", "#contact-usage", "#brandlane-heading", "#lightbox", "#rates",
    "#short-term-rentals", "#specialties", "#steady-feed-heading", "#twilight", "#work",
    "#lines-portraits", "#lines-portraits-heading", ".lb-live", ".page-hero-work", ".pf-tile",
    ".sp-steady", ".sr-mono--data",
    *{f"#line-portraits-{n}" for n in (1, 2, 3)},
    *{f"#package-{k}-{n}" for k in ("brand_partner", "photography", "portraits", "real_estate", "videography") for n in (1, 2, 3)},
    *{f"#packages-{k}" for k in ("brand_partner", "photography", "portraits", "real_estate", "videography")},
    *{f"#packages-{k}-heading" for k in ("brand_partner", "photography", "portraits", "real_estate", "videography")},
    *{f"#svc-{k}" for k in ("brand_partner", "photography", "portraits", "real_estate", "videography")},
}  # fmt: skip


def sheet(name: str) -> lib.Sheet:
    return lib.Sheet(name, (ROOT / "static" / name).read_text())


def is_admin_rule(r: lib.Rule) -> bool:
    def admin(sel: str) -> bool:
        return any(
            "sr-admin" in c.classes or "admin-shell" in c.classes for c in lib.parse_selector(sel)
        )

    return r.kind == "style" and all(admin(s) for s in r.selectors)


def test_only_the_marketing_shell_swaps_in_the_site_subset():
    users = {}
    for p in (ROOT / "templates").rglob("*.html"):
        t = p.read_text()
        if "block legacy_css" in t:
            users[str(p.relative_to(ROOT / "templates"))] = t
    assert set(users) == {"base.html", "site/base_site.html"}
    assert 'mise.css?v={{ static_rev }}") layer(mise)' in users["base.html"]
    assert 'mise-site.css?v={{ static_rev }}") layer(mise)' in users["site/base_site.html"]
    assert "/static/mise.css" not in users["site/base_site.html"]


def test_admin_shell_attaches_the_deck_sheet_exactly_once_and_nothing_else_does():
    admin = (ROOT / "templates/admin/base_admin.html").read_text()
    assert admin.count("/static/screening-admin.css?v={{ static_rev }}") == 1
    for p in (ROOT / "templates").rglob("*.html"):
        if p.name != "base_admin.html":
            assert "screening-admin.css" not in p.read_text(), p


def test_site_subset_is_a_verbatim_ordered_subset_of_mise_css():
    full = [r.sig for r in sheet("mise.css").rules]
    pos = 0
    for r in sheet("mise-site.css").rules:
        pos = full.index(r.sig, pos) + 1  # raises ValueError if missing or out of order
    assert not [r for r in sheet("mise-site.css").rules if "admin-shell" in r.prelude]


def test_screening_keeps_no_admin_only_rule_ahead_of_the_shared_primitives():
    scr = sheet("screening.css")
    cut = scr.text.index(TAIL_MARK)
    early = [r.prelude for r in scr.rules if r.start < cut and is_admin_rule(r)]
    assert not early, early[:5]


def test_admin_sheet_repeats_the_public_tail_in_order():
    scr, adm = sheet("screening.css"), sheet("screening-admin.css")
    cut = adm.text.index(TAIL_MARK)
    head = [r for r in adm.rules if r.start < cut]
    tail = [r for r in adm.rules if r.start >= cut]
    assert any(is_admin_rule(r) for r in head)
    sigs = [r.sig for r in scr.rules]
    # in the deck part, what screening.css lacks is admin-only rules and keyframes only they use
    absent = [r for r in head if r.sig not in set(sigs)]
    assert all(is_admin_rule(r) or r.prelude.startswith("@keyframes") for r in absent)
    repeated = [r.sig for r in head if r.sig in set(sigs)] + [r.sig for r in tail]
    start = sigs.index(repeated[0])
    assert sigs[start:] == repeated  # screening.css ends with exactly what the admin sheet repeats


def test_every_class_and_id_in_the_marketing_goldens_has_a_rule_or_is_a_known_hook():
    have = set()
    for name in ("mise-site.css", "screening-room-tokens.css", "screening.css"):
        for r in sheet(name).rules:
            for sel in r.selectors:
                for c in lib.parse_selector(sel):
                    have |= {"." + x for x in c.classes} | {"#" + x for x in c.ids}
    used = set()
    for p in (ROOT / "tests/golden/site").glob("*.html"):
        for n in lib.parse_html(p.read_text()):
            used |= {"." + c for c in n.classes}
            if n.id:
                used.add("#" + n.id)
    assert used, "no goldens found"
    assert used - have == UNSTYLED & used, sorted((used - have) ^ (UNSTYLED & used))
    assert not (UNSTYLED & have), sorted(UNSTYLED & have)  # a hook that got styled leaves the list


def test_no_stale_reference_to_the_old_single_sheet_deck():
    scr = (ROOT / "static/screening.css").read_text()
    assert re.search(r"moved to static/screening-admin\.css", scr)


def test_site_subset_is_complete_every_dropped_mise_rule_is_dead_on_marketing():
    """mise-site.css is exactly the rules a marketing page can use: a rule deleted from it, or a new
    marketing class that needs a legacy rule, fails here (regenerate with css_split_measure.py subset)."""
    a = measure.analyse(None)  # working tree: reachable templates, scripts, goldens
    mise, keep, matched = a["sheets"]["mise.css"], a["keep"], a["matched"]
    site = sheet("mise-site.css")
    assert [r.sig for r in mise.rules if keep.get(r.idx)] == [r.sig for r in site.rules]
    dropped = [r for r in mise.rules if not keep.get(r.idx)]
    assert dropped
    for r in dropped:  # dead: needs something no marketing template/script/string has ...
        if r.kind == "style":
            assert not any(measure.selector_satisfiable(x, a["uni"]) for x in r.selectors), (
                r.prelude
            )
            assert ("mise.css", r.idx) not in matched, (
                r.prelude
            )  # ... and matches no golden element
        else:  # an @keyframes nothing kept names
            assert not any(
                re.search(r"(?<![-\w])" + re.escape(r.prelude.split()[1]) + r"(?![-\w])", k.body)
                for k in mise.rules
                if keep.get(k.idx)
            ), r.prelude


# The admin-only COMMAND DECK rules, in order. Pinned because nothing else in the tree says what the
# split moved: dropping or editing one in screening-admin.css must be a deliberate change of this pin.
ADMIN_DECK_RULES = 259
ADMIN_DECK_SHA256 = (
    "a1bb70e2440fc577fc1a9c759a6b65e8a791022d5fbb20d219220f930299a3ee"  # pragma: allowlist secret
)


def test_admin_sheet_holds_every_admin_deck_rule_and_screening_holds_none_of_them():
    adm, scr = sheet("screening-admin.css"), sheet("screening.css")
    cut = adm.text.index(TAIL_MARK)
    deck = [r for r in adm.rules if r.start < cut and is_admin_rule(r)]
    assert len(deck) == ADMIN_DECK_RULES
    assert hashlib.sha256("\n".join(r.sig for r in deck).encode()).hexdigest() == ADMIN_DECK_SHA256
    lost = {r.sig for r in deck}
    assert not [
        r.prelude for r in scr.rules if r.sig in lost
    ]  # lost from screening.css, present here
