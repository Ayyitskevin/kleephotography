"""Coverage for the six admin modules no test touched.

`app/admin/forms.py`, `audit.py`, `content.py`, `doc_templates.py`,
`reference.py` and `email_templates.py` had zero executed routes: the public
`/forms/{slug}` POST path is covered elsewhere, but the admin side that builds
and reads those forms was not, and neither was the audit trail an operator
reaches for during a dispute.

Each module gets: the admin gate, a GET that asserts the page actually renders
the rows it claims to, and — where the module writes — a CRUD round trip that
asserts the DATABASE changed. The suite shares one SQLite file with every other
test file, so every fixture cleans up after itself.
"""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import reference
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def admin_client():
    with TestClient(app) as client:
        response = client.post(
            "/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303
        yield client


@pytest.fixture
def form_id():
    fid = db.run(
        "INSERT INTO forms (slug, title, kind, active) VALUES (?,?,?,1)",
        ("untested-brief", "Restaurant Shoot Brief", "questionnaire"),
    )
    yield fid
    db.run("DELETE FROM forms WHERE id=?", (fid,))


@pytest.fixture
def client_id():
    cid = db.run(
        "INSERT INTO clients (name, company) VALUES (?,?)", ("Untested Chef", "Untested Kitchen")
    )
    yield cid
    db.run("DELETE FROM clients WHERE id=?", (cid,))


@pytest.fixture
def project_id(client_id):
    pid = db.run(
        "INSERT INTO projects (client_id, title) VALUES (?,?)", (client_id, "Autumn Menu Shoot")
    )
    yield pid
    db.run("DELETE FROM projects WHERE id=?", (pid,))


def _new_id(response, prefix: str) -> int:
    """Pull the row id out of a create route's 303 Location."""
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(prefix)
    return int(location[len(prefix) :].split("/")[0])


# ── admin gate ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/admin/forms",
        "/admin/audit",
        "/admin/audit.csv",
        "/admin/content",
        "/admin/templates",
        "/admin/reference",
        "/admin/studio/email-templates",
    ],
)
def test_pages_redirect_to_login_without_a_session(path):
    with TestClient(app) as anon:
        response = anon.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


# ── forms: the builder ───────────────────────────────────────────────────────


def test_forms_list_splits_leads_from_questionnaires_and_counts_submissions(admin_client, form_id):
    lead_id = db.run(
        "INSERT INTO forms (slug, title, kind) VALUES (?,?,?)",
        ("untested-lead", "Wedding Enquiry", "lead"),
    )
    try:
        db.run(
            "INSERT INTO form_submissions (form_id, name, email) VALUES (?,?,?)",
            (lead_id, "Dana", "dana@example.test"),
        )
        db.run(
            "INSERT INTO form_fields (form_id, label, ftype) VALUES (?,?,?)",
            (lead_id, "Venue", "short_text"),
        )
        page = admin_client.get("/admin/forms")
        assert page.status_code == 200

        # kind decides which section a form lands in
        after_new_form = page.text.split('data-section="lead"')[1]
        leads, quests = after_new_form.split('data-section="questionnaire"')
        assert "Wedding Enquiry" in leads
        assert "Restaurant Shoot Brief" not in leads
        assert "Restaurant Shoot Brief" in quests

        assert "1 field</span>" in leads
        assert f'/admin/forms/{lead_id}/submissions">1 submission<' in leads
        assert f'/admin/forms/{form_id}/submissions">0 submissions<' in quests
    finally:
        db.run("DELETE FROM forms WHERE id=?", (lead_id,))


def test_form_create_update_and_delete_round_trip(admin_client):
    created = admin_client.post(
        "/admin/forms",
        data={"title": "  Tasting Menu Brief  ", "kind": "questionnaire"},
        follow_redirects=False,
    )
    fid = _new_id(created, "/admin/forms/")
    try:
        row = db.one("SELECT * FROM forms WHERE id=?", (fid,))
        assert row["title"] == "Tasting Menu Brief"
        assert row["kind"] == "questionnaire"
        assert row["slug"] and row["active"] == 1

        updated = admin_client.post(
            f"/admin/forms/{fid}",
            data={"title": "Tasting Menu Questionnaire", "intro": "  Tell us about the menu.  "},
            follow_redirects=False,
        )
        assert updated.status_code == 303
        row = db.one("SELECT * FROM forms WHERE id=?", (fid,))
        assert row["title"] == "Tasting Menu Questionnaire"
        assert row["intro"] == "Tell us about the menu."
        # the checkbox is absent when unticked — the form must go dormant
        assert row["active"] == 0

        reactivated = admin_client.post(
            f"/admin/forms/{fid}",
            data={"title": "Tasting Menu Questionnaire", "active": "true"},
            follow_redirects=False,
        )
        assert reactivated.status_code == 303
        row = db.one("SELECT * FROM forms WHERE id=?", (fid,))
        assert (row["active"], row["intro"]) == (1, None)

        deleted = admin_client.post(f"/admin/forms/{fid}/delete", follow_redirects=False)
        assert deleted.status_code == 303
        assert deleted.headers["location"] == "/admin/forms"
        assert db.one("SELECT id FROM forms WHERE id=?", (fid,)) is None
    finally:
        db.run("DELETE FROM forms WHERE id=?", (fid,))


def test_form_create_rejects_a_blank_title_or_unknown_kind(admin_client):
    before = db.one("SELECT COUNT(*) AS n FROM forms")["n"]
    for data in ({"title": "   ", "kind": "lead"}, {"title": "Fine", "kind": "survey"}):
        response = admin_client.post("/admin/forms", data=data, follow_redirects=False)
        assert response.status_code == 400
    assert db.one("SELECT COUNT(*) AS n FROM forms")["n"] == before


def test_form_update_404s_for_a_missing_form(admin_client):
    response = admin_client.post(
        "/admin/forms/99887766", data={"title": "Ghost"}, follow_redirects=False
    )
    assert response.status_code == 404


def test_field_add_update_and_delete_round_trip(admin_client, form_id):
    added = admin_client.post(
        f"/admin/forms/{form_id}/fields",
        data={
            "label": "  Which service?  ",
            "ftype": "dropdown",
            "required": "true",
            "options": "Lunch\n  Dinner  \n\nBrunch\n",
        },
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert added.headers["location"] == f"/admin/forms/{form_id}"

    field = db.one("SELECT * FROM form_fields WHERE form_id=?", (form_id,))
    assert field["label"] == "Which service?"
    assert field["ftype"] == "dropdown"
    assert field["required"] == 1
    assert json.loads(field["options"]) == ["Lunch", "Dinner", "Brunch"]
    assert field["sort_order"] == 1

    # switching to a type without choices must drop the stale option list
    updated = admin_client.post(
        f"/admin/forms/fields/{field['id']}",
        data={"label": "Notes", "ftype": "long_text", "options": "Lunch\nDinner"},
        follow_redirects=False,
    )
    assert updated.status_code == 303
    field = db.one("SELECT * FROM form_fields WHERE id=?", (field["id"],))
    assert (field["label"], field["ftype"], field["options"], field["required"]) == (
        "Notes",
        "long_text",
        None,
        0,
    )

    deleted = admin_client.post(f"/admin/forms/fields/{field['id']}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert db.one("SELECT id FROM form_fields WHERE id=?", (field["id"],)) is None


def test_field_add_rejects_a_blank_label_or_unknown_type(admin_client, form_id):
    for data in (
        {"label": "  ", "ftype": "short_text"},
        {"label": "Fine", "ftype": "signature"},
    ):
        response = admin_client.post(
            f"/admin/forms/{form_id}/fields", data=data, follow_redirects=False
        )
        assert response.status_code == 400
    assert db.all_("SELECT id FROM form_fields WHERE form_id=?", (form_id,)) == []


def test_field_move_swaps_neighbours_and_no_ops_at_the_ends(admin_client, form_id):
    for label in ("First", "Second"):
        assert (
            admin_client.post(
                f"/admin/forms/{form_id}/fields",
                data={"label": label, "ftype": "short_text"},
                follow_redirects=False,
            ).status_code
            == 303
        )

    def order():
        return [
            r["label"]
            for r in db.all_(
                "SELECT label FROM form_fields WHERE form_id=? ORDER BY sort_order, id", (form_id,)
            )
        ]

    assert order() == ["First", "Second"]
    second = db.one("SELECT id FROM form_fields WHERE form_id=? AND label='Second'", (form_id,))
    assert (
        admin_client.post(
            f"/admin/forms/fields/{second['id']}/move", data={"dir": "up"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert order() == ["Second", "First"]

    # already top of the list: a no-op, not a crash or a duplicated sort_order
    assert (
        admin_client.post(
            f"/admin/forms/fields/{second['id']}/move", data={"dir": "up"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert order() == ["Second", "First"]

    bad = admin_client.post(
        f"/admin/forms/fields/{second['id']}/move", data={"dir": "sideways"}, follow_redirects=False
    )
    assert bad.status_code == 400
    assert order() == ["Second", "First"]


def test_form_builder_renders_fields_choices_and_the_public_link(admin_client, form_id):
    admin_client.post(
        f"/admin/forms/{form_id}/fields",
        data={"label": "Which service?", "ftype": "dropdown", "options": "Lunch\nDinner"},
        follow_redirects=False,
    )
    slug = db.one("SELECT slug FROM forms WHERE id=?", (form_id,))["slug"]

    page = admin_client.get(f"/admin/forms/{form_id}")
    assert page.status_code == 200
    assert "Restaurant Shoot Brief" in page.text
    assert "Which service?" in page.text
    assert "Dropdown" in page.text
    assert "Lunch, Dinner" in page.text
    assert f"/forms/{slug}" in page.text


def test_form_submissions_page_renders_the_stored_answers(admin_client, form_id):
    field_id = db.run(
        "INSERT INTO form_fields (form_id, label, ftype, sort_order) VALUES (?,?,?,1)",
        (form_id, "Which service?", "dropdown"),
    )
    db.run(
        "INSERT INTO form_submissions (form_id, name, email, data) VALUES (?,?,?,?)",
        (
            form_id,
            "Dana Chef",
            "dana@example.test",
            json.dumps({str(field_id): ["Lunch", "Dinner"]}),
        ),
    )
    db.run(
        "INSERT INTO form_submissions (form_id, name, email, data) VALUES (?,?,?,?)",
        (form_id, "Sam Sous", "sam@example.test", "not json at all"),
    )

    page = admin_client.get(f"/admin/forms/{form_id}/submissions")
    assert page.status_code == 200
    assert "Dana Chef" in page.text
    assert "dana@example.test" in page.text
    assert "Which service?" in page.text
    assert "Lunch, Dinner" in page.text
    # an unparseable payload must not take the whole page down
    assert "Sam Sous" in page.text


def test_form_delete_takes_its_fields_and_submissions_with_it(admin_client, form_id):
    field_id = db.run(
        "INSERT INTO form_fields (form_id, label, ftype) VALUES (?,?,?)",
        (form_id, "Which service?", "short_text"),
    )
    sub_id = db.run("INSERT INTO form_submissions (form_id, name) VALUES (?,?)", (form_id, "Dana"))

    assert (
        admin_client.post(f"/admin/forms/{form_id}/delete", follow_redirects=False).status_code
        == 303
    )
    assert db.one("SELECT id FROM form_fields WHERE id=?", (field_id,)) is None
    assert db.one("SELECT id FROM form_submissions WHERE id=?", (sub_id,)) is None


# ── audit: the evidence trail ────────────────────────────────────────────────


@pytest.fixture
def audit_events():
    """Four events, one per category. Dated far ahead so they sort to the top of
    the newest-first window whatever else the session has logged."""
    seeded = [
        ("invoice", 990001, "update", "kevin", '{"total_cents": [100000, 125000]}'),
        ("proposal", 990002, "create", None, '{"title": "Autumn Menu"}'),
        ("gallery", 990003, "soft_delete", None, None),
        ("google_calendar", 990004, "connect", None, "not json"),
    ]
    ids = [
        db.run(
            """INSERT INTO audit_log (entity_type, entity_id, action, actor, diff_json, created_at)
               VALUES (?,?,?,COALESCE(?,'admin'),?,?)""",
            (etype, eid, action, actor, diff, f"2099-01-0{n + 1} 09:00:00"),
        )
        for n, (etype, eid, action, actor, diff) in enumerate(seeded)
    ]
    yield
    db.run(f"DELETE FROM audit_log WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))


def test_audit_page_titles_details_and_amounts_every_category(admin_client, audit_events):
    page = admin_client.get("/admin/audit")
    assert page.status_code == 200

    assert "Invoice updated" in page.text
    assert "invoice #990001 · changed: total_cents" in page.text
    assert "$1,250.00" in page.text  # the NEW value of an [old, new] diff
    assert "kevin" in page.text

    assert "Proposal created" in page.text
    assert "Gallery deleted" in page.text  # soft_delete reads as deleted
    assert "Google calendar connect" in page.text  # unmapped action passes through
    assert "google_calendar #990004" not in page.text  # underscores humanised


def test_audit_category_filter_narrows_the_feed(admin_client, audit_events):
    money = admin_client.get("/admin/audit?cat=money")
    assert money.status_code == 200
    assert "invoice #990001" in money.text
    assert "proposal #990002" not in money.text
    assert "gallery #990003" not in money.text

    access = admin_client.get("/admin/audit?cat=auth")
    assert "google calendar #990004" in access.text
    assert "invoice #990001" not in access.text


def test_audit_unknown_category_falls_back_to_all(admin_client, audit_events):
    page = admin_client.get("/admin/audit?cat=laundry")
    assert page.status_code == 200
    assert "invoice #990001" in page.text
    assert "proposal #990002" in page.text


def test_audit_csv_exports_the_events_as_parseable_rows(admin_client, audit_events):
    response = admin_client.get("/admin/audit.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "kleephotography_audit_log.csv" in response.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Time", "Event", "Detail", "Amount", "Actor"]
    by_detail = {r[2]: r for r in rows[1:]}

    invoice = by_detail["invoice #990001 · changed: total_cents"]
    assert invoice[1] == "Invoice updated"
    assert invoice[3] == "$1,250.00"
    assert invoice[4] == "kevin"

    assert by_detail["proposal #990002"][4] == "admin"
    assert by_detail["gallery #990003"][3] == ""  # no cents field, no amount


# ── content: the read-only brand/caption portal ──────────────────────────────


@pytest.fixture
def content_fixtures(client_id, project_id):
    db.run(
        """INSERT INTO brand_kits
           (client_id, label, stored, bytes, position, opacity, scale_pct, margin_pct)
           VALUES (?,?,?,?,?,?,?,?)""",
        (client_id, "Menu logo", "logo.png", 2048, "bl", 80, 30, 6),
    )
    db.run(
        "INSERT INTO brand_assets (client_id, filename, stored, bytes) VALUES (?,?,?,?)",
        (client_id, "menu-autumn.pdf", "stored-menu.pdf", 1500),
    )
    plan_id = db.run(
        "INSERT INTO recurring_plans (project_id, title) VALUES (?,?)",
        (project_id, "Monthly social retainer"),
    )
    db.run(
        """INSERT INTO retainer_captions
           (plan_id, period, label, body, status, ai_drafted, ai_model)
           VALUES (?,?,?,?,?,1,?)""",
        (plan_id, "2099-03", "Reel", "Braised short rib, twelve hours.", "approved", "odysseus"),
    )
    yield


def test_content_portal_renders_the_real_kit_asset_and_caption(
    admin_client, client_id, content_fixtures
):
    page = admin_client.get(f"/admin/content?client={client_id}")
    assert page.status_code == 200
    assert "Scoped to Untested Kitchen" in page.text
    assert "Menu logo" in page.text
    assert "Bottom left" in page.text  # 9-grid code humanised
    assert "80%" in page.text  # overlay opacity, straight off the kit row
    assert "menu-autumn.pdf" in page.text
    assert "1 KB" in page.text  # 1500 bytes, humanised
    assert "Braised short rib, twelve hours." in page.text
    assert "Monthly social retainer" in page.text
    assert "Approved" in page.text


def test_content_switch_scopes_to_one_client(admin_client, client_id, content_fixtures):
    other_id = db.run("INSERT INTO clients (name, company) VALUES (?,?)", ("Other", "Other Co"))
    try:
        db.run(
            "INSERT INTO brand_assets (client_id, filename, stored, bytes) VALUES (?,?,?,?)",
            (other_id, "other-brand.pdf", "stored-other.pdf", 900),
        )
        scoped = admin_client.get(f"/admin/content?client={client_id}")
        assert "menu-autumn.pdf" in scoped.text
        assert "other-brand.pdf" not in scoped.text

        everyone = admin_client.get("/admin/content")
        assert "Across every client" in everyone.text
        assert "menu-autumn.pdf" in everyone.text
        assert "other-brand.pdf" in everyone.text
    finally:
        db.run("DELETE FROM clients WHERE id=?", (other_id,))


@pytest.mark.parametrize("value", ["99887766", "", "not-an-id"])
def test_content_ignores_an_unusable_client_id(admin_client, client_id, content_fixtures, value):
    page = admin_client.get(f"/admin/content?client={value}")
    assert page.status_code == 200
    assert "Across every client" in page.text
    assert "menu-autumn.pdf" in page.text


# ── doc_templates: the template shelf ────────────────────────────────────────


def test_templates_gallery_lists_presets_contracts_and_open_projects(admin_client, project_id):
    page = admin_client.get("/admin/templates")
    assert page.status_code == 200
    assert "Photography — Starter" in page.text
    assert "Unilateral NDA" in page.text
    assert "Autumn Menu Shoot" in page.text

    db.run("UPDATE projects SET status='archived' WHERE id=?", (project_id,))
    page = admin_client.get("/admin/templates")
    assert "Autumn Menu Shoot" not in page.text


def test_use_template_creates_a_populated_proposal_draft(admin_client, project_id):
    response = admin_client.post(
        "/admin/templates/use",
        data={"project_id": project_id, "doc_type": "proposal", "key": "photo_starter"},
        follow_redirects=False,
    )
    did = _new_id(response, "/admin/studio/proposals/")
    row = db.one("SELECT * FROM proposals WHERE id=?", (did,))
    assert row["project_id"] == project_id
    assert row["title"] == "Photography — Starter — Autumn Menu Shoot"
    assert row["total_cents"] == 75000
    assert json.loads(row["line_items"])
    assert row["status"] == "draft"


def test_use_template_rejects_an_unknown_doc_type(admin_client, project_id):
    before = db.one("SELECT COUNT(*) AS n FROM proposals WHERE project_id=?", (project_id,))["n"]
    response = admin_client.post(
        "/admin/templates/use",
        data={"project_id": project_id, "doc_type": "moodboard", "key": ""},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert (
        db.one("SELECT COUNT(*) AS n FROM proposals WHERE project_id=?", (project_id,))["n"]
        == before
    )


def test_use_template_404s_for_a_missing_project(admin_client):
    response = admin_client.post(
        "/admin/templates/use",
        data={"project_id": 99887766, "doc_type": "proposal", "key": "photo_starter"},
        follow_redirects=False,
    )
    assert response.status_code == 404


# ── reference: the playbook shelf ────────────────────────────────────────────


def test_reference_renders_the_playbook_pricing_and_shoot_kit(admin_client):
    page = admin_client.get("/admin/reference")
    assert page.status_code == 200

    assert "Consultation Call" in page.text
    assert "Archived" in page.text
    assert reference.SHOOT_KIT[0] in page.text

    for row in reference._pricing_rows():
        assert row["item"] in page.text
        assert row["price"] in page.text
    # derived from the real proposal presets, not a hard-coded price list
    assert "from $750" in page.text


# ── email templates: the send-form snippet library ───────────────────────────


def test_email_template_create_update_and_soft_delete_round_trip(admin_client):
    created = admin_client.post(
        "/admin/studio/email-templates",
        data={
            "name": "  Proposal ready  ",
            "subject": "  Your proposal from {site_name}  ",
            "body": "Hi {first_name}, your proposal is ready: {doc_url}",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/admin/studio/email-templates"

    row = db.one("SELECT * FROM email_templates ORDER BY id DESC LIMIT 1")
    tid = row["id"]
    try:
        assert row["name"] == "Proposal ready"
        assert row["subject"] == "Your proposal from {site_name}"
        assert row["deleted_at"] is None

        page = admin_client.get("/admin/studio/email-templates")
        assert page.status_code == 200
        assert "Proposal ready" in page.text
        assert "{first_name}" in page.text  # merge-field crib stays on the page

        updated = admin_client.post(
            f"/admin/studio/email-templates/{tid}",
            data={
                "name": "Proposal ready — v2",
                "subject": "Your proposal",
                "body": "Hi {first_name}, have a look: {doc_url}",
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303
        row = db.one("SELECT * FROM email_templates WHERE id=?", (tid,))
        assert row["name"] == "Proposal ready — v2"
        assert row["subject"] == "Your proposal"

        deleted = admin_client.post(
            f"/admin/studio/email-templates/{tid}/delete", follow_redirects=False
        )
        assert deleted.status_code == 303
        row = db.one("SELECT * FROM email_templates WHERE id=?", (tid,))
        assert row["deleted_at"] is not None  # soft delete: the row survives
        assert "Proposal ready — v2" not in admin_client.get("/admin/studio/email-templates").text
    finally:
        db.run("DELETE FROM email_templates WHERE id=?", (tid,))


@pytest.mark.parametrize(
    "data",
    [
        {"name": "  ", "subject": "S", "body": "B"},
        {"name": "N", "subject": "  ", "body": "B"},
        {"name": "N", "subject": "S", "body": "   "},
    ],
)
def test_email_template_create_requires_name_subject_and_body(admin_client, data):
    before = db.one("SELECT COUNT(*) AS n FROM email_templates")["n"]
    response = admin_client.post("/admin/studio/email-templates", data=data, follow_redirects=False)
    assert response.status_code == 400
    assert db.one("SELECT COUNT(*) AS n FROM email_templates")["n"] == before


def test_email_template_update_404s_once_soft_deleted(admin_client):
    tid = db.run(
        "INSERT INTO email_templates (name, subject, body, deleted_at) VALUES (?,?,?,?)",
        ("Gone", "S", "B", "2026-01-01 00:00:00"),
    )
    try:
        response = admin_client.post(
            f"/admin/studio/email-templates/{tid}",
            data={"name": "Back", "subject": "S", "body": "B"},
            follow_redirects=False,
        )
        assert response.status_code == 404
        assert db.one("SELECT name FROM email_templates WHERE id=?", (tid,))["name"] == "Gone"
    finally:
        db.run("DELETE FROM email_templates WHERE id=?", (tid,))
