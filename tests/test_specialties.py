import pytest

from app import specialties
from app.render import ALT_MAX_CHARS, _frame_alt, _portfolio_alt, _tag_label

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_split_tag_prefixed():
    assert specialties.split_tag("re/exteriors") == ("re", "exteriors")
    assert specialties.split_tag("pl/golden-hour") == ("pl", "golden-hour")
    assert specialties.split_tag("fb/dishes") == ("fb", "dishes")
    # prefix matching is case/space tolerant
    assert specialties.split_tag(" RE/Twilight ") == ("re", "Twilight")


@pytest.mark.unit
def test_split_tag_legacy_unprefixed_is_fb():
    assert specialties.split_tag("dishes") == ("fb", "dishes")
    assert specialties.split_tag("motion") == ("fb", "motion")
    assert specialties.split_tag("") == ("fb", "")
    assert specialties.split_tag(None) == ("fb", "")


@pytest.mark.unit
def test_split_tag_unknown_prefix_stays_in_label():
    # a stray slash never mis-buckets work into another vertical
    assert specialties.split_tag("behind/scenes") == ("fb", "behind/scenes")


@pytest.mark.unit
def test_by_slug():
    key, meta = specialties.by_slug("real-estate")
    assert key == "re" and meta["name"] == "Real Estate"
    key, meta = specialties.by_slug("portraits")
    assert key == "pl"
    key, meta = specialties.by_slug("food-beverage")
    assert key == "fb"
    assert specialties.by_slug("weddings") is None


@pytest.mark.unit
def test_portfolio_alt_craft_follows_prefix():
    assert (
        _portfolio_alt({"portfolio_tag": "re/exteriors"}, site_name="Kevin Lee Photography")
        == "Exteriors — real estate photography by Kevin Lee Photography"
    )
    assert (
        _portfolio_alt({"portfolio_tag": "pl/headshots"}, site_name="Kevin Lee Photography")
        == "Headshots — portrait & lifestyle photography by Kevin Lee Photography"
    )
    # legacy unprefixed tags keep their F&B alt text verbatim
    assert (
        _portfolio_alt({"portfolio_tag": "Dishes"}, site_name="Kevin Lee Photography")
        == "Dishes — food & beverage photography by Kevin Lee Photography"
    )
    # untagged assets keep the legacy default
    assert (
        _portfolio_alt({"portfolio_tag": ""}, site_name="Kevin Lee Photography")
        == "Food & beverage photography by Kevin Lee Photography"
    )
    # a bare prefix tag ('re/') still reads as a sentence
    assert (
        _portfolio_alt({"portfolio_tag": "re/"}, site_name="Kevin Lee Photography")
        == "Real estate photography by Kevin Lee Photography"
    )


@pytest.mark.unit
def test_portfolio_alt_prefers_the_generated_description():
    """Argus writes a real description per frame; the tag phrase is the fallback.

    Without this the whole /portfolio grid shipped one identical alt string per
    tag — see app/argus_writeback.py, whose only other reader is an admin
    hover overlay.
    """
    asset = {"portfolio_tag": "fb/dishes", "argus_alt_text": "Seared scallops on a slate plate"}
    assert (
        _portfolio_alt(asset, site_name="Kevin Lee Photography")
        == "Seared scallops on a slate plate"
    )
    # The studio name is not appended to a real description — it is already in
    # the <title> and the LocalBusiness JSON-LD.
    assert "Kevin Lee" not in _portfolio_alt(asset, site_name="Kevin Lee Photography")


@pytest.mark.unit
def test_portfolio_alt_falls_back_when_the_description_is_absent_or_blank():
    for value in (None, "", "   "):
        assert (
            _portfolio_alt(
                {"portfolio_tag": "re/exteriors", "argus_alt_text": value},
                site_name="Kevin Lee Photography",
            )
            == "Exteriors — real estate photography by Kevin Lee Photography"
        )


@pytest.mark.unit
def test_generated_alt_is_collapsed_and_capped():
    """Argus is a remote service; its output length is not ours to trust."""
    messy = {"argus_alt_text": "  A plate\n\twith   herbs  "}
    assert _portfolio_alt(messy, site_name="K") == "A plate with herbs"

    long = {"argus_alt_text": "word " * 200}
    out = _portfolio_alt(long, site_name="K")
    assert len(out) <= ALT_MAX_CHARS
    assert out.endswith("\u2026")


@pytest.mark.unit
def test_frame_alt_uses_the_positional_fallback_only_when_undescribed():
    """The client gallery's fallback is positional, not a craft phrase."""
    assert _frame_alt({"argus_alt_text": "Bride on the stairs"}, "Wedding — frame 0042") == (
        "Bride on the stairs"
    )
    assert _frame_alt({"argus_alt_text": None}, "Wedding — frame 0042") == "Wedding — frame 0042"
    assert _frame_alt({}, "Wedding — frame 0042") == "Wedding — frame 0042"


@pytest.mark.unit
def test_tag_label_filter():
    assert _tag_label("re/exteriors") == "Exteriors"
    assert _tag_label("Dishes") == "Dishes"
    assert _tag_label(None) == ""
