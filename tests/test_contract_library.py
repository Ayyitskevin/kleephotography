"""Smoke the specialty contract library files Kevin can draft from admin."""

import pytest

from app.admin.contracts import CONTRACT_LIBRARY, load_library_template

pytestmark = pytest.mark.unit

SPECIALTY_KEYS = ("real_estate_services", "portrait_services", "photography_agreement")


@pytest.mark.unit
def test_specialty_contract_templates_load():
    for key in SPECIALTY_KEYS:
        assert key in CONTRACT_LIBRARY
        body = load_library_template(key)
        assert len(body) > 200
        assert "{client_name}" in body or "CLIENT" in body.upper()


@pytest.mark.unit
def test_aerial_pass_preset_matches_specialty_rate():
    from app import specialties
    from app.admin.proposals import PRESETS

    items = PRESETS["aerial_pass"]["items"]
    paid = sum(i["qty"] * i["unit_cents"] for i in items)
    assert paid == specialties.AERIAL_PASS_CENTS
