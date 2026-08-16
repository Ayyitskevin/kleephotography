"""Pay-to-book (revenue roadmap item 1) + lead attribution (item 3).

The properties that must hold, in rough order of how much money each protects:
a free event type behaves exactly as before; a fee'd type holds its slot
without confirming anything; the hold blocks double-booking but expires
exactly; the webhook confirms idempotently and fires the side-effects only
then; money on a released hold alerts instead of silently re-confirming; and
the attribution answer survives from form to rollup without admitting free
text.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, scheduling
from app.main import app
from app.public import scheduling as pub_sched

pytestmark = pytest.mark.integration

_SECRET = "whsec_book_test"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", _SECRET)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def event_type(request):
    """A bookable event type with wide-open availability. Fee via param."""
    fee = getattr(request, "param", 0)
    slug = f"pb-{time.time_ns()}"
    db.run(
        """INSERT INTO event_types (slug, name, duration_min, min_notice_hours,
                                    booking_window_days, booking_fee_cents)
           VALUES (?,?,60,0,60,?)""",
        (slug, "Mini Session", fee),
    )
    et = db.one("SELECT * FROM event_types WHERE slug=?", (slug,))
    for wd in range(7):
        db.run(
            "INSERT INTO availability_rules (event_type_id, weekday, start_min, end_min) "
            "VALUES (?,?,540,1020)",
            (et["id"], wd),
        )
    yield et
    db.run(
        "DELETE FROM booking_payments WHERE booking_id IN "
        "(SELECT id FROM bookings WHERE event_type_id=?)",
        (et["id"],),
    )
    db.run("DELETE FROM bookings WHERE event_type_id=?", (et["id"],))
    db.run("DELETE FROM availability_rules WHERE event_type_id=?", (et["id"],))
    db.run("DELETE FROM event_types WHERE id=?", (et["id"],))


def _a_slot(et) -> str:
    """First open slot within the window, as the UTC string the form posts."""
    import datetime as dt

    day = dt.date.today() + dt.timedelta(days=7)
    slots = scheduling.slots_for_day(et, day, "", None)
    assert slots, "fixture produced no open slots"
    return slots[0]["utc"]


def _book(client, et, start, referral="", follow=False):
    return client.post(
        f"/book/{et['slug']}",
        data={
            "name": "Test Client",
            "email": "pb@example.com",
            "start": start,
            "tz": "",
            "referral": referral,
        },
        follow_redirects=follow,
    )


def _signed_event(client, body: dict):
    raw = json.dumps(body).encode()
    t = int(time.time())
    mac = hmac.new(_SECRET.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/stripe", content=raw, headers={"stripe-signature": f"t={t},v1={mac}"}
    )


def _checkout_completed(event_id, booking_id, amount):
    return {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{event_id}",
                "object": "checkout.session",
                "payment_status": "paid",
                "amount_total": amount,
                "metadata": {"kind": "booking", "booking_id": str(booking_id)},
            }
        },
    }


# ── free path: byte-for-byte unchanged ──────────────────────────────────────


def test_free_event_type_confirms_immediately(client, event_type, monkeypatch):
    notified = []
    monkeypatch.setattr(pub_sched.booking_notify, "confirm", lambda bid: notified.append(bid))
    r = _book(client, event_type, _a_slot(event_type))
    assert r.status_code == 303 and r.headers["location"].startswith("/booking/")
    b = db.one("SELECT * FROM bookings WHERE event_type_id=?", (event_type["id"],))
    assert b["status"] == "confirmed"
    assert b["paid_cents"] == 0
    assert notified == [b["id"]]


# ── attribution (item 3) ────────────────────────────────────────────────────


def test_booking_records_a_valid_referral(client, event_type, monkeypatch):
    monkeypatch.setattr(pub_sched.booking_notify, "confirm", lambda bid: None)
    _book(client, event_type, _a_slot(event_type), referral="Google / search")
    b = db.one("SELECT referral_source FROM bookings WHERE event_type_id=?", (event_type["id"],))
    assert b["referral_source"] == "Google / search"


def test_free_text_referral_never_reaches_the_column(client, event_type, monkeypatch):
    """The rollup GROUP BY must be unpollutable — a tampered POST stores NULL."""
    monkeypatch.setattr(pub_sched.booking_notify, "confirm", lambda bid: None)
    _book(client, event_type, _a_slot(event_type), referral="<script>alert(1)</script>")
    b = db.one("SELECT referral_source FROM bookings WHERE event_type_id=?", (event_type["id"],))
    assert b["referral_source"] is None


def test_contact_form_records_and_guards_referral(client):
    for sent, stored in [
        ("Instagram / social media", "Instagram / social media"),
        ("something typed", None),
    ]:
        r = client.post(
            "/contact",
            data={
                "name": "A",
                "email": f"a{time.time_ns()}@example.com",
                "message": "hello there",
                "referral": sent,
            },
        )
        assert r.status_code == 200
        row = db.one("SELECT referral_source FROM inquiries ORDER BY id DESC LIMIT 1")
        assert row["referral_source"] == stored


# ── the hold (item 1) ───────────────────────────────────────────────────────


@pytest.mark.parametrize("event_type", [15000], indirect=True)
def test_paid_type_holds_without_confirming(client, event_type, monkeypatch):
    notified = []
    monkeypatch.setattr(pub_sched.booking_notify, "confirm", lambda bid: notified.append(bid))
    created = {}

    class _Sess:
        id = "cs_hold_test"
        url = "https://checkout.stripe.test/pay"

    def fake_create(**kwargs):
        created.update(kwargs)
        return _Sess()

    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setattr(pub_sched.stripe.checkout.Session, "create", fake_create)

    start = _a_slot(event_type)
    r = _book(client, event_type, start)
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.test/pay"
    b = db.one("SELECT * FROM bookings WHERE event_type_id=?", (event_type["id"],))
    assert b["status"] == "pending_payment"
    assert b["stripe_session_id"] == "cs_hold_test"
    assert notified == [], "side-effects fired before any money arrived"
    assert created["metadata"] == {"kind": "booking", "booking_id": str(b["id"])}
    assert created["line_items"][0]["price_data"]["unit_amount"] == 15000

    # The hold occupies the slot: booking the same instant now loses the race.
    r2 = _book(client, event_type, start)
    assert r2.status_code == 409

    # ...but an EXPIRED hold does not: age it past the TTL and the slot is open.
    db.run(
        "UPDATE bookings SET created_at=datetime('now', ?) WHERE id=?",
        (f"-{config.BOOKING_PAY_TTL_MIN + 5} minutes", b["id"]),
    )
    import datetime as dt

    day = dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S").date()
    open_now = {s["utc"] for s in scheduling.slots_for_day(event_type, day, "", None)}
    assert start in open_now, "expired hold still pinning its slot"
    # and the sweep tidies the row itself
    assert scheduling.expire_pending_payments() >= 1
    assert db.one("SELECT status FROM bookings WHERE id=?", (b["id"],))["status"] == "cancelled"


@pytest.mark.parametrize("event_type", [15000], indirect=True)
def test_fee_with_stripe_unconfigured_refuses_loudly(client, event_type, monkeypatch):
    """Fail loud, never free: a paid type must not silently give itself away."""
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "")
    pinged = []
    monkeypatch.setattr(pub_sched.alerts, "ops_alert", lambda sig, txt: pinged.append(sig))
    r = _book(client, event_type, _a_slot(event_type))
    assert r.status_code == 503
    assert (
        db.one("SELECT COUNT(*) n FROM bookings WHERE event_type_id=?", (event_type["id"],))["n"]
        == 0
    )
    assert pinged and pinged[0].startswith("booking_fee_unconfigured")


# ── the webhook (item 1, money half) ────────────────────────────────────────


def _held_booking(client, event_type, monkeypatch):
    class _Sess:
        id = "cs_wh"
        url = "https://checkout.stripe.test/pay"

    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setattr(pub_sched.stripe.checkout.Session, "create", lambda **k: _Sess())
    _book(client, event_type, _a_slot(event_type))
    return db.one("SELECT * FROM bookings WHERE event_type_id=?", (event_type["id"],))


@pytest.mark.parametrize("event_type", [15000], indirect=True)
def test_webhook_confirms_hold_and_fires_side_effects(client, event_type, monkeypatch):
    b = _held_booking(client, event_type, monkeypatch)
    notified = []
    import app.booking_notify as bn

    monkeypatch.setattr(bn, "confirm", lambda bid: notified.append(bid))

    r = _signed_event(client, _checkout_completed("evt_bk_ok", b["id"], 15000))
    assert r.status_code == 200 and r.json() == {"ok": True}
    after = db.one("SELECT * FROM bookings WHERE id=?", (b["id"],))
    assert after["status"] == "confirmed"
    assert after["paid_cents"] == 15000 and after["paid_at"]
    assert notified == [b["id"]], "confirmation side-effects did not fire on payment"
    pay = db.one("SELECT * FROM booking_payments WHERE booking_id=?", (b["id"],))
    assert pay["stripe_event_id"] == "evt_bk_ok" and pay["amount_cents"] == 15000

    # Redelivery: idempotent, no second notify, no second payment row.
    r2 = _signed_event(client, _checkout_completed("evt_bk_ok", b["id"], 15000))
    assert r2.status_code == 200 and r2.json().get("duplicate") is True
    assert len(notified) == 1
    assert (
        db.one("SELECT COUNT(*) n FROM booking_payments WHERE booking_id=?", (b["id"],))["n"] == 1
    )


@pytest.mark.parametrize("event_type", [15000], indirect=True)
def test_money_on_a_released_hold_alerts_instead_of_reconfirming(client, event_type, monkeypatch):
    """Minute-31 payment on a 30-minute hold: the slot may be re-sold already."""
    b = _held_booking(client, event_type, monkeypatch)
    db.run(
        "UPDATE bookings SET status='cancelled', cancel_reason='Payment not completed' WHERE id=?",
        (b["id"],),
    )
    from app.public import pay as pay_mod

    fired = []
    monkeypatch.setattr(pay_mod.alerts, "payment_anomaly", lambda *a, **k: fired.append((a, k)))

    r = _signed_event(client, _checkout_completed("evt_bk_late", b["id"], 15000))
    assert r.status_code == 200
    assert r.json()["unapplied"] == "paid_after_release"
    assert db.one("SELECT status FROM bookings WHERE id=?", (b["id"],))["status"] == "cancelled"
    assert fired and fired[0][1].get("entity") == "booking"
    row = db.one(
        "SELECT action FROM audit_log WHERE entity_type='booking' AND entity_id=? "
        "ORDER BY id DESC LIMIT 1",
        (b["id"],),
    )
    assert row["action"] == "stripe_paid_after_release"
    db.run("DELETE FROM audit_log WHERE entity_type='booking' AND entity_id=?", (b["id"],))


def test_webhook_acks_unknown_booking(client, monkeypatch):
    from app.public import pay as pay_mod

    monkeypatch.setattr(pay_mod.alerts, "payment_anomaly", lambda *a, **k: None)
    r = _signed_event(client, _checkout_completed("evt_bk_ghost", 99_654_321, 5000))
    assert r.status_code == 200
    assert r.json()["unapplied"] == "unknown_booking"
    db.run("DELETE FROM audit_log WHERE entity_type='booking' AND entity_id=99654321")


@pytest.mark.parametrize("event_type", [15000], indirect=True)
def test_pay_again_route_guards_status(client, event_type, monkeypatch):
    b = _held_booking(client, event_type, monkeypatch)
    # live hold → redirects to a fresh checkout
    r = client.post(f"/booking/{b['token']}/pay", follow_redirects=False)
    assert r.status_code == 303
    # confirmed booking → no pay route
    db.run("UPDATE bookings SET status='confirmed' WHERE id=?", (b["id"],))
    assert client.post(f"/booking/{b['token']}/pay").status_code == 404
