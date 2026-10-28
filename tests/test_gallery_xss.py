"""Reflected XSS on the public favorite toggle (CodeQL py/reflective-xss).

The three alerts are one path parameter, ``asset_id`` on
``POST /g/{slug}/fav/{asset_id}``, written into three HTML responses: the
circled take, the uncircled take, and the proof-cap refusal. FastAPI rejects a
non-integer id before the handler. The span itself is rendered by Jinja so the
id is escaped if it ever reaches the template.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import config, db, security
from app.main import app
from app.public.gallery import _fav_span

pytestmark = pytest.mark.integration

_PAYLOAD = "<script>alert(1)</script>"
_ATTR_BREAK = '1"><img src=x onerror=alert(1)>'


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _seed(slug: str) -> tuple[int, int, int]:
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        (slug, "XSS gallery", "1234"),
    )
    sid = db.run(
        "INSERT INTO sections (gallery_id, name, position, proof_target) VALUES (?,?,?,?)",
        (gid, "Selects", 0, 1),
    )
    ids = []
    for i, name in enumerate(("one.jpg", "two.jpg")):
        src = config.MEDIA_DIR / str(gid) / "original"
        src.mkdir(parents=True, exist_ok=True)
        (src / name).write_bytes(b"fake-image-bytes")
        ids.append(
            db.run(
                "INSERT INTO assets (gallery_id, section_id, kind, filename, stored, status) "
                "VALUES (?,?,?,?,?,?)",
                (gid, sid, "photo", name, name, "ready"),
            )
        )
    token = f"{slug}-visitor"
    db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (gid, token))
    return gid, ids[0], ids[1]


def _auth(client: TestClient, gid: int, slug: str) -> None:
    client.cookies.set(security.visitor_cookie_name(gid), security.sign(f"{slug}-visitor"))


def _cleanup(client: TestClient, gid: int) -> None:
    db.run(
        "DELETE FROM favorites WHERE asset_id IN (SELECT id FROM assets WHERE gallery_id=?)",
        (gid,),
    )
    db.run("DELETE FROM visitors WHERE gallery_id=?", (gid,))
    db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
    db.run("DELETE FROM sections WHERE gallery_id=?", (gid,))
    db.run("DELETE FROM galleries WHERE id=?", (gid,))
    client.cookies.clear()


def _empty(asset_id: int) -> str:
    return f'<span id="take-{asset_id}" class="fav-btn sr-take">&#9675;</span>'


def _circled(asset_id: int, count: int) -> str:
    return f'<span id="take-{asset_id}" class="fav-btn faved sr-take is-circled">{count}</span>'


def _tags(html: str) -> list[tuple[str, dict[str, str]]]:
    class _Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.tags: list[tuple[str, dict[str, str]]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.tags.append((tag, {k: v or "" for k, v in attrs}))

    parser = _Collector()
    parser.feed(html)
    return parser.tags


def test_fav_span_escapes_markup_in_the_attribute():
    """The three sinks share this writer. A raw id must not open another tag."""
    empty = _fav_span(_PAYLOAD, circled=False)
    circled = _fav_span(_ATTR_BREAK, circled=True, count=1)
    assert _tags(empty) == [("span", {"id": f"take-{_PAYLOAD}", "class": "fav-btn sr-take"})]
    assert _tags(circled) == [
        (
            "span",
            {"id": f"take-{_ATTR_BREAK}", "class": "fav-btn faved sr-take is-circled"},
        )
    ]
    assert "<script>" not in empty and "<img" not in circled
    assert _fav_span(12, circled=False) == _empty(12)
    assert _fav_span(12, circled=True, count=3) == _circled(12, 3)


def test_favorite_toggle_rejects_markup_and_still_renders(client, monkeypatch):
    # A real photo favorite enqueues social_crops. Leave the job table alone so
    # a later test that reuses an asset id does not inherit this row.
    monkeypatch.setattr("app.public.gallery.jobs.enqueue", lambda *_a, **_k: 1)
    gid, first, second = _seed("xss-fav")
    try:
        _auth(client, gid, "xss-fav")
        page = client.get("/g/xss-fav")
        assert page.status_code == 200
        assert _empty(first) in page.text
        assert f"/media/xss-fav/thumb/{first}" in page.text

        # </script> contains a slash, so the path never matches {asset_id}.
        script = client.post(f"/g/xss-fav/fav/{quote(_PAYLOAD, safe='')}")
        assert script.status_code == 404
        assert _PAYLOAD not in script.text
        assert "<span" not in script.text

        # A slash-free breakout reaches the int converter and is rejected there.
        # The validation body is JSON, so the echoed input is not an HTML sink.
        broken = client.post(f"/g/xss-fav/fav/{quote(_ATTR_BREAK, safe='')}")
        assert broken.status_code == 422
        assert broken.headers["content-type"].startswith("application/json")
        assert "<span" not in broken.text
        detail = broken.json()["detail"][0]
        assert detail["loc"] == ["path", "asset_id"]
        assert detail["type"] == "int_parsing"

        circled = client.post(f"/g/xss-fav/fav/{first}")
        assert circled.status_code == 200
        assert circled.text.startswith(_circled(first, 1))
        assert "<script>" not in circled.text

        cleared = client.post(f"/g/xss-fav/fav/{first}")
        assert cleared.status_code == 200
        assert cleared.text.startswith(_empty(first))
        assert "faved" not in cleared.text

        again = client.post(f"/g/xss-fav/fav/{first}")
        assert again.status_code == 200 and again.text.startswith(_circled(first, 1))

        capped = client.post(f"/g/xss-fav/fav/{second}")
        assert capped.status_code == 409
        assert capped.text == _empty(second)
        assert "<script>" not in capped.text

        reloaded = client.get("/g/xss-fav")
        assert reloaded.status_code == 200
        assert _circled(first, 1) in reloaded.text
        assert _empty(second) in reloaded.text
    finally:
        _cleanup(client, gid)
