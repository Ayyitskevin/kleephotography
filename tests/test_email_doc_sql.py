"""The studio email route's table name is an allowlist, not the path segment.

CodeQL py/sql-injection traces ``kind`` on ``POST /admin/studio/{kind}/{doc_id}/email``
through ``db.one``. A kind outside the three studio doc tables is rejected
before any SQL is built; a real kind is a literal in the statement and the
document id stays a bound parameter.
"""

from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import db
from app.admin.emails import KINDS, _doc_table
from app.main import app

_FORM = {"to": "a@b.test", "subject": "Invoice", "message": "Link inside."}


@pytest.mark.unit
def test_doc_table_is_the_kinds_allowlist():
    assert {kind: _doc_table(kind) for kind in KINDS} == {kind: kind for kind in KINDS}


@pytest.mark.unit
@pytest.mark.parametrize(
    "kind",
    ["clients", "sqlite_master", "invoices;DROP TABLE clients--", "proposals' OR '1'='1"],
)
def test_doc_table_rejects_anything_else(kind):
    with pytest.raises(HTTPException) as exc:
        _doc_table(kind)
    assert exc.value.status_code == 404


@pytest.fixture
def admin_client():
    with TestClient(app) as client:
        response = client.post(
            "/admin/login",
            data={"password": "test-pw"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield client


def _spy_one(monkeypatch):
    seen: list[tuple[str, tuple]] = []
    real = db.one

    def spy(sql, params=()):
        seen.append((sql, params))
        return real(sql, params)

    monkeypatch.setattr(db, "one", spy)
    return seen


@pytest.mark.integration
def test_known_kind_queries_its_own_table_with_a_bound_id(admin_client, monkeypatch):
    seen = _spy_one(monkeypatch)
    response = admin_client.post(
        "/admin/studio/invoices/999999/email",
        data=_FORM,
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert ("SELECT * FROM invoices WHERE id=?", (999999,)) in seen
    assert all("999999" not in sql for sql, _params in seen)


@pytest.mark.integration
@pytest.mark.parametrize(
    "kind",
    ["clients", "sqlite_master", "invoices;DROP TABLE clients--", "proposals' OR '1'='1"],
)
def test_rejected_kind_never_reaches_sql(admin_client, monkeypatch, kind):
    seen = _spy_one(monkeypatch)
    response = admin_client.post(
        "/admin/studio/" + quote(kind, safe="") + "/1/email",
        data=_FORM,
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert all(kind not in sql and f"FROM {kind}" not in sql for sql, _params in seen)
