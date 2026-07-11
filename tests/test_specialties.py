import pytest

from app import specialties
from app.render import _portfolio_alt, _tag_label


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
def test_tag_label_filter():
    assert _tag_label("re/exteriors") == "Exteriors"
    assert _tag_label("Dishes") == "Dishes"
    assert _tag_label(None) == ""
