"""Public marketing image metadata contracts."""

import pytest
from PIL import Image

from app.public import site

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("thumb_size", "web_size", "warns"),
    [
        ((80, 120), (320, 200), True),
        ((80, 120), (80, 120), False),
    ],
)
def test_public_photo_spec_omits_invalid_srcsets(
    tmp_path, monkeypatch, caplog, thumb_size, web_size, warns
):
    monkeypatch.setattr(site.config, "MEDIA_DIR", tmp_path)
    asset = {"id": 17, "gallery_id": 9, "stored": "frame.jpg"}

    for variant, size in (("thumb", thumb_size), ("web", web_size)):
        directory = tmp_path / "9" / variant
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (20, 40, 60)).save(directory / "frame.jpg", "JPEG")

    spec = site._public_photo_spec(asset)

    assert spec["available"] is True
    assert spec["srcset"] is None
    assert (spec["thumb"]["width"], spec["thumb"]["height"]) == thumb_size
    assert (spec["web"]["width"], spec["web"]["height"]) == web_size
    assert ("mismatched derivative ratios" in caplog.text) is warns


def test_public_video_poster_spec_prefers_poster_then_thumb(tmp_path, monkeypatch):
    monkeypatch.setattr(site.config, "MEDIA_DIR", tmp_path)
    asset = {"id": 23, "gallery_id": 9, "stored": "reel.mp4"}
    web = tmp_path / "9" / "web"
    thumb = tmp_path / "9" / "thumb"
    web.mkdir(parents=True)
    thumb.mkdir(parents=True)
    Image.new("RGB", (320, 180), (20, 40, 60)).save(web / "reel_poster.jpg", "JPEG")
    Image.new("RGB", (80, 45), (20, 40, 60)).save(thumb / "reel.jpg", "JPEG")

    poster = site._public_video_poster_spec(asset)
    assert poster == {
        "available": True,
        "url": "/site/poster/23",
        "width": 320,
        "height": 180,
    }

    (web / "reel_poster.jpg").unlink()
    fallback = site._public_video_poster_spec(asset)
    assert fallback == {
        "available": True,
        "url": "/site/img/23?variant=thumb",
        "width": 80,
        "height": 45,
    }

    (thumb / "reel.jpg").unlink()
    assert site._public_video_poster_spec(asset) == {
        "available": False,
        "url": None,
        "width": None,
        "height": None,
    }


def test_public_specs_reject_corrupt_derivatives(tmp_path, monkeypatch):
    monkeypatch.setattr(site.config, "MEDIA_DIR", tmp_path)
    gallery = tmp_path / "9"
    web = gallery / "web"
    thumb = gallery / "thumb"
    web.mkdir(parents=True)
    thumb.mkdir(parents=True)

    photo = {"id": 31, "gallery_id": 9, "stored": "frame.jpg"}
    Image.new("RGB", (320, 180), (20, 40, 60)).save(web / "frame.jpg", "JPEG")
    Image.new("RGB", (80, 45), (20, 40, 60)).save(thumb / "frame.jpg", "JPEG")
    (web / "frame.jpg").write_bytes(b"not an image")

    photo_fallback = site._public_photo_spec(photo)
    assert photo_fallback["available"] is True
    assert photo_fallback["srcset"] is None
    assert photo_fallback["web"] == {
        "url": "/site/img/31?variant=thumb",
        "width": 80,
        "height": 45,
    }

    (thumb / "frame.jpg").write_bytes(b"also not an image")
    assert site._public_photo_spec(photo)["available"] is False

    video = {"id": 32, "gallery_id": 9, "stored": "reel.mp4"}
    Image.new("RGB", (320, 180), (20, 40, 60)).save(web / "reel_poster.jpg", "JPEG")
    Image.new("RGB", (80, 45), (20, 40, 60)).save(thumb / "reel.jpg", "JPEG")
    (web / "reel_poster.jpg").write_bytes(b"not an image")

    assert site._public_video_poster_spec(video) == {
        "available": True,
        "url": "/site/img/32?variant=thumb",
        "width": 80,
        "height": 45,
    }

    (thumb / "reel.jpg").write_bytes(b"also not an image")
    assert site._public_video_poster_spec(video) == {
        "available": False,
        "url": None,
        "width": None,
        "height": None,
    }


def test_portfolio_screening_room_sizes_match_multicol_contract():
    """Keep responsive selection aligned with the default SR masonry geometry."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    template = (root / "templates/site/portfolio.html").read_text()
    legacy_css = (root / "static/mise.css").read_text()
    screening_css = (root / "static/screening.css").read_text()
    exact_sizes = (
        "(max-width: 641px) calc(100vw - 32px), "
        "(max-width: 700px) calc(50vw - 21px), "
        "(max-width: 705px) calc(100vw - 96px), "
        "(max-width: 1015px) calc(50vw - 53px), "
        "(max-width: 1325px) calc(33.333333vw - 38.666667px), "
        "(max-width: 1415px) calc(25vw - 31.5px), 322.5px"
    )

    def css_block(source, marker):
        start = source.index(marker)
        brace = source.index("{", start)
        depth = 0
        for index in range(brace, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    return source[start : index + 1]
        raise AssertionError(f"Unclosed CSS block: {marker}")

    assert '{% set tile_sizes = "' + exact_sizes + '" %}' in template
    assert "{% if sr_enabled() %}" in template
    assert '{% else %}\n{% set tile_sizes = "(max-width: 700px) 100vw, 300px" %}' in template
    assert "box-sizing: border-box" in css_block(legacy_css, "* {")

    masonry = css_block(legacy_css, ".portfolio-masonry {")
    for rule in ("column-width: 300px", "column-gap: 16px", "max-width: 1320px"):
        assert rule in masonry
    assert "column-gap: 10px" in css_block(screening_css, ".sr .portfolio-masonry {")

    wrap = css_block(screening_css, ".sr .sr-wrap {")
    for rule in ("width: 100%", "max-width: 1440px", "padding: 0 48px"):
        assert rule in wrap
    mobile_rule = ".sr .sr-wrap { padding: 0 16px; }"
    mobile_rule_at = screening_css.index(mobile_rule)
    mobile_at = screening_css.rfind("@media (max-width: 700px)", 0, mobile_rule_at)
    assert mobile_rule in css_block(screening_css[mobile_at:], "@media (max-width: 700px)")

    def declared_slot(viewport):
        if viewport <= 641:
            return viewport - 32
        if viewport <= 700:
            return viewport / 2 - 21
        if viewport <= 705:
            return viewport - 96
        if viewport <= 1015:
            return viewport / 2 - 53
        if viewport <= 1325:
            return (viewport - 116) / 3
        if viewport <= 1415:
            return viewport / 4 - 31.5
        return 322.5

    for viewport in (390, 641, 642, 700, 701, 705, 706, 1015, 1016, 1325, 1326, 1415, 1416, 1920):
        outer = min(viewport, 1440)
        padding = 32 if viewport <= 700 else 96
        available = min(outer - padding, 1320)
        columns = max(1, int((available + 10) / 310))
        css_slot = (available + 10) / columns - 10
        assert declared_slot(viewport) == pytest.approx(css_slot, abs=0.01)


def test_lightbox_mixed_media_semantics_contract():
    """Keep the shared viewer's source semantics honest for photo and video."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    marketing = (root / "templates/site/_lightbox.html").read_text()
    gallery = (root / "templates/public/gallery.html").read_text()
    javascript = (root / "static/lightbox.js").read_text()

    for template in (marketing, gallery):
        assert 'aria-label="Media viewer"' in template
        assert 'aria-label="Photo viewer"' not in template
        assert 'class="lb-play" aria-label="Slideshow" aria-pressed="false"' in template

    def block(start, end):
        return javascript.split(start, 1)[1].split(end, 1)[0]

    stop_show = block("function stopShow()", "function startShow()")
    start_show = block("function startShow()", "// the marketing-site lightbox")
    media_name = block("function mediaName(t, fallback)", "function render(i)")
    render = block("function render(i)", "// Return focus")
    tile_init = block("tiles.forEach((t, i) =>", "// Shared fav trigger")

    assert 'playBtn.setAttribute("aria-pressed", "false")' in stop_show
    assert 'playBtn.setAttribute("aria-pressed", "true")' not in stop_show
    assert 'playBtn.setAttribute("aria-pressed", "true")' in start_show
    assert 'playBtn.setAttribute("aria-pressed", "false")' not in start_show
    assert "return (source && source.alt) || fallback;" in media_name
    assert 'v.setAttribute("aria-label", mediaName(t, "Video"));' in render
    assert "if (t.dataset.poster) v.poster = t.dataset.poster;" in render
    assert 'img.alt = mediaName(t, "");' in render
    assert 't.dataset.kind === "video" ? "open video" : "view larger"' in tile_init
    assert 'img.setAttribute("aria-label", mediaName(t, fallback) + " — " + action);' in tile_init
    assert '(img.alt || "Photo") + " — view larger"' not in javascript
    assert 'v.poster = t.dataset.poster || "";' not in javascript


def test_lightbox_arrow_navigation_respects_interactive_owners():
    """Keep global lightbox arrows away from native/editable controls."""
    from pathlib import Path

    javascript = (Path(__file__).resolve().parents[1] / "static/lightbox.js").read_text()
    keydown = javascript.split('document.addEventListener("keydown", (e) => {', 1)[1].split(
        "// Touch gestures", 1
    )[0]

    hidden_guard = "if (lb.hidden || e.defaultPrevented || e.isComposing) return;"
    escape = 'if (e.key === "Escape") {'
    arrow_owner = "const arrowOwner = target && ("
    arrow_branch = 'if (e.key === "ArrowLeft" || e.key === "ArrowRight") {'
    modifier_guard = "if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || arrowOwner) return;"
    tab_branch = 'if (e.key === "Tab") {'
    escape_block = keydown.split(escape, 1)[1].split("const target", 1)[0]
    ownership_setup = keydown.split("const target", 1)[1].split(arrow_branch, 1)[0]
    arrow_block = keydown.split(arrow_branch, 1)[1].split("// Keep Tab", 1)[0]
    tab_block = keydown.split(tab_branch, 1)[1]

    assert hidden_guard in keydown
    assert escape in keydown
    assert "close();" in escape_block
    assert "return;" in escape_block
    assert escape_block.index("close();") < escape_block.index("return;")
    assert arrow_owner in keydown
    assert "target.isContentEditable" in keydown
    assert 'target.closest("input, textarea, select, video")' in keydown
    assert arrow_branch in keydown
    assert "return;" not in ownership_setup
    assert modifier_guard in arrow_block
    assert "e.preventDefault();" in arrow_block
    assert "stopShow();" in arrow_block
    assert 'step(e.key === "ArrowLeft" ? -1 : 1);' in arrow_block
    assert (
        arrow_block.index(modifier_guard)
        < arrow_block.index("e.preventDefault();")
        < arrow_block.index("stopShow();")
        < arrow_block.index('step(e.key === "ArrowLeft" ? -1 : 1);')
        < arrow_block.rindex("return;")
    )
    assert tab_branch in keydown
    assert "lb.querySelectorAll" in tab_block
    assert 'button, a[href], input, textarea, [tabindex]:not([tabindex="-1"])' in tab_block
    assert "e.shiftKey && document.activeElement === first" in tab_block
    assert "!e.shiftKey && document.activeElement === last" in tab_block
    assert tab_block.count("e.preventDefault();") == 2
    assert "last.focus();" in tab_block
    assert "first.focus();" in tab_block
    assert "if (arrowOwner) return;" not in keydown
    assert keydown.index(hidden_guard) < keydown.index(escape) < keydown.index(arrow_owner)
    assert keydown.index(arrow_owner) < keydown.index(arrow_branch)
    assert keydown.index(arrow_branch) < keydown.index(tab_branch)


def test_lightbox_comment_response_ownership_source_contract():
    """Keep late comment responses from mutating a different video."""
    from pathlib import Path

    javascript = (Path(__file__).resolve().parents[1] / "static/lightbox.js").read_text()

    def block(start, end):
        return javascript.split(start, 1)[1].split(end, 1)[0]

    state = block("let activeVideo = null;", "function fmtTC(s)")
    owner = block(
        "function ownsCommentResponse(assetId, requestVersion) {",
        "\n  }\n\n  function fmtTC(s)",
    )
    load = block("async function loadComments(assetId)", "function stopShow()")
    render = block("function render(i)", "// Return focus")
    video = render.split('if (t.dataset.kind === "video") {', 1)[1].split("\n    } else {", 1)[0]
    close = block("function close()", "// Each tile image")
    submit = block(
        'if (cForm) cForm.addEventListener("submit", async (e) => {',
        "function vcError(msg)",
    )

    load_guard = "if (!ownsCommentResponse(assetId, requestVersion)) return;"
    submit_guard = load_guard
    error_message = 'vcError("Couldn\'t post your note — refresh the page and try again.");'

    assert "let commentRequestVersion = 0;" in state
    assert "function ownsCommentResponse(assetId, requestVersion)" in state
    assert owner.strip() == (
        "return activeAsset === assetId && commentRequestVersion === requestVersion;"
    )

    assert load.count("const requestVersion = ++commentRequestVersion;") == 1
    assert 'await fetch("/g/" + slug + "/comments/" + assetId)' in load
    assert load.count(load_guard) == 2
    assert "if (!res.ok) return;" in load
    assert "const comments = await res.json();" in load
    assert "renderComments(await res.json())" not in load
    first_load_guard = load.index(load_guard)
    second_load_guard = load.index(load_guard, first_load_guard + 1)
    assert (
        load.index("const requestVersion")
        < load.index("await fetch(")
        < first_load_guard
        < load.index("if (!res.ok) return;")
        < load.index("await res.json()")
        < second_load_guard
        < load.index("renderComments(comments);")
    )

    assert "activeAsset = t.dataset.id;" in render
    video_resets = (
        "lastComments = [];",
        'if (cList) cList.innerHTML = "";',
        'cCount.textContent = "";',
        'cCount.classList.remove("ok");',
        'vcError("");',
    )
    assert all(reset in video for reset in video_resets)
    assert video.index("activeAsset = t.dataset.id;") < video.index(video_resets[0])
    assert all(
        video.index(reset) < video.index("loadComments(activeAsset);") for reset in video_resets
    )
    assert render.index("activeAsset = null;") < render.index("commentRequestVersion += 1;")
    assert close.index("activeAsset = null;") < close.index("commentRequestVersion += 1;")

    assert "const assetId = activeAsset;" in submit
    assert submit.count("const requestVersion = ++commentRequestVersion;") == 1
    assert 'await fetch("/g/" + slug + "/comments/" + assetId' in submit
    assert '"/comments/" + activeAsset' not in submit
    assert submit.count(submit_guard) == 2
    assert "const comments = await res.json().catch(() => null);" in submit
    assert "renderComments(comments);" in submit
    parse_failure = submit.split("if (!comments) {", 1)[1].split("\n      }", 1)[0]
    assert parse_failure.strip() == f"{error_message}\n        return;"
    first_submit_guard = submit.index(submit_guard)
    second_submit_guard = submit.index(submit_guard, first_submit_guard + 1)
    assert submit.count(error_message) == 2
    first_error = submit.index(error_message)
    second_error = submit.index(error_message, first_error + 1)
    assert (
        submit.index("if (!body || !activeAsset) return;")
        < submit.index("const assetId")
        < submit.index("const requestVersion")
        < submit.index("await fetch(")
        < first_submit_guard
        < submit.index("if (res && res.ok)")
        < submit.index("await res.json()")
        < second_submit_guard
        < first_error
        < submit.index("renderComments(comments);")
        < submit.index('cBody.value = "";')
    )
    assert (
        first_submit_guard
        < submit.index("res.status === 403")
        < submit.index("window.location.reload();")
    )
    assert first_submit_guard < second_error
    assert second_submit_guard < first_error
