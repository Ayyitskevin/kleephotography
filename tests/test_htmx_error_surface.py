"""The HTMX failure toast has to be legible on every surface that can fail.

`base.html` ships the `.sr-toasts` live region everywhere, but every toast rule
used to be `.sr`-scoped — so on `public/contract.html` (extends base_cream.html,
body.cream-theme, and it stays that way: it is the e-sign document) the toast
rendered as unstyled text in a static div below the whole agreement. Same on
every surface whenever MISE_SCREENING_ROOM=false. These tests pin the unscoped
base layer that fixes it AND the constraint that keeps the kill switch honest:
the base layer may only touch properties the `.sr` cinema layer also sets, so
Screening Room surfaces still render exactly as they did.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

ROOT = Path(__file__).resolve().parents[1]
SECTION_START = "/* ── toasts — window.miseToast"
SECTION_END = "/* ── flow chrome"


def _toast_rules() -> dict[str, set[str]]:
    """selector -> property names, for every toast rule in screening.css.

    Rules inside @media merge into their selector: the base and cinema layers
    carry parallel media queries, so a per-selector union is the right unit for
    the subset check below.
    """
    css = (ROOT / "static/screening.css").read_text()
    section = css[css.index(SECTION_START) : css.index(SECTION_END)]
    section = re.sub(r"/\*.*?\*/", "", section, flags=re.S)
    rules: dict[str, set[str]] = {}
    for selector, body in re.findall(r"([^{}@]+?)\s*\{([^{}]*)\}", section):
        selector = " ".join(selector.split())
        if not selector.startswith("."):
            continue  # @keyframes steps ("from"), @media heads
        props = {d.split(":", 1)[0].strip() for d in body.split(";") if ":" in d}
        rules.setdefault(selector, set()).update(props)
    return rules


@pytest.mark.unit
def test_toast_is_styled_without_the_screening_room_scope():
    """An error must be visible with no body.sr ancestor — the kill-switch path."""
    rules = _toast_rules()

    container = rules.get(".sr-toasts")
    assert container, "unscoped .sr-toasts rule is gone — toasts fall back to static flow"
    # fixed + stacked, or the toast lands below the agreement instead of over it
    assert {"position", "z-index", "right", "bottom"} <= container

    toast = rules.get(".sr-toast")
    assert toast, "unscoped .sr-toast rule is gone — the message renders as bare text"
    # a background and an ink colour are what make it legible over page content
    assert {"background", "color", "padding"} <= toast
    assert ".sr-toast--danger" in rules, "the danger variant needs an unscoped colour too"


@pytest.mark.unit
def test_screening_room_toast_styling_is_unchanged_by_the_base_layer():
    """The cinema skin stays a pure .sr enhancement (ops/CSS-DUAL-STACK.md).

    Every property the unscoped layer sets must also be set by the matching
    `.sr` rule. `.sr …` always outranks the bare selector on specificity, so
    this subset relation is exactly the condition for SR surfaces to render
    byte-identically — and it fails loudly if someone adds, say, a
    border-radius to the base layer that would leak into Screening Room.
    """
    rules = _toast_rules()
    for base in (
        ".sr-toasts",
        ".sr-toast",
        ".sr-toast--ok",
        ".sr-toast--warn",
        ".sr-toast--danger",
        ".sr-toast.is-out",
    ):
        scoped = f".sr {base}"
        assert scoped in rules, f"{scoped} disappeared — the .sr toast skin is the SR look"
        leaked = rules[base] - rules[scoped]
        assert not leaked, (
            f"{base} sets {sorted(leaked)}, which .sr does not override — it would leak into Screening Room"
        )


@pytest.mark.integration
@pytest.mark.parametrize("screening_room", (True, False))
def test_contract_page_carries_the_toast_region_in_both_stacks(monkeypatch, screening_room):
    """The e-sign page never opts into .sr — so it must get the toast anyway.

    smoke/test_02_studio_docs.py pins `class="cream-theme"` here deliberately;
    the fix is CSS, not a bodyclass change to a document a client signs.
    """
    monkeypatch.setattr(config, "SCREENING_ROOM", screening_room)
    client_id = db.run("INSERT INTO clients (name, company) VALUES (?,?)", ("Toast Probe", "TP"))
    project_id = db.run(
        "INSERT INTO projects (client_id, title) VALUES (?,?)", (client_id, "Toast probe")
    )
    slug = f"toastprobe{int(screening_room)}"
    db.run(
        """INSERT INTO contracts (project_id, slug, title, body, body_sha256, status)
           VALUES (?,?,?,?,?,'sent')""",
        (project_id, slug, "Services Agreement", "BODY", "a" * 64),
    )
    try:
        with TestClient(app) as pub:
            page = pub.get(f"/c/{slug}")
        assert page.status_code == 200
        assert 'class="cream-theme"' in page.text  # never .sr — legal surface
        assert 'class="sr-toasts"' in page.text  # the live region miseToast targets
        assert "/static/behaviors.js" in page.text  # the handler that fills it
        assert "/static/screening.css" in page.text  # the sheet that styles it
        assert 'hx-post="/c/' in page.text  # the failure path this exists for
    finally:
        db.run("DELETE FROM contracts WHERE slug=?", (slug,))
        db.run("DELETE FROM projects WHERE id=?", (project_id,))
        db.run("DELETE FROM clients WHERE id=?", (client_id,))
