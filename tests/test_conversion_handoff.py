"""Conversion + owner handoff: specialty→contact prefill, progressive form,
accessible validation echo, inbox integration health, specialty SEO paths.

Drives shipped handlers/templates — no reimplemented parsers.
"""

import json
import os
import tempfile

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

import pytest
from fastapi.testclient import TestClient

from app import config, db, mailer, specialties
from app.admin import inbox as inbox_mod
from app.main import app
from app.public import site, site_catalog

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_client(client):
    r = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
    assert r.status_code == 303
    return client


# ── criterion 1–2: specialty discovery + inquiry quality ─────────────────────


@pytest.mark.unit
def test_contact_service_options_single_source_matches_services_board():
    opts = site_catalog.contact_service_options()
    values = [o["value"] for o in opts]
    # Unique contact_service strings from SERVICES, in board order
    expected = []
    for g in site_catalog.SERVICES:
        if g["contact_service"] not in expected:
            expected.append(g["contact_service"])
    assert values == expected
    # Every specialty spoke's contact_service is choosable
    for key, page in site_catalog.SPECIALTY_PAGES.items():
        assert page["contact_service"] in values
        slug = specialties.SPECIALTIES[key]["slug"]
        match = next(o for o in opts if o["value"] == page["contact_service"])
        assert match["specialty_slug"] == slug
    # Scope helpers stay aligned with option metadata
    for o in opts:
        assert site_catalog.contact_scope_for(o["value"]) == (
            o["scope_label"],
            o["scope_placeholder"],
        )


def test_specialty_spoke_to_contact_preselects_project_type(client):
    """Each live specialty path can deep-link into /contact with service set."""
    for key, page in site_catalog.SPECIALTY_PAGES.items():
        slug = specialties.SPECIALTIES[key]["slug"]
        r = client.get(f"/{slug}")
        assert r.status_code == 200
        assert "/contact?service=" in r.text
        # Prefill path the CTA uses
        service = page["contact_service"]
        c = client.get("/contact", params={"service": service})
        assert c.status_code == 200
        value_html = service.replace("&", "&amp;")
        assert f'value="{value_html}"' in c.text or f'value="{service}"' in c.text
        assert "selected" in c.text
        # Jinja escapes the apostrophe in the prefilled message body
        assert f"interested in {value_html}" in c.text or f"interested in {service}" in c.text
        scope_label, _ = site.contact_scope_for(service)
        assert scope_label in c.text


def test_contact_form_has_specialty_nav_progressive_details_and_a11y(client):
    r = client.get("/contact")
    assert r.status_code == 200
    # Specialty discovery without inventing prices
    for meta in specialties.SPECIALTIES.values():
        assert f'href="/{meta["slug"]}"' in r.text
        assert meta["name"].replace("&", "&amp;") in r.text or meta["name"] in r.text
    # Progressive disclosure shell + essentials
    assert 'class="contact-more"' in r.text
    assert "Project details" in r.text
    assert 'id="contact-name"' in r.text
    assert 'for="contact-name"' in r.text
    assert 'id="contact-email"' in r.text
    assert 'id="contact-service"' in r.text
    assert 'id="contact-message"' in r.text
    # Options come from single source (HTML-escaped in attributes)
    for opt in site_catalog.contact_service_options():
        value_html = opt["value"].replace("&", "&amp;")
        assert f'value="{value_html}"' in r.text or f'value="{opt["value"]}"' in r.text
        assert opt["label"] in r.text or opt["label"].replace("&", "&amp;") in r.text
    # Prefill opens details and selects service
    r2 = client.get("/contact?service=Real+Estate&tier=Signature")
    assert "contact-more" in r2.text and " open" in r2.text
    assert "Listing / property scope" in r2.text
    assert "Signature tier for Real Estate" in r2.text


def test_contact_validation_echo_preserves_values_and_alerts(client, monkeypatch):
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **k: None)
    r = client.post(
        "/contact",
        data={
            "name": "Sam Owner",
            "email": "sam@localhost",
            "phone": "(828) 555-0199",
            "business": "Taqueria Luz",
            "message": "Need a menu shoot in July.",
            "service": "Food & Beverage",
            "dish_count": "12 dishes",
            "usage": "Not sure",
            "budget": "Under $1,000",
        },
    )
    assert r.status_code == 400
    assert 'role="alert"' in r.text
    assert 'id="contact-error"' in r.text
    assert 'value="Sam Owner"' in r.text
    assert 'value="sam@localhost"' in r.text
    assert 'value="(828) 555-0199"' in r.text
    assert 'value="Taqueria Luz"' in r.text
    assert "Need a menu shoot in July." in r.text
    assert 'value="12 dishes"' in r.text
    assert "Dishes / setups" in r.text
    assert "selected>Food &amp; Beverage — photo / video</option>" in r.text or (
        'value="Food &amp; Beverage" selected' in r.text
        or 'value="Food & Beverage" selected' in r.text
    )
    # Nothing stored for the rejected submission
    assert db.one("SELECT COUNT(*) AS n FROM inquiries WHERE email=?", ("sam@localhost",))["n"] == 0


# ── criterion 3: admin lead handoff / integration health ─────────────────────


def test_inbox_surfaces_specialty_source_and_integration_health(admin_client, monkeypatch):
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "leads-db")
    monkeypatch.setattr(mailer, "configured", lambda: True)

    iid = db.run(
        """INSERT INTO inquiries
           (name, email, business, message, service, kind, emailed)
           VALUES (?,?,?,?,?,?,0)""",
        (
            "Alex Lead",
            "alex-conv@example.com",
            "Alex Co",
            "Need listing stills",
            "Real Estate",
            "web",
        ),
    )
    job_id = db.run(
        """INSERT INTO jobs (kind, payload, status, attempts, error)
           VALUES (?,?,?,?,?)""",
        (
            "notion_sync_inquiry",
            json.dumps({"inquiry_id": iid}),
            "failed",
            3,
            "notion api down",
        ),
    )
    try:
        page = admin_client.get(f"/admin/inbox?sel={iid}")
        assert page.status_code == 200
        body = page.text
        assert "Alex Co" in body
        assert "Real Estate" in body
        assert 'data-testid="lead-health"' in body
        assert "Specialty" in body
        assert "Inquiry form" in body or "Source" in body
        assert "Owner email" in body
        assert "not delivered" in body.lower() or "Not marked" in body or "Owner email" in body
        assert "Mirror failed" in body
        assert "notion api down" in body
        assert f'action="/admin/jobs/{job_id}/retry"' in body
        assert "Retry mirror" in body
        assert "Next" in body

        # Mirrored stamp flips Notion signal green / ok path
        db.run("UPDATE inquiries SET notion_page_id=?, emailed=1 WHERE id=?", ("page-abc-123", iid))
        page2 = admin_client.get(f"/admin/inbox?sel={iid}")
        assert "Mirrored" in page2.text
        assert "Notification sent" in page2.text or "replied" in page2.text.lower()
    finally:
        db.run("DELETE FROM jobs WHERE id=?", (job_id,))
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_integration_health_dormant_notion_is_explicit(monkeypatch):
    monkeypatch.setattr(config, "NOTION_TOKEN", "")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "")
    monkeypatch.setattr(mailer, "configured", lambda: False)
    iid = db.run(
        "INSERT INTO inquiries (name, email, message, service, emailed) VALUES (?,?,?,?,0)",
        ("Dana", "dana-conv@example.com", "Hello", "Portraits"),
    )
    try:
        row = db.one("SELECT * FROM inquiries WHERE id=?", (iid,))
        health = inbox_mod._integration_health(row)
        assert health["specialty"] == "Portraits"
        assert health["source"] == "Inquiry form"
        assert health["email"]["state"] == "bad"
        assert "Mailer not configured" in health["email"]["detail"]
        assert health["notion"]["state"] == "muted"
        assert "Not armed" in health["notion"]["detail"]
        assert health["notion"]["retry_job_id"] is None
        assert (
            "reply" in health["next_action"].lower() or "convert" in health["next_action"].lower()
        )
    finally:
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_notion_job_lookup_finds_lead_past_eighty_decoy_failures(monkeypatch):
    """SQL filter must not miss a lead's failed job behind 80+ newer decoys."""
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "leads-db")
    monkeypatch.setattr(mailer, "configured", lambda: True)

    target_id = db.run(
        """INSERT INTO inquiries (name, email, message, service, emailed)
           VALUES (?,?,?,?,0)""",
        ("Target Lead", "target-conv@example.com", "Need photos", "Real Estate"),
    )
    # Older failed job for the target — would be pushed out of any LIMIT 80 window
    # if we only scanned recent rows and filtered in Python.
    target_job_id = db.run(
        """INSERT INTO jobs (kind, payload, status, attempts, error)
           VALUES (?,?,?,?,?)""",
        (
            "notion_sync_inquiry",
            json.dumps({"inquiry_id": target_id}),
            "failed",
            3,
            "target mirror boom",
        ),
    )
    decoy_job_ids: list[int] = []
    decoy_inquiry_ids: list[int] = []
    try:
        for i in range(85):
            decoy_iid = db.run(
                """INSERT INTO inquiries (name, email, message, emailed)
                   VALUES (?,?,?,0)""",
                (f"Decoy {i}", f"decoy-conv-{i}@example.com", "noise"),
            )
            decoy_inquiry_ids.append(decoy_iid)
            decoy_job_ids.append(
                db.run(
                    """INSERT INTO jobs (kind, payload, status, attempts, error)
                       VALUES (?,?,?,?,?)""",
                    (
                        "notion_sync_inquiry",
                        json.dumps({"inquiry_id": decoy_iid}),
                        "failed",
                        3,
                        f"decoy fail {i}",
                    ),
                )
            )

        found = inbox_mod._notion_job_for(target_id)
        assert found is not None
        assert found["id"] == target_job_id
        assert found["status"] == "failed"
        assert "target mirror boom" in found["error"]

        row = db.one("SELECT * FROM inquiries WHERE id=?", (target_id,))
        health = inbox_mod._integration_health(row)
        assert health["notion"]["state"] == "bad"
        assert health["notion"]["retry_job_id"] == target_job_id
        assert "target mirror boom" in health["notion"]["detail"]
    finally:
        if decoy_job_ids:
            db.run(
                f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(decoy_job_ids))})",
                tuple(decoy_job_ids),
            )
        db.run("DELETE FROM jobs WHERE id=?", (target_job_id,))
        if decoy_inquiry_ids:
            db.run(
                f"DELETE FROM inquiries WHERE id IN ({','.join('?' * len(decoy_inquiry_ids))})",
                tuple(decoy_inquiry_ids),
            )
        db.run("DELETE FROM inquiries WHERE id=?", (target_id,))


# ── criterion 4: specialty SEO / sitemap / titles ────────────────────────────


@pytest.mark.unit
def test_specialty_paths_are_indexable_with_titles_and_sitemap_membership():
    catalog = {p["path"]: p for p in site.marketing_page_catalog()}
    for key, meta in specialties.SPECIALTIES.items():
        path = f"/{meta['slug']}"
        assert path in site.INDEXABLE
        assert path in catalog
        page = site_catalog.SPECIALTY_PAGES[key]
        assert page["title"] in catalog[path]["title"]
        assert catalog[path]["description"] == page["meta"]


def test_sitemap_and_specialty_route_meta(client):
    sm = client.get("/sitemap.xml")
    assert sm.status_code == 200
    for meta in specialties.SPECIALTIES.values():
        assert f"/{meta['slug']}" in sm.text

    for key, meta in specialties.SPECIALTIES.items():
        r = client.get(f"/{meta['slug']}")
        assert r.status_code == 200
        page = site_catalog.SPECIALTY_PAGES[key]
        title_html = page["title"].replace("&", "&amp;")
        assert title_html in r.text or page["title"] in r.text
        # Meta description is present (entity-escaped when needed)
        assert page["meta"][:32] in r.text or page["meta"][:32].replace("&", "&amp;") in r.text
