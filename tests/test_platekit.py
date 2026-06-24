from app import platekit


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


def test_platekit_signup_url_prefills_client_fields():
    url = platekit.signup_url({"name": "Avery", "company": "Blue Plate", "email": "a@example.com"})
    assert url.startswith("https://platekit.kleephotography.com/?")
    assert url.endswith("#signup")
    assert "company=Blue+Plate" in url
    assert "email=a%40example.com" in url
