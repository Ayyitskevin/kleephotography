"""Half-screen (861–1280px) admin tables: the scroll rule must not unhide or
mis-pin them.

In that range `table.list` becomes `display: block` so wide tables scroll inside
the column. That rule outranks the UA's `[hidden] { display: none }`, which put
the Studio board's inactive list view under the board; and a sticky th measured
`top: 47px` from the table instead of the viewport, sliding the header over
row 1. Both were browser-observed at 960px; this pins the two guards in place.
"""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "static/mise-admin.css").read_text("utf-8")


def _media_block(query: str) -> str:
    start = CSS.index(query) + len(query)
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(CSS[i], 0)
        i += 1
    return CSS[start:i]


@pytest.mark.unit
def test_halfscreen_scroll_rule_keeps_hidden_tables_hidden_and_unpins_headers():
    block = _media_block("@media (min-width: 861px) and (max-width: 1280px){")

    assert re.search(r"table\.list\s*\{[^}]*display:\s*block", block)
    assert re.search(r"table\.list\[hidden\]\s*\{\s*display:\s*none;?\s*\}", block)
    assert re.search(
        r"table\.list thead th,\s*\.admin-shell table\.list tr:first-child th\s*\{\s*position:\s*static",
        block,
    )
