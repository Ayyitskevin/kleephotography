"""Unit tests for the db.ident SQL-identifier safety gate.

Keeps pure units fast; full row/404 behavior covered by smoke + integration.
"""

import pytest

from app import db


@pytest.mark.unit
def test_ident_allows_whitelisted():
    allowed = {"clients", "projects", "galleries", "assets", "inquiries"}
    assert db.ident("clients", allowed) == "clients"
    assert db.ident("projects", allowed) == "projects"


@pytest.mark.unit
def test_ident_rejects_disallowed_raises():
    with pytest.raises(ValueError) as exc:
        db.ident("; DROP TABLE clients;", {"clients"})
    assert "disallowed" in str(exc.value).lower()
