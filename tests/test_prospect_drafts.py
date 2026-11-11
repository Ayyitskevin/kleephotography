"""Outreach preparation on disposable SQLite, without lifespan/provider workers."""

import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import audit, config, db, ratelimit, security
from app.admin import prospect_drafts
from app.main import app

pytestmark = pytest.mark.integration
ROOT = "/admin/studio/prospects"


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "drafts.db")
    monkeypatch.setattr(config, "SITE_NAME", "Fixture Studio")
    db.migrate()
    ratelimit._hits.clear()
    c = TestClient(app)
    assert c.post("/admin/login", data={"password": config.ADMIN_PASSWORD}).status_code == 200
    yield c
    c.close()


def prospect(
    label="Synthetic bistro", name="Fixture Chef", email="chef@fixture.example", phone="8285550123"
):
    return db.run(
        "INSERT INTO prospects (business,contact_name,email,phone,source,qualification,notes,specialty,business_key,contact_key) "
        "VALUES (?,?,?,?, 'Synthetic research', 'potential_fit', 'PRIVATE research; do not include automatically', 'fb', ?, ?)",
        (label, name, email, phone, label.casefold(), name.casefold()),
    )


def event(
    pid, direction="out", outcome="sent", channel="email", notes="PRIVATE conversation notes"
):
    return db.run(
        "INSERT INTO prospect_contact_events (prospect_id,operation_key,request_digest,channel,direction,outcome,occurred_on,notes) VALUES (?,?,?,?,?,?,'2030-04-15',?)",
        (pid, str(uuid.uuid4()), "fixture", channel, direction, outcome, notes),
    )


def inputs(**extra):
    return (
        dict(
            channel="email",
            purpose="introduction",
            reference_event_id="",
            personalization="",
            next_step="",
            operation_key=str(uuid.uuid4()),
        )
        | extra
    )


def preview(admin, pid, **extra):
    response = admin.post(f"{ROOT}/{pid}/drafts/preview", data=inputs(**extra))
    assert response.status_code == 200, response.text
    return response


def create(admin, pid, **extra):
    response = preview(admin, pid, **extra)
    values = response.context["values"]
    saved = admin.post(
        f"{ROOT}/{pid}/drafts",
        data={
            "token": response.context["token"],
            "subject": values["subject"],
            "body": values["body"],
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    return admin.get(saved.headers["location"])


def edit_values(page, **extra):
    return {
        "token": page.context["token"],
        "subject": page.context["values"]["subject"],
        "body": page.context["values"]["body"],
    } | extra


def effects():
    tables = (
        "prospects",
        "tasks",
        "prospect_contact_events",
        "clients",
        "projects",
        "inquiries",
        "messages",
        "jobs",
        "proposals",
        "contracts",
        "invoices",
        "payments",
        "bookings",
        "emails_log",
    )
    return {t: [dict(r) for r in db.all_(f"SELECT * FROM {db.ident(t, tables)}")] for t in tables}


def drafts():
    return [dict(r) for r in db.all_("SELECT * FROM prospect_message_drafts ORDER BY id")]


@pytest.mark.parametrize("channel", ["email", "sms"])
@pytest.mark.parametrize("purpose", ["introduction", "follow_up", "conversation"])
def test_starters_use_explicit_context_and_leave_private_notes_out(admin, channel, purpose):
    pid = prospect()
    eid = event(
        pid,
        "in" if purpose == "conversation" else "out",
        "reply_received" if purpose == "conversation" else "sent",
    )
    before = effects()
    response = preview(
        admin,
        pid,
        channel=channel,
        purpose=purpose,
        reference_event_id="" if purpose == "introduction" else str(eid),
        personalization="The seasonal menu is the focus.",
    )
    text = response.context["values"]["body"]
    assert "Fixture Chef" in text and "The seasonal menu is the focus." in text
    assert "PRIVATE" not in text and text.count("?") == 1
    assert ("Thanks for your reply." in text) == (purpose == "conversation")
    assert (response.context["values"]["subject"] == "") == (channel == "sms")
    assert effects() == before and not drafts()
    assert not db.all_("SELECT * FROM audit_log")


def test_blank_contact_and_specialty_do_not_invent_a_name_or_niche(admin):
    pid = prospect(name="")
    db.run("UPDATE prospects SET specialty='' WHERE id=?", (pid,))
    body = preview(admin, pid).context["values"]["body"]
    assert body.startswith("Hi,") and "about photography for Synthetic bistro" in body
    assert "food & beverage" not in body


def test_selected_call_conversation_uses_conversation_wording(admin):
    pid = prospect()
    eid = event(pid, outcome="conversation", channel="call")
    body = preview(admin, pid, purpose="conversation", reference_event_id=str(eid)).context[
        "values"
    ]["body"]
    assert "Thanks for the conversation." in body and "your reply" not in body


@pytest.mark.parametrize(
    "changes",
    [
        {"channel": "fax"},
        {"purpose": "campaign"},
        {"operation_key": "bad"},
        {"purpose": "follow_up"},
        {"purpose": "conversation"},
        {"personalization": "a" * 501},
        {"next_step": "a" * 251},
        {"personalization": "bad\x00text"},
    ],
)
def test_invalid_setup_retains_entries_and_does_not_write(admin, changes):
    pid = prospect()
    values = inputs(**changes)
    response = admin.post(f"{ROOT}/{pid}/drafts/preview", data=values)
    assert response.status_code == 400
    assert response.context["values"] == values and not drafts()


@pytest.mark.parametrize(
    "purpose,direction,outcome,channel",
    [
        ("conversation", "out", "voicemail", "call"),
        ("conversation", "out", "sent", "email"),
        ("follow_up", "in", "reply_received", "email"),
        ("introduction", "out", "sent", "email"),
    ],
)
def test_a_contact_attempt_cannot_become_a_conversation(
    admin, purpose, direction, outcome, channel
):
    pid = prospect()
    eid = event(pid, direction, outcome, channel)
    response = admin.post(
        f"{ROOT}/{pid}/drafts/preview", data=inputs(purpose=purpose, reference_event_id=str(eid))
    )
    assert response.status_code == 400 and not drafts()


def test_reference_contact_must_belong_to_prospect(admin):
    pid, other = prospect(), prospect("Another fixture")
    eid = event(other)
    response = admin.post(
        f"{ROOT}/{pid}/drafts/preview",
        data=inputs(purpose="follow_up", reference_event_id=str(eid)),
    )
    assert response.status_code == 409 and not drafts()


@pytest.mark.parametrize("field,channel", [("email", "email"), ("phone", "sms")])
def test_missing_channel_blocks_creation(admin, field, channel):
    pid = prospect(**{field: ""})
    response = admin.post(f"{ROOT}/{pid}/drafts/preview", data=inputs(channel=channel))
    assert response.status_code == 400 and "recipient" in response.text and not drafts()


def test_save_edit_copy_preserve_contact_and_workflow_state(admin):
    pid = prospect()
    before = effects()
    page = create(admin, pid)
    row = drafts()[0]
    url = f"{ROOT}/{pid}/drafts/{row['id']}"
    assert row["version"] == 1 and "PRIVATE" in row["context_json"]
    assert json.loads(row["context_json"])["state"]["prospect"]["email"] == "chef@fixture.example"
    assert (
        admin.post(
            url,
            data=edit_values(page, body="A reviewed message. One next step?"),
            follow_redirects=False,
        ).status_code
        == 303
    )
    page = admin.get(url)
    assert (
        drafts()[0]["version"] == 2 and drafts()[0]["body"] == "A reviewed message. One next step?"
    )
    saved = drafts()
    audit_before = [dict(r) for r in db.all_("SELECT * FROM audit_log")]
    copied = admin.post(url + "/copy", data=edit_values(page, body="Unsaved copy text?"))
    assert copied.status_code == 200 and copied.json()["body"] == "Unsaved copy text?"
    assert copied.json()["prospect_id"] == pid and copied.json()["draft_id"] == row["id"]
    assert drafts() == saved and effects() == before
    assert [dict(r) for r in db.all_("SELECT * FROM audit_log")] == audit_before
    assert copied.headers["cache-control"] == "no-store"
    assert admin.get(url).headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", ["email", "notes", "task", "contact", "handoff"])
def test_context_changes_after_preview_refuse_and_keep_original_recipient(admin, change):
    pid = prospect()
    p = preview(admin, pid)
    original = p.context["context"]
    if change == "email":
        db.run("UPDATE prospects SET email='replacement@fixture.example' WHERE id=?", (pid,))
    elif change == "notes":
        db.run("UPDATE prospects SET notes='Changed research' WHERE id=?", (pid,))
    elif change == "task":
        db.run(
            "INSERT INTO tasks (prospect_id,title,due_date) VALUES (?, 'New next action', '2030-04-16')",
            (pid,),
        )
    elif change == "contact":
        event(pid, "in", "reply_received")
    else:
        handoff(admin, pid)
    values = p.context["values"]
    r = admin.post(
        f"{ROOT}/{pid}/drafts",
        data={
            "token": p.context["token"],
            "subject": values["subject"],
            "body": "My unsaved text?",
        },
    )
    assert r.status_code == 409 and r.context["values"]["body"] == "My unsaved text?"
    assert r.context["context"] == original and not drafts()


@pytest.mark.parametrize("change", ["recipient", "not_fit", "not_interested", "contact", "project"])
def test_saved_draft_context_changes_disable_copy_and_preserve_text(admin, change):
    pid = prospect()
    if change == "project":
        handoff(admin, pid)
    page = create(admin, pid)
    row = drafts()[0]
    url = f"{ROOT}/{pid}/drafts/{row['id']}"
    if change == "recipient":
        db.run("UPDATE prospects SET phone='8285550987' WHERE id=?", (pid,))
    elif change == "not_fit":
        db.run("UPDATE prospects SET qualification='not_fit' WHERE id=?", (pid,))
    elif change == "not_interested":
        event(pid, "in", "not_interested")
    elif change == "contact":
        event(pid)
    else:
        db.run(
            "UPDATE projects SET title='Updated project' WHERE id=(SELECT project_id FROM prospect_handoffs WHERE prospect_id=?)",
            (pid,),
        )
    before = drafts()
    assert admin.post(url + "/copy", data=edit_values(page)).status_code == 409
    error = admin.post(url, data=edit_values(page, body="Keep these edits?"))
    assert error.status_code == 409 and "Keep these edits?" in error.text
    current = admin.get(url)
    assert current.context["stale"] and drafts() == before
    assert 'data-draft-copy="body" disabled' in current.text


def handoff(admin, pid):
    form = admin.get(f"{ROOT}/{pid}/handoff?client_id=new").context["values"]
    values = dict(form) | {
        "photography_need": "PRIVATE brief with requested planning date",
        "interest_confirmed": "yes",
        "target_date": "2030-05-10",
    }
    response = admin.post(f"{ROOT}/{pid}/handoff/review", data=values)
    assert response.status_code == 200
    assert (
        admin.post(
            f"{ROOT}/{pid}/handoff",
            data=values | {"review_token": response.context["review_token"]},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_project_entry_links_to_source_and_never_treats_target_as_booking(admin):
    pid = prospect()
    handoff(admin, pid)
    page = create(admin, pid)
    h = db.one("SELECT * FROM prospect_handoffs WHERE prospect_id=?", (pid,))
    assert f"{ROOT}/{pid}/drafts" in admin.get(f"/admin/studio/projects/{h['project_id']}").text
    assert "PRIVATE brief" in page.text and "Not a confirmed booking" in page.text
    assert "2030-05-10" not in drafts()[0]["body"]
    assert "PRIVATE" not in drafts()[0]["body"]


@pytest.mark.parametrize(
    "kind", ["missing", "tampered", "expired", "wrong_scope", "other_prospect"]
)
def test_creation_requires_its_original_preview_receipt(admin, monkeypatch, kind):
    pid = prospect()
    page = preview(admin, pid)
    token = page.context["token"]
    if kind == "missing":
        token = ""
    elif kind == "tampered":
        token = "x" + token
    elif kind == "expired":
        monkeypatch.setattr(prospect_drafts, "TOKEN_AGE", -1)
    elif kind == "wrong_scope":
        token = security.sign(json.dumps({"kind": "other", "prospect_id": pid}))
    else:
        pid = prospect("Other recipient")
    r = admin.post(
        f"{ROOT}/{pid}/drafts",
        data={"token": token, "subject": "Subject", "body": "Unsaved message"},
    )
    assert r.status_code == 409 and "Unsaved message" in r.text and not drafts()


def test_create_retry_and_key_reuse_never_duplicate_or_replace_draft(admin):
    pid = prospect()
    page = preview(admin, pid)
    data = {"token": page.context["token"], "subject": "Subject", "body": "Original text"}
    first = admin.post(f"{ROOT}/{pid}/drafts", data=data, follow_redirects=False)
    db.run("UPDATE prospects SET notes='Later research' WHERE id=?", (pid,))
    again = admin.post(f"{ROOT}/{pid}/drafts", data=data, follow_redirects=False)
    assert (
        first.status_code == again.status_code == 303
        and first.headers["location"] == again.headers["location"]
    )
    assert admin.post(f"{ROOT}/{pid}/drafts", data=data | {"body": "Different"}).status_code == 409
    assert len(drafts()) == 1 and drafts()[0]["body"] == "Original text"


def test_concurrent_create_and_edit_cannot_duplicate_or_lose_edits(admin):
    pid = prospect()
    page = preview(admin, pid)
    payload = {"token": page.context["token"], "subject": "Subject", "body": "Original"}
    cookies = dict(admin.cookies)

    def race(url, payloads):
        barrier = threading.Barrier(2)

        def attempt(data):
            c = TestClient(app)
            c.cookies.update(cookies)
            try:
                barrier.wait()
                return c.post(url, data=data, follow_redirects=False).status_code
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return sorted(pool.map(attempt, payloads))

    assert race(f"{ROOT}/{pid}/drafts", [payload, payload]) == [303, 303]
    assert len(drafts()) == 1
    url = f"{ROOT}/{pid}/drafts/{drafts()[0]['id']}"
    page = admin.get(url)
    assert race(url, [edit_values(page, body="Edit A"), edit_values(page, body="Edit B")]) == [
        303,
        409,
    ]
    assert drafts()[0]["body"] in {"Edit A", "Edit B"} and drafts()[0]["version"] == 2


def test_edit_retry_stale_form_and_cross_record_tokens(admin):
    pid = prospect()
    first = create(admin, pid)
    second = create(admin, pid)
    first_url = f"{ROOT}/{pid}/drafts/{first.context['draft']['id']}"
    second_url = f"{ROOT}/{pid}/drafts/{second.context['draft']['id']}"
    payload = edit_values(first, body="Edited body")
    for _ in range(2):
        assert admin.post(first_url, data=payload, follow_redirects=False).status_code == 303
    assert admin.post(first_url, data=payload | {"body": "Lost edit"}).status_code == 409
    assert admin.post(first_url + "/copy", data=payload).status_code == 409
    for suffix in ("", "/copy"):
        assert admin.post(second_url + suffix, data=payload).status_code == 409
    assert drafts()[0]["version"] == 2 and drafts()[1]["version"] == 1
    assert (
        len(db.all_("SELECT * FROM audit_log WHERE action='edit' AND entity_type='prospect_draft'"))
        == 1
    )


@pytest.mark.parametrize(
    "channel,subject,body",
    [
        ("email", "", "Body"),
        ("email", "Bad\nSubject", "Body"),
        ("email", "x" * 241, "Body"),
        ("email", "Subject", ""),
        ("email", "Subject", "x" * 4001),
        ("email", "Subject", "x\x00y"),
        ("sms", "Subject", "Body"),
        ("sms", "", "x" * 1601),
    ],
)
def test_message_validation_applies_to_create_edit_and_copy(admin, channel, subject, body):
    pid = prospect()
    p = preview(admin, pid, channel=channel)
    invalid = {"token": p.context["token"], "subject": subject, "body": body}
    assert admin.post(f"{ROOT}/{pid}/drafts", data=invalid).status_code == 400
    assert not drafts()
    page = create(admin, pid, channel=channel)
    url = f"{ROOT}/{pid}/drafts/{drafts()[0]['id']}"
    for suffix in ("", "/copy"):
        assert (
            admin.post(url + suffix, data=invalid | {"token": page.context["token"]}).status_code
            == 400
        )
    assert drafts()[0]["version"] == 1


def test_audit_failure_rolls_back_creation_and_edit(admin, monkeypatch):
    pid = prospect()
    page = preview(admin, pid)

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")

    with monkeypatch.context() as m:
        m.setattr(audit, "log", fail)
        with pytest.raises(RuntimeError, match="Synthetic audit"):
            admin.post(
                f"{ROOT}/{pid}/drafts",
                data={"token": page.context["token"], "subject": "Subject", "body": "Text"},
            )
    assert not drafts()
    page = create(admin, pid)
    before = drafts()
    monkeypatch.setattr(audit, "log", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit"):
        admin.post(
            f"{ROOT}/{pid}/drafts/{drafts()[0]['id']}", data=edit_values(page, body="Edited")
        )
    assert drafts() == before


def test_auth_origin_and_record_ownership_on_all_routes(admin):
    pid = prospect()
    page = create(admin, pid)
    did = page.context["draft"]["id"]
    other = prospect("Other prospect")
    urls = [
        f"{ROOT}/{pid}/drafts",
        f"{ROOT}/{pid}/drafts/preview",
        f"{ROOT}/{pid}/drafts/{did}",
        f"{ROOT}/{pid}/drafts/{did}/copy",
    ]
    before = drafts()
    anonymous = TestClient(app)
    try:
        for url in urls:
            assert (
                anonymous.post(url, data=edit_values(page), follow_redirects=False).status_code
                == 303
            )
            assert (
                admin.post(
                    url,
                    data=edit_values(page),
                    headers={"Origin": "https://other.example"},
                    follow_redirects=False,
                ).status_code
                == 403
            )
        for url in (urls[0], urls[2]):
            assert anonymous.get(url, follow_redirects=False).status_code == 303
    finally:
        anonymous.close()
    for suffix in ("", "/copy"):
        assert (
            admin.post(f"{ROOT}/{other}/drafts/{did}{suffix}", data=edit_values(page)).status_code
            == 404
        )
    assert admin.get(f"{ROOT}/{other}/drafts/{did}").status_code == 404
    assert admin.get(f"{ROOT}/999999/drafts").status_code == 404
    assert drafts() == before


class FormCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.ids = set()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            assert self.depth == 0
            self.depth += 1
        if "id" in attrs:
            assert attrs["id"] not in self.ids
            self.ids.add(attrs["id"])
        if tag == "script":
            self.scripts.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1
            assert self.depth == 0


@pytest.mark.parametrize("screening_room", [True, False])
def test_rendering_escapes_and_has_scoped_editor_and_nonce_in_both_styles(
    admin, monkeypatch, screening_room
):
    monkeypatch.setattr(config, "SCREENING_ROOM", screening_room)
    pid = prospect("Synthetic <script>business()</script>")
    page = create(admin, pid, personalization="</textarea><script>private()</script>")
    assert "<script>business()</script>" not in page.text
    assert "<script>private()</script>" not in page.text
    assert "&lt;script&gt;private()&lt;/script&gt;" in page.text
    assert f'data-prospect-id="{pid}"' in page.text
    for response in (
        admin.get(f"{ROOT}/{pid}/drafts"),
        page,
        admin.post(
            f"{ROOT}/{pid}/drafts/{page.context['draft']['id']}", data=edit_values(page, body="")
        ),
    ):
        parser = FormCheck()
        parser.feed(response.text)
        assert parser.depth == 0
        js = next(
            s for s in parser.scripts if s.get("src", "").startswith("/static/prospect_drafts.js")
        )
        assert js.get("nonce") and js["nonce"] in response.headers["content-security-policy"]


def test_saved_drafts_paginate_per_prospect(admin):
    pid = prospect()
    other = prospect("Other fixture")
    for n in range(22):
        create(admin, pid, personalization=f"Fixture message {n}.")
    create(admin, other)
    first = admin.get(f"{ROOT}/{pid}/drafts")
    second = admin.get(f"{ROOT}/{pid}/drafts?page=2")
    assert len(first.context["drafts"]) == 20 and first.context["has_next"]
    assert len(second.context["drafts"]) == 2 and not second.context["has_next"]
    assert {d["id"] for d in first.context["drafts"]}.isdisjoint(
        d["id"] for d in second.context["drafts"]
    )
    assert all(d["id"] != drafts()[-1]["id"] for d in first.context["drafts"])


def test_upgrade_preserves_existing_history_and_is_repeatable(tmp_path, monkeypatch):
    source = db.MIGRATIONS_DIR
    old = tmp_path / "before-drafts"
    old.mkdir()
    for path in source.glob("*.sql"):
        if path.name < "087_":
            shutil.copy(path, old / path.name)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "upgrade.db")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old)
    db.migrate()
    pid = prospect()
    event(pid)
    before = effects()
    monkeypatch.setattr(db, "MIGRATIONS_DIR", source)
    db.migrate()
    db.migrate()
    assert effects() == before and not drafts()
    assert not db.all_("PRAGMA foreign_key_check")
