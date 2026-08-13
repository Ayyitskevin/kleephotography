"""The mise.css / mise-admin.css split contract.

`static/mise.css` ships on every surface; `static/mise-admin.css` ships only
with the admin shell. These tests pin the three properties that make the split
safe: nothing admin-scoped rides along on public pages, nothing in the admin
sheet can reach outside `.admin-shell`, and both sheets enter the same cascade
layer so the Screening Room contract in `ops/CSS-DUAL-STACK.md` still holds.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_CSS = ROOT / "static/mise.css"
ADMIN_CSS = ROOT / "static/mise-admin.css"
BASE = ROOT / "templates/base.html"
BASE_CREAM = ROOT / "templates/base_cream.html"
BASE_ADMIN = ROOT / "templates/admin/base_admin.html"
LOGIN = ROOT / "templates/admin/login.html"

# A rule kept in the public sheet purely so a later unscoped admin rule keeps
# out-ordering it carries this marker (see ops/CSS-DUAL-STACK.md).
ORDER_PINNED = "/* ORDER-PINNED"


def _top_level_nodes(css: str):
    """Yield (kind, prelude, body_text) for every top-level construct."""
    i, n = 0, len(css)
    while i < n:
        if css[i].isspace():
            i += 1
            continue
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        brace = css.find("{", i)
        if brace < 0:
            break
        depth, j = 0, brace
        while j < n:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        prelude = css[i:brace].strip()
        yield ("atrule" if prelude.startswith("@") else "rule"), prelude, css[brace + 1 : j]
        i = j + 1


def _selectors(prelude: str) -> list[str]:
    prelude = re.sub(r"/\*.*?\*/", " ", prelude, flags=re.S)
    return [s.strip() for s in prelude.split(",") if s.strip()]


def _admin_scoped(prelude: str) -> bool:
    sels = _selectors(prelude)
    return bool(sels) and all("admin-shell" in s for s in sels)


def test_public_sheet_ships_no_admin_only_rules():
    """Every .admin-shell-only rule left in the public sheet is order-pinned."""
    css = PUBLIC_CSS.read_text()
    unmarked = []
    for kind, prelude, _body in _top_level_nodes(css):
        if kind != "rule" or not _admin_scoped(prelude):
            continue
        start = css.find(prelude)
        if ORDER_PINNED not in css[max(0, start - 120) : start]:
            unmarked.append(prelude)
    assert not unmarked, f"admin-only rules shipped to public pages: {unmarked}"


def test_public_sheet_admin_rules_inside_at_rules_are_genuinely_mixed():
    """An @media block kept in the public sheet must serve public selectors too."""
    css = PUBLIC_CSS.read_text()
    for kind, prelude, body in _top_level_nodes(css):
        if kind != "atrule":
            continue
        inner = list(_top_level_nodes(body))
        admin = [p for k, p, _ in inner if k == "rule" and _admin_scoped(p)]
        public = [p for k, p, _ in inner if k == "rule" and not _admin_scoped(p)]
        if admin:
            assert public, f"{prelude} holds only admin rules — belongs in mise-admin.css"


def test_admin_sheet_can_never_paint_outside_the_shell():
    """Every selector in the admin sheet is .admin-shell-scoped."""
    css = ADMIN_CSS.read_text()
    stray = []
    for kind, prelude, body in _top_level_nodes(css):
        rules = (
            [(prelude, body)]
            if kind == "rule"
            else [(p, b) for k, p, b in _top_level_nodes(body) if k == "rule"]
        )
        if kind == "atrule" and not prelude.startswith(("@media", "@container", "@supports")):
            continue
        stray += [p for p, _ in rules if not _admin_scoped(p)]
    assert not stray, f"non-admin selectors in mise-admin.css: {stray[:10]}"


def test_admin_sheet_is_smaller_bet_than_the_public_one():
    """The split is only worth its risk if the public sheet really shrank."""
    assert PUBLIC_CSS.stat().st_size < ADMIN_CSS.stat().st_size
    assert PUBLIC_CSS.stat().st_size < 200_000


def _markup(template: Path) -> str:
    """Template text minus its {# … #} comments, which may name the sheet."""
    return re.sub(r"\{#.*?#\}", " ", template.read_text(), flags=re.S)


def test_only_the_admin_shell_loads_the_admin_sheet():
    assert "mise-admin.css" not in _markup(BASE)
    assert "mise-admin.css" not in _markup(BASE_CREAM)
    # login.html extends base_cream directly, so it never gets the admin sheet
    assert "base_cream.html" in LOGIN.read_text()
    assert "mise-admin.css" not in _markup(LOGIN)
    assert _markup(BASE_ADMIN).count("mise-admin.css") == 1


def test_both_sheets_enter_the_same_cascade_layer():
    """Layer(mise) is the contract that keeps unlayered Screening Room on top."""
    idiom = re.compile(
        r'<style>@import url\("/static/(mise|mise-admin)\.css\?v=\{\{ static_rev \}\}"\) layer\(mise\);</style>'
    )
    assert idiom.search(BASE.read_text()).group(1) == "mise"
    assert idiom.search(BASE_ADMIN.read_text()).group(1) == "mise-admin"


def test_screening_caption_floor():
    """The 8.5–9px caption/kicker sizes are gone. 9.5px labels are a separate scale."""
    css = (ROOT / "static/screening.css").read_text()
    assert "8.5px" not in css
    assert not re.search(r"font-size:\s*9px\b", css)
    assert not re.search(r"\bfont:\s*[^;{}]*\s9px/", css)


def test_admin_shell_markup_lives_only_on_the_admin_base():
    """The moved rules are unreachable elsewhere only while this stays true."""
    wearers = [
        p
        for p in (ROOT / "templates").rglob("*.html")
        if re.search(r'class="[^"]*\badmin-shell\b', p.read_text())
    ]
    assert wearers == [BASE_ADMIN]
