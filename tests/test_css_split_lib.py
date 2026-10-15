"""The CSS toolkit behind the R-2 split (scripts/css_split_lib.py).

Its tokenizer once looped without advancing on a `[attr]` selector and grew
until the kernel OOM-killed it. These tests pin the progress guard and the
bracket/paren termination that caused it, and the parts of the matcher the
split proof relies on.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import css_split_lib as lib  # noqa: E402

pytestmark = pytest.mark.unit


def test_attribute_selector_terminates_and_parses():
    comps = lib.parse_selector('html[data-theme="dark"] .site-body a[href]:hover')
    assert [c.tag for c in comps] == ["html", None, "a"]
    assert comps[0].attrs == [("data-theme", "=", "dark")]
    assert comps[2].attrs == [("href", None, None)]


@pytest.mark.parametrize("bad", ["a[href", ".x:not(.y", 'a[b="c'])
def test_unterminated_selectors_raise_instead_of_looping(bad):
    with pytest.raises(ValueError):
        lib.parse_selector(bad)


def test_scan_finds_a_closing_bracket_at_depth_zero():
    assert lib._scan("[a=b]c", 1, "]") == 4
    assert lib._scan("(x(y))z", 1, ")") == 5
    assert lib._scan("no stop here", 0, ";") == -1


def test_parse_selector_position_always_advances():
    # every shape the sheets use, parsed under a hard iteration ceiling via the guard
    for sel in [
        "*", "a", ".a.b", "#i", ".a > .b + .c ~ .d", "a:not(.x, .y)::before",
        ".a:nth-child(2n+1)", "::view-transition-old(root)", "[hidden]", ".lightbox[hidden]",
    ]:  # fmt: skip
        assert lib.parse_selector(sel)


def test_sheet_parses_nested_media_and_atomic_rules():
    css = "/* c */ a{b:c} @media (x){d{e:f} g{h:i}} @keyframes k{from{a:b}to{a:c}} @import 'x';"
    sheet = lib.Sheet("t.css", css)
    assert [(r.kind, r.ctx) for r in sheet.rules] == [
        ("style", ()),
        ("style", ("@media (x)",)),
        ("style", ("@media (x)",)),
        ("atomic", ()),
        ("stmt", ()),
    ]
    assert sheet.src(sheet.rules[0]) == "a{b:c}"


def test_sheet_rejects_unbalanced_input_instead_of_looping():
    with pytest.raises(ValueError):
        lib.Sheet("t.css", "a{b:c")
    with pytest.raises(ValueError):
        lib.Sheet("t.css", "a{b:c}}")


def test_specificity_and_optimistic_matching():
    assert lib.specificity(".sr-admin .sr-btn") == (0, 2, 0)
    assert lib.specificity("#a .b:hover") == (1, 2, 0)
    assert lib.specificity(":where(.a) b") == (0, 0, 1)
    nodes = lib.parse_html('<html><body class="sr"><a class="x" href="/">y</a></body></html>')
    a = next(n for n in nodes if n.tag == "a")
    assert lib.match_selector(a, ".sr a.x:hover", set())  # dynamic pseudo: optimistic
    assert not lib.match_selector(a, ".sr-admin a.x", set())
    assert lib.match_selector(a, "html[data-theme=dark] a", {"data-theme"})  # script-set attr
    assert not lib.match_selector(a, "html[data-theme=dark] a", set())
