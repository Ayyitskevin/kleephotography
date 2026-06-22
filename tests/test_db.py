"""Unit tests for db helpers (ident safety gate + spark_series availability).

Keeps pure units fast; full row/404 behavior covered by smoke + integration.
"""

import datetime as dt

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


@pytest.mark.unit
def test_spark_series_exists_and_callable():
    assert hasattr(db, "spark_series")
    assert callable(db.spark_series)


@pytest.mark.unit
def test_get_or_404_exists():
    # Signature and presence; behavior (404 raise) exercised in smoke paths.
    assert hasattr(db, "get_or_404")
    # get_or_404 uses one() internally
    assert callable(db.get_or_404)


@pytest.mark.unit
def test_date_window_labels():
    d = dt.date(2026, 6, 22)
    labels = db.date_window_labels(d, 3)
    assert labels == ["2026-06-20", "2026-06-21", "2026-06-22"]
    labels7 = db.date_window_labels(d, 1)
    assert labels7 == ["2026-06-22"]


@pytest.mark.unit
def test_clients_for_select_exists():
    assert hasattr(db, "clients_for_select")
    assert callable(db.clients_for_select)
