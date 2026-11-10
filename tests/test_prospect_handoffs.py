"""Reviewed handoffs against isolated SQLite; no lifespan or outbound workers."""

import datetime as dt
import json
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import audit, config, db, ratelimit
from app.admin import prospect_handoffs, studio
from app.main import app

pytestmark = pytest.mark.integration
ROOT = "/admin/studio/prospects"


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "handoff.db")
    monkeypatch.setattr(config, "BRAND_DIR", tmp_path / "brand")
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2030, 4, 15))
    db.migrate()
    ratelimit._hits.clear()
    client = TestClient(app)
    assert client.post("/admin/login", data={"password": config.ADMIN_PASSWORD}).status_code == 200
    yield client
    client.close()


def prospect(label="Synthetic handoff bistro", **extra):
    row_id = db.run(
        "INSERT INTO prospects (business,contact_name,email,phone,source,qualification,notes,specialty,business_key,contact_key) "
        "VALUES (?, 'Fixture Chef', 'chef@fixture.example', '8285550123', 'Synthetic research', 'potential_fit', 'Original research', 'fb', ?, 'fixture chef')",
        (label, label.casefold()),
    )
    for field, value in extra.items():
        db.run(
            f"UPDATE prospects SET {db.ident(field, {'qualification', 'email', 'phone', 'notes'})}=? WHERE id=?",
            (value, row_id),
        )
    return row_id


def client(
    name="Existing contact", company="Existing business", email="other@fixture.example", phone=""
):
    return db.run(
        "INSERT INTO clients (name,company,email,phone,notes) VALUES (?,?,?,?, 'Preserve existing notes')",
        (name, company, email, phone),
    )


def task(pid):
    return db.run(
        "INSERT INTO tasks (prospect_id,title,due_date) VALUES (?, 'Discuss photography needs', '2030-04-15')",
        (pid,),
    )


def form(admin, pid, **changes):
    response = admin.get(f"{ROOT}/{pid}/handoff?client_id={changes.get('client_id', 'new')}")
    assert response.status_code == 200
    values = dict(response.context["values"])
    values.update(
        title="Seasonal menu photography",
        photography_need="Menu and interior photographs for a launch.",
        target_date="2030-05-10",
        interest_confirmed="yes",
    )
    if values["task_id"]:
        values["task_disposition"] = "keep"
    return values | changes


def reviewed(admin, pid, values):
    response = admin.post(f"{ROOT}/{pid}/handoff/review", data=values)
    assert response.status_code == 200, response.text
    assert response.context["review_token"]
    return values | {"review_token": response.context["review_token"]}


def save(admin, pid, values):
    return admin.post(f"{ROOT}/{pid}/handoff", data=values, follow_redirects=False)


def count(table):
    tables = {
        "clients",
        "projects",
        "prospects",
        "prospect_handoffs",
        "tasks",
        "prospect_contact_events",
        "audit_log",
        "inquiries",
        "proposals",
        "contracts",
        "invoices",
        "payments",
        "bookings",
        "messages",
        "jobs",
        "emails_log",
    }
    return db.one(f"SELECT COUNT(*) n FROM {db.ident(table, tables)}")["n"]


def unrelated():
    return {
        t: count(t)
        for t in (
            "inquiries",
            "proposals",
            "contracts",
            "invoices",
            "payments",
            "bookings",
            "messages",
            "jobs",
            "emails_log",
            "prospect_contact_events",
        )
    }


def test_review_is_read_only_and_exactly_reviewed_new_opportunity_is_unbooked(admin):
    pid = prospect()
    before = unrelated()
    saved_prospect = dict(db.one("SELECT * FROM prospects WHERE id=?", (pid,)))
    values = form(admin, pid)
    n_clients, n_projects, n_audit = count("clients"), count("projects"), count("audit_log")
    payload = reviewed(admin, pid, values)
    assert (count("clients"), count("projects"), count("audit_log")) == (
        n_clients,
        n_projects,
        n_audit,
    )
    result = save(admin, pid, payload)
    assert result.status_code == 303
    h = db.one("SELECT * FROM prospect_handoffs WHERE prospect_id=?", (pid,))
    p = db.one("SELECT * FROM projects WHERE id=?", (h["project_id"],))
    c = db.one("SELECT * FROM clients WHERE id=?", (h["client_id"],))
    assert c["company"] == saved_prospect["business"] and c["phone"] == saved_prospect["phone"]
    assert p["status"] == "inquiry_received" and p["shoot_date"] is None
    assert p["notes"] == values["photography_need"]
    assert h["target_date"] == values["target_date"] and h["specialty"] == "fb"
    assert json.loads(h["prospect_snapshot"]) == saved_prospect
    assert dict(db.one("SELECT * FROM prospects WHERE id=?", (pid,))) == saved_prospect
    assert unrelated() == before
    for url in (
        result.headers["location"],
        f"/admin/studio/clients/{c['id']}",
        f"{ROOT}/{pid}",
        f"{ROOT}/{pid}/handoff",
    ):
        page = admin.get(url)
        assert page.status_code == 200
        assert f"{ROOT}/{pid}" in page.text
    assert count("clients") == n_clients + 1 and count("projects") == n_projects + 1


def test_reuse_existing_client_and_project_preserves_every_field_and_other_tasks(admin):
    pid = prospect()
    cid = client()
    project_id = db.run(
        "INSERT INTO projects (client_id,title,status,notes,shoot_date) VALUES (?, 'Existing session', 'proposal_sent', 'Existing brief', '2030-06-01')",
        (cid,),
    )
    tid = db.run(
        "INSERT INTO tasks (project_id,title) VALUES (?, 'Existing project task')", (project_id,)
    )
    original_client = dict(db.one("SELECT * FROM clients WHERE id=?", (cid,)))
    original_project = dict(db.one("SELECT * FROM projects WHERE id=?", (project_id,)))
    original_task = dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,)))
    values = form(
        admin, pid, client_id=str(cid), project_id=str(project_id), title="Ignored new title"
    )
    before = (count("clients"), count("projects"), count("tasks"), unrelated())
    assert save(admin, pid, reviewed(admin, pid, values)).status_code == 303
    assert (count("clients"), count("projects"), count("tasks"), unrelated()) == before
    assert dict(db.one("SELECT * FROM clients WHERE id=?", (cid,))) == original_client
    assert dict(db.one("SELECT * FROM projects WHERE id=?", (project_id,))) == original_project
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,))) == original_task


@pytest.mark.parametrize("matched_by", ["email", "phone", "business"])
def test_duplicate_clients_require_explicit_reuse_on_review_and_final_save(admin, matched_by):
    pid = prospect()
    values = form(admin, pid)
    payload = reviewed(admin, pid, values)
    data = (
        {"email": " CHEF@FIXTURE.EXAMPLE "}
        if matched_by == "email"
        else {"phone": "+1 (828) 555-0123"}
        if matched_by == "phone"
        else {"company": " SYNTHETIC   HANDOFF BISTRO "}
    )
    cid = client(**data)
    for suffix, data in (("/review", values), ("", payload)):
        response = admin.post(f"{ROOT}/{pid}/handoff{suffix}", data=data, follow_redirects=False)
        assert response.status_code == 409
        assert "matching client" in response.text and count("prospect_handoffs") == 0
    page = admin.get(f"{ROOT}/{pid}/handoff")
    assert cid in [r["id"] for r in page.context["matches"]]
    chosen = form(admin, pid, client_id=str(cid))
    assert save(admin, pid, reviewed(admin, pid, chosen)).status_code == 303
    assert count("clients") == 1


@pytest.mark.parametrize("disposition", ["keep", "complete", "cancel"])
def test_pending_task_choice_is_explicit_and_history_survives(admin, disposition):
    pid = prospect()
    tid = task(pid)
    original = dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,)))
    values = form(
        admin, pid, task_disposition=disposition, task_note="Operator reviewed this action"
    )
    response = save(admin, pid, reviewed(admin, pid, values))
    assert response.status_code == 303
    current = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    if disposition == "keep":
        assert dict(current) == original
        page = admin.get(ROOT + "/follow-ups").context["prospects"]
        assert [r["id"] for r in page] == [pid]
        assert "Discuss photography needs" in admin.get(response.headers["location"]).text
    else:
        assert current["version"] == original["version"] + 1
        assert bool(current["done"]) == (disposition == "complete")
        assert bool(current["canceled_at"]) == (disposition == "cancel")
    assert admin.get(ROOT + "/follow-ups?filter=unplanned").context["total"] == 0
    assert current["prospect_id"] == pid and current["project_id"] is None
    assert count("prospect_contact_events") == 0
    assert "Linked project" in admin.get(ROOT).text


@pytest.mark.parametrize("mutation", ["prospect", "client", "project", "task", "new_task"])
def test_changes_after_review_are_refused_without_partial_effects(admin, mutation):
    pid = prospect()
    cid = client()
    project_id = db.run("INSERT INTO projects (client_id,title) VALUES (?, 'Existing')", (cid,))
    tid = None if mutation == "new_task" else task(pid)
    values = form(admin, pid, client_id=str(cid), project_id=str(project_id))
    payload = reviewed(admin, pid, values)
    if mutation == "prospect":
        db.run("UPDATE prospects SET notes='Research changed' WHERE id=?", (pid,))
    elif mutation == "client":
        db.run("UPDATE clients SET email='changed@fixture.example' WHERE id=?", (cid,))
    elif mutation == "project":
        db.run("UPDATE projects SET status='consultation_call' WHERE id=?", (project_id,))
    elif mutation == "task":
        db.run("UPDATE tasks SET due_date='2030-04-20',version=version+1 WHERE id=?", (tid,))
    else:
        task(pid)
    before = (count("clients"), count("projects"), count("audit_log"))
    result = save(admin, pid, payload)
    assert result.status_code == 409 and "preserved" in result.text
    assert (count("clients"), count("projects"), count("audit_log")) == before
    assert count("prospect_handoffs") == 0


@pytest.mark.parametrize(
    "change",
    [
        {"review_token": ""},
        {"review_token": "bad"},
        {"title": "Changed title"},
        {"target_date": "2030-06-10"},
        {"client_id": "99999"},
        {"task_disposition": "cancel", "task_note": "Changed decision"},
    ],
)
def test_commit_requires_review_of_exact_contents(admin, change):
    pid = prospect()
    task(pid)
    payload = reviewed(admin, pid, form(admin, pid)) | change
    assert save(admin, pid, payload).status_code == 409
    assert count("clients") == count("projects") == count("prospect_handoffs") == 0


def test_review_is_scoped_to_prospect_and_expires(admin, monkeypatch):
    first = prospect()
    second = prospect("Other synthetic bistro", email="second@fixture.example", phone="8285550144")
    payload = reviewed(admin, first, form(admin, first))
    assert save(admin, second, payload).status_code == 409
    monkeypatch.setattr(prospect_handoffs, "REVIEW_MAX_AGE", -1)
    assert save(admin, first, payload).status_code == 409
    assert count("clients") == count("projects") == 0


def test_exact_retry_survives_later_edits_and_never_repeats_task_or_audit(admin):
    pid = prospect()
    tid = task(pid)
    payload = reviewed(
        admin, pid, form(admin, pid, task_disposition="complete", task_note="Handled")
    )
    result = save(admin, pid, payload)
    assert result.status_code == 303
    db.run("UPDATE prospects SET notes='Later research' WHERE id=?", (pid,))
    next_task = task(pid)
    before = (count("clients"), count("projects"), count("tasks"), count("audit_log"))
    again = save(admin, pid, payload)
    assert again.status_code == 303 and again.headers["location"] == result.headers["location"]
    assert (count("clients"), count("projects"), count("tasks"), count("audit_log")) == before
    assert db.one("SELECT done FROM tasks WHERE id=?", (tid,))["done"] == 1
    assert db.one("SELECT done FROM tasks WHERE id=?", (next_task,))["done"] == 0


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_saves_create_one_handoff_and_one_project(admin, same_request):
    pid = prospect()
    values = form(admin, pid)
    first = reviewed(admin, pid, values)
    second = (
        first
        if same_request
        else reviewed(admin, pid, values | {"operation_key": uuid.uuid4().hex})
    )
    barrier = threading.Barrier(2)
    cookies = dict(admin.cookies)

    def submit(payload):
        c = TestClient(app)
        c.cookies.update(cookies)
        try:
            barrier.wait()
            return save(c, pid, payload).status_code
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(submit, (first, second)))
    assert statuses == ([303, 303] if same_request else [303, 409])
    assert count("clients") == count("projects") == count("prospect_handoffs") == 1
    assert db.one("SELECT COUNT(*) n FROM audit_log WHERE action='handoff'")["n"] == 1


@pytest.mark.parametrize("entity", ["client", "project", "prospect_task", "prospect"])
def test_audit_failure_rolls_back_client_project_task_and_receipt(admin, monkeypatch, entity):
    pid = prospect()
    tid = task(pid)
    payload = reviewed(
        admin, pid, form(admin, pid, task_disposition="complete", task_note="Handled")
    )
    original = dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,)))
    old_log = audit.log

    def fail(con, kind, *args, **kwargs):
        if kind == entity:
            raise RuntimeError("Synthetic audit failure")
        return old_log(con, kind, *args, **kwargs)

    monkeypatch.setattr(audit, "log", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        save(admin, pid, payload)
    assert (
        count("clients")
        == count("projects")
        == count("prospect_handoffs")
        == count("audit_log")
        == 0
    )
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,))) == original


@pytest.mark.parametrize(
    "changes",
    [
        {"photography_need": ""},
        {"photography_need": "x" * 4001},
        {"title": ""},
        {"title": "x" * 201},
        {"target_date": "2030-02-30"},
        {"target_date": "20300510"},
        {"specialty": "invalid"},
        {"interest_confirmed": ""},
        {"client_id": "0"},
        {"client_id": "new", "project_id": "1"},
        {"operation_key": "short"},
    ],
)
def test_invalid_review_writes_nothing(admin, changes):
    pid = prospect()
    response = admin.post(f"{ROOT}/{pid}/handoff/review", data=form(admin, pid, **changes))
    assert response.status_code == 400
    assert count("clients") == count("projects") == count("prospect_handoffs") == 0


def test_not_fit_and_missing_prospect_are_refused(admin):
    pid = prospect(qualification="not_fit")
    assert admin.post(f"{ROOT}/{pid}/handoff/review", data=form(admin, pid)).status_code == 400
    assert admin.get(f"{ROOT}/99999/handoff").status_code == 404
    values = form(admin, pid)
    assert admin.post(f"{ROOT}/99999/handoff/review", data=values).status_code == 404


@pytest.mark.parametrize("problem", ["wrong_client", "closed", "archived", "deleted"])
def test_existing_project_must_belong_to_selected_client_and_be_open(admin, problem):
    pid = prospect()
    cid, other = client(), client(name="Different contact")
    project_id = db.run(
        "INSERT INTO projects (client_id,title,status) VALUES (?, 'Existing', ?)",
        (
            other if problem == "wrong_client" else cid,
            "project_closed"
            if problem == "closed"
            else "archived"
            if problem == "archived"
            else "inquiry_received",
        ),
    )
    if problem == "deleted":
        db.run("DELETE FROM projects WHERE id=?", (project_id,))
    response = admin.post(
        f"{ROOT}/{pid}/handoff/review",
        data=form(admin, pid, client_id=str(cid), project_id=str(project_id)),
    )
    assert response.status_code == 409 and count("prospect_handoffs") == 0


def test_pending_task_cannot_be_omitted_or_borrowed_from_another_prospect(admin):
    pid, other = prospect(), prospect("Other synthetic bistro")
    task(pid)
    other_task = task(other)
    for extra in (
        {"task_id": "", "task_version": "", "task_disposition": "none"},
        {"task_id": str(other_task)},
        {"task_disposition": "complete", "task_note": ""},
    ):
        response = admin.post(f"{ROOT}/{pid}/handoff/review", data=form(admin, pid, **extra))
        assert response.status_code in (400, 409)
    assert count("prospect_handoffs") == 0


def test_linked_client_force_delete_refuses_before_unlinking_or_removing_files(admin):
    pid = prospect()
    payload = reviewed(admin, pid, form(admin, pid))
    assert save(admin, pid, payload).status_code == 303
    h = db.one("SELECT * FROM prospect_handoffs")
    gid = db.run(
        "INSERT INTO galleries (title,slug,pin,client_id) VALUES ('Fixture','FixtureKeep','1234',?)",
        (h["client_id"],),
    )
    brand = config.BRAND_DIR / str(h["client_id"])
    brand.mkdir(parents=True, exist_ok=True)
    original = brand / "fixture.txt"
    original.write_text("Keep this synthetic file")
    for force in ("", "true"):
        response = admin.post(
            f"/admin/studio/clients/{h['client_id']}/delete",
            data={"force": force},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "handoff history" in response.text
    assert original.exists()
    assert (
        db.one("SELECT client_id FROM galleries WHERE id=?", (gid,))["client_id"] == h["client_id"]
    )
    assert db.one("SELECT 1 FROM projects WHERE id=?", (h["project_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        db.run("DELETE FROM projects WHERE id=?", (h["project_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        db.run("DELETE FROM prospects WHERE id=?", (pid,))
    other = client()
    with pytest.raises(sqlite3.IntegrityError):
        db.run("UPDATE projects SET client_id=? WHERE id=?", (other, h["project_id"]))


def test_unlinked_client_force_delete_retains_existing_behavior(admin):
    cid = client()
    db.run("INSERT INTO projects (client_id,title) VALUES (?, 'Disposable project')", (cid,))
    gid = db.run(
        "INSERT INTO galleries (title,slug,pin,client_id) VALUES ('Fixture','FixtureUnlink','1234',?)",
        (cid,),
    )
    assert (
        admin.post(f"/admin/studio/clients/{cid}/delete", follow_redirects=False).status_code == 400
    )
    assert (
        admin.post(
            f"/admin/studio/clients/{cid}/delete", data={"force": "true"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert db.one("SELECT client_id FROM galleries WHERE id=?", (gid,))["client_id"] is None
    assert count("clients") == count("projects") == 0


def test_client_id_cannot_be_reused_while_its_brand_files_are_removed(admin, monkeypatch):
    cid = client()
    brand = config.BRAND_DIR / str(cid)
    brand.mkdir(parents=True)
    (brand / "old.txt").write_text("Synthetic old asset")
    original_rmtree = shutil.rmtree
    checked = []

    def remove(path, **kwargs):
        con = sqlite3.connect(config.DB_PATH, timeout=0)
        try:
            # A competing creator must wait until cleanup and deletion finish.
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                con.execute("INSERT INTO clients (name) VALUES ('Competing client')")
            checked.append(True)
        finally:
            con.close()
        original_rmtree(path, **kwargs)

    monkeypatch.setattr(studio.shutil, "rmtree", remove)
    response = admin.post(
        f"/admin/studio/clients/{cid}/delete", data={"force": "true"}, follow_redirects=False
    )
    assert response.status_code == 303 and checked == [True] and not brand.exists()
    assert client(name="Replacement") == cid
    brand.mkdir(parents=True)
    fresh = brand / "new.txt"
    fresh.write_text("Synthetic new asset")
    assert fresh.exists()


def test_handoff_and_force_delete_serialize_without_partial_deletion(admin):
    pid = prospect()
    cid = client()
    gid = db.run(
        "INSERT INTO galleries (title,slug,pin,client_id) VALUES ('Fixture','FixtureRace','1234',?)",
        (cid,),
    )
    brand = config.BRAND_DIR / str(cid)
    brand.mkdir(parents=True, exist_ok=True)
    fixture = brand / "fixture.txt"
    fixture.write_text("Synthetic brand asset")
    payload = reviewed(admin, pid, form(admin, pid, client_id=str(cid)))
    barrier = threading.Barrier(2)
    cookies = dict(admin.cookies)

    def attempt(kind):
        c = TestClient(app)
        c.cookies.update(cookies)
        try:
            barrier.wait()
            return (
                save(c, pid, payload).status_code
                if kind == "handoff"
                else c.post(
                    f"/admin/studio/clients/{cid}/delete",
                    data={"force": "true"},
                    follow_redirects=False,
                ).status_code
            )
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        handoff_status, delete_status = list(pool.map(attempt, ("handoff", "delete")))
    if handoff_status == 303:
        assert delete_status == 400
        assert fixture.exists() and count("clients") == count("prospect_handoffs") == 1
        assert db.one("SELECT client_id FROM galleries WHERE id=?", (gid,))["client_id"] == cid
    else:
        assert handoff_status == 409 and delete_status == 303
        assert (
            not fixture.exists()
            and count("clients") == count("prospect_handoffs") == count("projects") == 0
        )
        assert db.one("SELECT client_id FROM galleries WHERE id=?", (gid,))["client_id"] is None


def test_two_prospects_cannot_race_to_create_duplicate_business_clients(admin):
    first = prospect()
    second = db.run(
        "INSERT INTO prospects (business,contact_name,source,qualification,business_key,contact_key) "
        "VALUES ('Synthetic handoff bistro', 'Second contact', 'Fixture', 'potential_fit', 'synthetic handoff bistro', 'second contact')"
    )
    payloads = [(pid, reviewed(admin, pid, form(admin, pid))) for pid in (first, second)]
    barrier = threading.Barrier(2)
    cookies = dict(admin.cookies)

    def attempt(pair):
        c = TestClient(app)
        c.cookies.update(cookies)
        try:
            barrier.wait()
            return save(c, pair[0], pair[1]).status_code
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, payloads)) == [303, 409]
    assert count("clients") == count("prospect_handoffs") == count("projects") == 1


def test_failed_handoff_constraint_rolls_back_all_prior_writes(admin):
    pid = prospect()
    tid = task(pid)
    payload = reviewed(
        admin, pid, form(admin, pid, task_disposition="cancel", task_note="Switching to project")
    )
    db.run(
        "CREATE TRIGGER fail_handoff BEFORE INSERT ON prospect_handoffs BEGIN SELECT RAISE(ABORT, 'synthetic refusal'); END"
    )
    assert save(admin, pid, payload).status_code == 409
    assert (
        count("clients")
        == count("projects")
        == count("prospect_handoffs")
        == count("audit_log")
        == 0
    )
    assert db.one("SELECT canceled_at FROM tasks WHERE id=?", (tid,))["canceled_at"] is None


def test_handoff_without_contact_channels_preserves_unknowns_and_original_research(admin):
    pid = prospect(email="", phone="")
    db.run("UPDATE prospects SET contact_name='' WHERE id=?", (pid,))
    assert (
        save(admin, pid, reviewed(admin, pid, form(admin, pid, target_date=""))).status_code == 303
    )
    h = db.one("SELECT * FROM prospect_handoffs WHERE prospect_id=?", (pid,))
    c = db.one("SELECT * FROM clients WHERE id=?", (h["client_id"],))
    assert c["name"] == c["company"] and c["email"] is None and c["phone"] is None
    assert h["target_date"] is None
    db.run("UPDATE prospects SET notes='Later research' WHERE id=?", (pid,))
    page = admin.get(f"{ROOT}/{pid}/handoff")
    assert "Original research" in page.text and "Later research" not in page.text
    assert f"/clients/{c['id']}/delete" not in admin.get(f"/admin/studio/clients/{c['id']}").text


def test_all_handoff_routes_require_admin_and_posts_check_origin(admin):
    pid = prospect()
    values = form(admin, pid)
    before = (count("clients"), count("projects"), count("audit_log"))
    anonymous = TestClient(app)
    try:
        assert anonymous.get(f"{ROOT}/{pid}/handoff", follow_redirects=False).status_code == 303
        for suffix in ("", "/edit", "/review"):
            url = f"{ROOT}/{pid}/handoff{suffix}"
            assert anonymous.post(url, data=values, follow_redirects=False).status_code == 303
            assert (
                admin.post(
                    url,
                    data=values,
                    headers={"Origin": "https://other.example"},
                    follow_redirects=False,
                ).status_code
                == 403
            )
    finally:
        anonymous.close()
    assert (count("clients"), count("projects"), count("audit_log")) == before


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.ids = []
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            assert self.depth == 0
            self.depth += 1
        if "id" in attrs:
            assert attrs["id"] not in self.ids
            self.ids.append(attrs["id"])
        if tag == "input" and "name" in attrs:
            self.inputs[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1
            assert self.depth >= 0


@pytest.mark.parametrize("screening_room", [True, False])
def test_both_styles_escape_and_preserve_review_forms_without_nested_forms(
    admin, monkeypatch, screening_room
):
    monkeypatch.setattr(config, "SCREENING_ROOM", screening_room)
    pid = prospect("Synthetic <script>business()</script>")
    values = form(admin, pid, photography_need="</textarea><script>brief()</script>")
    response = admin.post(f"{ROOT}/{pid}/handoff/review", data=values)
    assert response.status_code == 200
    assert "<script>business()</script>" not in response.text
    assert "&lt;script&gt;brief()&lt;/script&gt;" in response.text
    parser = Forms()
    parser.feed(response.text)
    assert parser.depth == 0 and parser.inputs["photography_need"] == values["photography_need"]
    for suffix in ("/edit", "/review"):
        page = admin.post(
            f"{ROOT}/{pid}/handoff{suffix}",
            data=values | ({"title": ""} if suffix == "/review" else {}),
        )
        parser = Forms()
        parser.feed(page.text)
        assert parser.depth == 0 and parser.inputs["operation_key"] == values["operation_key"]


def test_upgrade_preserves_pre_handoff_data_and_reruns_safely(tmp_path, monkeypatch):
    source = db.MIGRATIONS_DIR
    old = tmp_path / "before-handoffs"
    old.mkdir()
    for path in source.glob("*.sql"):
        if path.name < "086_":
            shutil.copy(path, old / path.name)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "upgrade.db")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old)
    db.migrate()
    pid = prospect()
    cid = client()
    db.run("INSERT INTO projects (client_id,title) VALUES (?, 'Existing')", (cid,))
    task(pid)
    tables = ("prospects", "clients", "projects", "tasks")
    before = {t: [dict(r) for r in db.all_(f"SELECT * FROM {db.ident(t, tables)}")] for t in tables}
    monkeypatch.setattr(db, "MIGRATIONS_DIR", source)
    db.migrate()
    db.migrate()
    for table in tables:
        assert [dict(r) for r in db.all_(f"SELECT * FROM {db.ident(table, tables)}")] == before[
            table
        ]
    assert count("prospect_handoffs") == 0
    assert not db.all_("PRAGMA foreign_key_check")
