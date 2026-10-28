"""Per-event-type content slots: confirm_note (sent in the confirmation email)
and prep_note (stored only; a later reminder reads it).

What must hold: an empty slot changes nothing — the confirmation body is
byte-identical to the one the base code sent, checked against literals captured
from that code and stored under tests/fixtures/event_type_notes/ (never rebuilt
by the code under test). A set confirmation note appears exactly once, between
the booking details and the manage link, in the client's email only. The admin
form saves, audits and clamps both slots without disturbing any other column
(least of all booking_fee_cents), and neither note ever renders on a public
page — event_types.description is the public text, these slots are not.
Assertions are scoped to this file's own rows — shared test DB."""

import json
import pathlib
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import booking_notify, config, db, mailer
from app.admin import scheduling as admin_scheduling
from app.main import app

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "event_type_notes"
EMAIL = "etnote@example.com"
START, END = "2027-04-06 14:00:00", "2027-04-06 15:00:00"
# Pinned so the stored base literals do not depend on the environment.
CONFIG = {
    "BASE_URL": "https://studio.example.test",
    "SITE_NAME": "Example Studio",
    "TIMEZONE": "America/New_York",
    "GMAIL_USER": "studio@example.test",
}
CONFIRM_NOTE = "etnote-confirm: park behind the building, door B."
PREP_NOTE = "etnote-prep: bring two outfits."


@pytest.fixture(autouse=True)
def _clean():
    yield
    db.run("DELETE FROM bookings WHERE token LIKE 'etnote-%'")
    db.run("DELETE FROM inquiries WHERE email = ?", (EMAIL,))
    db.run("DELETE FROM clients WHERE email = ?", (EMAIL,))
    db.run("DELETE FROM event_types WHERE slug LIKE 'etnote-%' OR slug = 'pl-session'")


@pytest.fixture
def emails(monkeypatch):
    """Mailer armed, config pinned; every send captured as (args, kwargs)."""
    sent = []
    for key, value in CONFIG.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **kw: sent.append((a, kw)))
    return sent


@pytest.fixture
def admin_client():
    with TestClient(app) as client:
        r = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
        assert r.status_code == 303
        yield client


def _event(slug="etnote-session", confirm_note=None, prep_note=None, **cols) -> int:
    cols = {
        "slug": slug,
        "name": "Studio Session",
        "description": "A relaxed hour in the studio.",
        "duration_min": 60,
        "location": "On-site",
        **cols,
    }
    eid = db.run(
        f"INSERT INTO event_types ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        tuple(cols.values()),
    )
    # Written only when given, so the same helper seeds the base schema (no slots).
    if confirm_note is not None:
        db.run("UPDATE event_types SET confirm_note=? WHERE id=?", (confirm_note, eid))
    if prep_note is not None:
        db.run("UPDATE event_types SET prep_note=? WHERE id=?", (prep_note, eid))
    return eid


def _booking(eid, token="etnote-1", status="confirmed", reschedule_of=None) -> int:
    return db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, phone, start_utc, end_utc, status, tz,
            reschedule_of, cancel_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            token,
            eid,
            "Ada",
            EMAIL,
            "+15550003333",
            START,
            END,
            status,
            "America/Los_Angeles",
            reschedule_of,
            "Rescheduled" if status == "cancelled" else "",
        ),
    )


def _confirm(eid, rescheduled=False) -> int:
    if rescheduled:
        old = _booking(eid, token="etnote-old", status="cancelled")
        bid = _booking(eid, token="etnote-new", reschedule_of=old)
    else:
        bid = _booking(eid)
    booking_notify.confirm(bid)
    return bid


def _mail(sent, to):
    mine = [(a, kw) for a, kw in sent if a[0] == to]
    assert len(mine) == 1, mine
    return mine[0]


def _base(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── T1: an empty slot is byte-identical to the base ─────────────────────────


@pytest.mark.parametrize(
    ("rescheduled", "client_fixture", "operator_fixture"),
    [
        (False, "confirm_client_body.txt", "confirm_operator_body.txt"),
        (True, "reschedule_client_body.txt", "reschedule_operator_body.txt"),
    ],
)
def test_empty_note_body_is_the_base_body(emails, rescheduled, client_fixture, operator_fixture):
    _confirm(_event(), rescheduled=rescheduled)
    (args, _) = _mail(emails, EMAIL)
    assert args[2] == _base(client_fixture)
    (args, _) = _mail(emails, CONFIG["GMAIL_USER"])
    assert args[2] == _base(operator_fixture)


def test_empty_note_after_migration_reads_empty_string(emails):
    eid = _event()
    row = db.one("SELECT confirm_note, prep_note FROM event_types WHERE id=?", (eid,))
    assert (row["confirm_note"], row["prep_note"]) == ("", "")


# ── T2: a set note appears once, before the manage link, client copy only ───


@pytest.mark.parametrize("rescheduled", [False, True])  # Q1 default: a reschedule repeats it
def test_note_appears_once_before_the_manage_link(emails, rescheduled):
    _confirm(_event(confirm_note=CONFIRM_NOTE, prep_note=PREP_NOTE), rescheduled=rescheduled)
    (args, kw) = _mail(emails, EMAIL)
    body = args[2]
    assert body.count(CONFIRM_NOTE) == 1
    assert body.index("  On-site\n") < body.index(CONFIRM_NOTE) < body.index("Need to change")
    # Exactly the base body with one note paragraph added after the details block.
    base = _base("reschedule_client_body.txt" if rescheduled else "confirm_client_body.txt")
    assert body.replace(f"{CONFIRM_NOTE}\n\n", "", 1) == base
    assert PREP_NOTE not in body  # stored only; no sender reads it yet
    # The operator copy and the .ics invite are unchanged by the slot.
    assert CONFIRM_NOTE not in kw["ics"]["content"]
    (op_args, op_kw) = _mail(emails, CONFIG["GMAIL_USER"])
    assert CONFIRM_NOTE not in op_args[2] and CONFIRM_NOTE not in op_kw["ics"]["content"]


# ── T3: admin form saves, round-trips, audits; an absent field keeps its value ─


def _audit_diff(eid):
    row = db.one(
        """SELECT diff_json FROM audit_log WHERE entity_type='event_type' AND entity_id=?
           AND action='update' ORDER BY id DESC LIMIT 1""",
        (eid,),
    )
    return json.loads(row["diff_json"])


def _post(client, eid, **data):
    r = client.post(f"/admin/scheduling/event/{eid}", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return db.one("SELECT * FROM event_types WHERE id=?", (eid,))


def test_admin_saves_both_notes_and_audits_them(admin_client):
    eid = _event()
    row = _post(
        admin_client, eid, name="Studio Session", confirm_note=CONFIRM_NOTE, prep_note=PREP_NOTE
    )
    assert (row["confirm_note"], row["prep_note"]) == (CONFIRM_NOTE, PREP_NOTE)
    diff = _audit_diff(eid)
    assert diff["confirm_note"] == ["", CONFIRM_NOTE]
    assert diff["prep_note"] == ["", PREP_NOTE]
    page = admin_client.get(f"/admin/scheduling/event/{eid}").text
    form = _form_fields(page, eid)
    assert (form["confirm_note"], form["prep_note"]) == (CONFIRM_NOTE, PREP_NOTE)


def test_absent_note_field_keeps_the_stored_text(admin_client):
    eid = _event(confirm_note=CONFIRM_NOTE, prep_note=PREP_NOTE)
    row = _post(admin_client, eid, name="Studio Session", prep_note="changed")
    assert (row["confirm_note"], row["prep_note"]) == (CONFIRM_NOTE, "changed")
    row = _post(admin_client, eid, name="Studio Session", confirm_note="")
    assert (row["confirm_note"], row["prep_note"]) == ("", "changed")


# ── T4: neither note ever renders on a public page ──────────────────────────


def test_notes_never_render_on_public_pages(emails):
    slug = "pl-session"  # the portraits page's CTA slug, so /portraits loads this row
    eid = _event(slug=slug, confirm_note=CONFIRM_NOTE, prep_note=PREP_NOTE)
    _booking(eid, token="etnote-pub")
    with TestClient(app) as pub:
        pages = {
            "/book": pub.get("/book"),
            f"/book/{slug}": pub.get(f"/book/{slug}"),
            f"/book/{slug} (htmx card)": pub.get(f"/book/{slug}", headers={"hx-request": "true"}),
            # The confirm step: a slot chosen, the details form re-shown (bad email).
            f"POST /book/{slug}": pub.post(
                f"/book/{slug}", data={"name": "Ada", "email": "nope", "start": START}
            ),
            "/booking/{token}": pub.get("/booking/etnote-pub"),
            "/booking/{token}/reschedule": pub.get("/booking/etnote-pub/reschedule"),
            "/booking/{token}/invite.ics": pub.get("/booking/etnote-pub/invite.ics"),
            "/portraits": pub.get("/portraits"),
        }
    for label, r in pages.items():
        assert r.status_code in (200, 400), (label, r.status_code)
        text = r.text.replace("\r\n ", "")  # unfold .ics content lines
        for marker in ("etnote-confirm", "etnote-prep"):  # each note's leading word
            assert marker not in text, (label, marker)
    # Positive controls: the row really reached each event-specific page.
    for label in ("/book", f"/book/{slug}", f"POST /book/{slug}", "/booking/{token}"):
        assert "Studio Session" in pages[label].text, label
    assert START in pages[f"POST /book/{slug}"].text
    assert f"/book/{slug}" in pages["/portraits"].text


# ── T5: a note-only edit leaves every other column alone ────────────────────


class _FormFields(HTMLParser):
    """Collect what the browser would submit from one <form action=...>."""

    def __init__(self, action):
        super().__init__(convert_charrefs=True)
        self.action, self.inside, self.fields, self._textarea = action, False, {}, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = a.get("action") == self.action
        elif self.inside and tag == "input" and a.get("name"):
            if a.get("type") == "checkbox":
                if "checked" in a:
                    self.fields[a["name"]] = a.get("value") or "on"
            else:
                self.fields[a["name"]] = a.get("value") or ""
        elif self.inside and tag == "textarea":
            self._textarea = a["name"]
            self.fields[self._textarea] = ""

    def handle_data(self, data):
        if self._textarea:
            self.fields[self._textarea] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._textarea = None
        elif tag == "form":
            self.inside = False


def _form_fields(page, eid):
    parser = _FormFields(f"/admin/scheduling/event/{eid}")
    parser.feed(page)
    return parser.fields


def test_note_only_edit_through_the_form_changes_nothing_else(admin_client):
    eid = _event(
        prep_note=PREP_NOTE,
        color="#123456",
        buffer_before_min=15,
        buffer_after_min=20,
        min_notice_hours=48,
        max_per_day=2,
        booking_window_days=90,
        slot_step_min=30,
        position=7,
        creates_notion_session=1,
        booking_fee_cents=12500,
        auto_meet=1,
    )
    before = dict(db.one("SELECT * FROM event_types WHERE id=?", (eid,)))
    fields = _form_fields(admin_client.get(f"/admin/scheduling/event/{eid}").text, eid)
    assert fields["prep_note"] == PREP_NOTE  # the form carries the stored slot back
    fields["confirm_note"] = CONFIRM_NOTE
    after = dict(_post(admin_client, eid, **fields))
    assert after.pop("confirm_note") == CONFIRM_NOTE
    before.pop("confirm_note")
    assert after == before


def test_note_only_post_keeps_the_booking_fee(admin_client):
    eid = _event(booking_fee_cents=12500, prep_note=PREP_NOTE)
    row = _post(admin_client, eid, name="Studio Session", confirm_note=CONFIRM_NOTE)
    assert row["booking_fee_cents"] == 12500
    assert row["prep_note"] == PREP_NOTE


# ── T6: an oversize note is clamped, not a 500 ──────────────────────────────


def test_oversize_note_is_clamped(admin_client):
    limit = admin_scheduling.NOTE_MAX
    eid = _event()
    row = _post(
        admin_client,
        eid,
        name="Studio Session",
        confirm_note="c" * (limit + 500),
        prep_note="p" * (limit + 1),
    )
    assert row["confirm_note"] == "c" * limit
    assert row["prep_note"] == "p" * limit
    page = admin_client.get(f"/admin/scheduling/event/{eid}").text
    assert page.count(f'maxlength="{limit}"') == 2  # the form states the same bound


# ── T8: the admin labels say where each note goes ───────────────────────────


class _NoteLabels(HTMLParser):
    """Visible text of each <label> that wraps a textarea, keyed by its name."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels, self._text, self._name, self._in_textarea = {}, None, None, False

    def handle_starttag(self, tag, attrs):
        if tag == "label":
            self._text, self._name = [], None
        elif tag == "textarea" and self._text is not None:
            self._name, self._in_textarea = dict(attrs)["name"], True

    def handle_data(self, data):
        if self._text is not None and not self._in_textarea:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._in_textarea = False
        elif tag == "label" and self._text is not None:
            if self._name:
                self.labels[self._name] = " ".join("".join(self._text).split())
            self._text = None


def test_note_labels_say_where_each_note_goes(admin_client):
    # Wording delegated 2026-10-25 (R15-3 = A). The confirmation note really is
    # emailed on booking and reschedule (T2); nothing reads the prep note yet.
    eid = _event(confirm_note=CONFIRM_NOTE, prep_note=PREP_NOTE)
    page = admin_client.get(f"/admin/scheduling/event/{eid}").text
    parser = _NoteLabels()
    parser.feed(page)
    assert parser.labels["confirm_note"] == (
        "Confirmation note: added to the client's booking confirmation and reschedule emails"
    )
    assert parser.labels["prep_note"] == "Prep note: saved only. Nothing sends it yet"
    assert "(internal;" not in page


# ── T7: the migration ───────────────────────────────────────────────────────


def test_migration_is_additive_unique_and_defaults_to_empty():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_migration_numbering", ROOT / "scripts" / "check-migration-numbering.py"
    )
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    assert checker.duplicates(ROOT / "migrations") == []
    forward = sorted((ROOT / "migrations").glob("*_event_type_notes.sql"))
    assert [p.name for p in forward] == ["083_event_type_notes.sql"]
    assert (ROOT / "migrations" / "rollback" / "083_event_type_notes.sql").is_file()
    cols = {r["name"]: r for r in db.all_("PRAGMA table_info(event_types)")}
    for name in ("confirm_note", "prep_note"):
        assert cols[name]["type"] == "TEXT"
        assert cols[name]["notnull"] == 1
        assert cols[name]["dflt_value"] == "''"  # no note text ships in the migration
