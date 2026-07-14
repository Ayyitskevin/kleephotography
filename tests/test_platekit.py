import pytest

from app import platekit

pytestmark = pytest.mark.unit


def test_platekit_slug_prefers_explicit_mapping():
    client = {"name": "Wrong Name", "company": "Wrong Company", "platekit_slug": " Blue Plate!! "}
    assert platekit.slug_for_client(client) == "blue-plate"


def test_platekit_slug_falls_back_to_company_or_name():
    assert (
        platekit.slug_for_client({"name": "Avery", "company": "Blue Plate", "platekit_slug": ""})
        == "blue-plate"
    )
    assert (
        platekit.slug_for_client({"name": "Avery Studio", "company": "", "platekit_slug": ""})
        == "avery-studio"
    )


def test_platekit_disabled_has_no_signup_url():
    state = platekit.packs_for_client({"name": "Avery", "company": "Blue Plate"})
    assert "signup_url" not in state
