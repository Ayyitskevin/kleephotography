"""Studio board intel: client-only touch age, the attention key, the card's
quiet chip / your-move stripe / column share bars, the quick-actions peek, and
the audit row every stage move now writes.

The suite shares one SQLite file with every other test file, so each fixture
removes what it inserted (audit rows included — project ids can be reused).
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import studio_context
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
def client_id():
    cid = db.run(
        "INSERT INTO clients (name, company, email, phone) VALUES (?,?,?,?)",
        ("Intel Chef", "Intel Kitchen", "chef@intel.example", "+15550100"),
    )
    yield cid
    db.run("DELETE FROM clients WHERE id=?", (cid,))


@pytest.fixture
def new_project(client_id):
    made: list[int] = []

    def make(title: str = "Board Intel Shoot", age_days: int = 0) -> int:
        pid = db.run(
            """INSERT INTO projects (client_id, title, created_at, stage_changed_at)
               VALUES (?, ?, datetime('now', ?), datetime('now', ?))""",
            (client_id, title, f"-{age_days} days", f"-{age_days} days"),
        )
        made.append(pid)
        return pid

    yield make
    for pid in made:
        inquiry_ids = [
            r["id"]
            for r in db.all_("SELECT id FROM inquiries WHERE converted_project_id=?", (pid,))
        ]
        for qid in inquiry_ids:
            db.run("DELETE FROM messages WHERE inquiry_id=?", (qid,))
            db.run("DELETE FROM inquiries WHERE id=?", (qid,))
        db.run("DELETE FROM proposals WHERE project_id=?", (pid,))
        db.run("DELETE FROM invoices WHERE project_id=?", (pid,))
        db.run("DELETE FROM audit_log WHERE entity_type='project' AND entity_id=?", (pid,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))


def _pipeline_row(pid: int):
    rows = studio_context._ctx_pipeline(studio_context.dt.date.today().isoformat())["projects"]
    return next(r for r in rows if r["id"] == pid)


def _proposal(pid: int, status: str) -> None:
    db.run(
        "INSERT INTO proposals (project_id, slug, title, status) VALUES (?,?,?,?)",
        (pid, f"intel-p-{pid}-{status}", "Menu shoot", status),
    )


# ── touch age ────────────────────────────────────────────────────────────────


def test_touch_age_counts_the_clients_reply_but_not_our_follow_up(new_project):
    pid = new_project()
    qid = db.run(
        """INSERT INTO inquiries (name, email, message, converted_project_id, created_at)
           VALUES ('Intel Chef', 'chef@intel.example', 'hi', ?, '2026-01-01 09:00:00')""",
        (pid,),
    )
    db.run(
        "INSERT INTO messages (inquiry_id, direction, channel, created_at) "
        "VALUES (?, 'in', 'sms', '2026-01-03 09:00:00')",
        (qid,),
    )
    db.run(
        "INSERT INTO messages (inquiry_id, direction, channel, created_at) "
        "VALUES (?, 'out', 'sms', '2026-01-10 09:00:00')",
        (qid,),
    )

    row = _pipeline_row(pid)

    assert row["last_client_touch_at"] == "2026-01-03 09:00:00"


def test_a_project_the_client_never_touched_ages_from_its_stage(new_project):
    pid = new_project(age_days=12)

    row = _pipeline_row(pid)

    assert row["last_client_touch_at"] is None
    assert row["days_quiet"] == row["days_in_stage"] == 12


# ── attention key ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "attention"),
    [("sent", "their_move"), ("viewed", "their_move"), ("accepted", "your_move")],
)
def test_intel_carries_an_attention_key_for_proposals(new_project, status, attention):
    pid = new_project()
    _proposal(pid, status)

    assert studio_context._ctx_intel()[pid]["attention"] == attention


def test_an_unpaid_invoice_is_money_out(new_project):
    pid = new_project()
    db.run(
        "INSERT INTO invoices (project_id, slug, title, status) VALUES (?,?,?, 'sent')",
        (pid, f"intel-i-{pid}", "Retainer"),
    )

    assert studio_context._ctx_intel()[pid]["attention"] == "money_out"


# ── board markup ─────────────────────────────────────────────────────────────


def _card(html: str, pid: int) -> str:
    match = re.search(
        rf'<article class="(?P<cls>[^"]*)"[^>]*\bdata-pid="{pid}".*?</article>', html, re.S
    )
    assert match, f"card {pid} not on the board"
    return match.group(0)


def test_board_flags_a_quiet_client_and_stripes_your_move(admin_client, new_project):
    quiet = new_project("Quiet Bistro", age_days=30)
    yours = new_project("Accepted Cafe")
    _proposal(yours, "accepted")

    html = admin_client.get("/admin/studio").text

    assert "quiet 30d" in _card(html, quiet)
    assert "quiet " not in _card(html, yours)
    assert "is-att-your_move" in _card(html, yours).split(">", 1)[0]
    assert 'class="stu-col-share"' in html


def test_every_card_carries_a_lazy_peek_that_the_click_handler_ignores(admin_client, new_project):
    pid = new_project()

    card = _card(admin_client.get("/admin/studio").text, pid)

    assert "data-goto-ignore" in card
    assert f'hx-get="/admin/studio/projects/{pid}/peek"' in card
    assert 'hx-trigger="toggle once"' in card


# ── peek ─────────────────────────────────────────────────────────────────────


def test_peek_shows_contacts_touch_and_a_stage_form(admin_client, new_project):
    pid = new_project()

    response = admin_client.get(f"/admin/studio/projects/{pid}/peek")

    assert response.status_code == 200
    assert 'href="mailto:chef@intel.example"' in response.text
    assert 'href="sms:+15550100"' in response.text
    assert "No client action recorded yet" in response.text
    assert f'hx-post="/admin/studio/projects/{pid}/status"' in response.text
    assert '<option value="inquiry_received" selected>' in response.text


def test_peek_404s_for_a_missing_project(admin_client):
    assert admin_client.get("/admin/studio/projects/999999/peek").status_code == 404


# ── stage history ────────────────────────────────────────────────────────────


def _stage_moves(pid: int) -> list:
    return [
        json.loads(r["diff_json"])["status"]
        for r in db.all_(
            "SELECT diff_json FROM audit_log WHERE entity_type='project' AND entity_id=? "
            "AND action='stage_move' ORDER BY id",
            (pid,),
        )
    ]


def test_each_real_stage_move_writes_one_audit_row(admin_client, new_project):
    pid = new_project()

    moved = admin_client.post(
        f"/admin/studio/projects/{pid}/status",
        data={"status": "consultation_call"},
        headers={"HX-Request": "true"},
    )
    admin_client.post(
        f"/admin/studio/projects/{pid}/status",
        data={"status": "consultation_call"},
        follow_redirects=False,
    )
    admin_client.post(
        f"/admin/studio/projects/{pid}",
        data={"title": "Board Intel Shoot", "status": "proposal_sent"},
        follow_redirects=False,
    )

    assert moved.status_code == 200 and 'id="studio-board"' in moved.text
    assert _stage_moves(pid) == [
        ["inquiry_received", "consultation_call"],
        ["consultation_call", "proposal_sent"],
    ]
