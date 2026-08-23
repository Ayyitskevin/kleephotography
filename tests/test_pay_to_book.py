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


def _checkout_completed(event_id, booking_id, amount, session_id="cs_wh", currency="usd"):
    return {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "object": "checkout.session",
                "payment_status": "paid",
                "amount_total": amount,
                "currency": currency,
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
    assert b["stripe_checkout_amount_cents"] == 15000
    assert b["stripe_checkout_currency"] == "usd"
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
def test_booking_reuses_open_checkout_on_pay_again(client, event_type, monkeypatch):
    created = []
    sessions = {}

    class _Sess:
        status = "open"
        amount_total = 15000
        currency = "usd"

        def __init__(self, number):
            self.id = f"cs_book_repeat_{number}"
            self.url = f"https://checkout.stripe.test/{self.id}"

    def fake_create(**kwargs):
        session = _Sess(len(created) + 1)
        created.append(kwargs)
        sessions[session.id] = session
        return session

    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setattr(pub_sched.stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(
        pub_sched.stripe.checkout.Session,
        "retrieve",
        lambda session_id, **kwargs: sessions[session_id],
    )

    first = _book(client, event_type, _a_slot(event_type))
    b = db.one("SELECT * FROM bookings WHERE event_type_id=?", (event_type["id"],))
    second = client.post(f"/booking/{b['token']}/pay", follow_redirects=False)

    assert first.status_code == 303 and second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    assert len(created) == 1
    assert created[0]["idempotency_key"].startswith(f"mise:booking:{b['id']}:15000:usd:")
    assert b["stripe_checkout_amount_cents"] == 15000
    assert b["stripe_checkout_currency"] == "usd"


@pytest.mark.parametrize("event_type", [15000], indirect=True)
def test_booking_quote_does_not_change_after_event_price_edit(client, event_type, monkeypatch):
    created = []
    sessions = {}

    class _Sess:
        status = "open"
        currency = "usd"

        def __init__(self, number, amount):
            self.id = f"cs_price_{number}"
            self.url = f"https://checkout.stripe.test/{self.id}"
            self.amount_total = amount

    def fake_create(**kwargs):
        amount = kwargs["line_items"][0]["price_data"]["unit_amount"]
        session = _Sess(len(created) + 1, amount)
        created.append(kwargs)
        sessions[session.id] = session
        return session

    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setattr(pub_sched.stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(
        pub_sched.stripe.checkout.Session,
        "retrieve",
        lambda session_id, **kwargs: sessions[session_id],
    )

    _book(client, event_type, _a_slot(event_type))
    b = db.one("SELECT * FROM bookings WHERE event_type_id=?", (event_type["id"],))
    sessions[b["stripe_session_id"]].status = "expired"
    db.run("UPDATE event_types SET booking_fee_cents=25000 WHERE id=?", (event_type["id"],))

    retry = client.post(f"/booking/{b['token']}/pay", follow_redirects=False)

    assert retry.status_code == 303
    assert len(created) == 2
    assert [c["line_items"][0]["price_data"]["unit_amount"] for c in created] == [15000, 15000]
    assert created[1]["idempotency_key"].endswith(b["stripe_session_id"])


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
        status = "open"
        amount_total = 15000
        currency = "usd"

    session = _Sess()
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setattr(pub_sched.stripe.checkout.Session, "create", lambda **k: session)
    monkeypatch.setattr(pub_sched.stripe.checkout.Session, "retrieve", lambda *a, **k: session)
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
@pytest.mark.parametrize(
    ("event", "code"),
    [
        (_checkout_completed("evt_bk_bad_amount", 0, 1), "checkout_mismatch"),
        (_checkout_completed("evt_bk_bad_currency", 0, 15000, currency="eur"), "checkout_mismatch"),
        (_checkout_completed("evt_bk_stale", 0, 15000, session_id="cs_stale"), "stale_session"),
    ],
)
def test_webhook_rejects_untrusted_booking_checkout_fields(
    client, event_type, monkeypatch, event, code
):
    b = _held_booking(client, event_type, monkeypatch)
    event["data"]["object"]["metadata"]["booking_id"] = str(b["id"])
    from app.public import pay as pay_mod

    monkeypatch.setattr(pay_mod.alerts, "payment_anomaly", lambda *a, **k: None)
    r = _signed_event(client, event)

    assert r.status_code == 200
    assert r.json()["unapplied"] == code
    assert (
        db.one("SELECT status FROM bookings WHERE id=?", (b["id"],))["status"] == "pending_payment"
    )
    assert (
        db.one("SELECT COUNT(*) n FROM booking_payments WHERE booking_id=?", (b["id"],))["n"] == 0
    )


@pytest.mark.parametrize("event_type", [15000], indirect=True)
def test_distinct_event_cannot_confirm_booking_twice(client, event_type, monkeypatch):
    b = _held_booking(client, event_type, monkeypatch)
    notified = []
    import app.booking_notify as bn
    from app.public import pay as pay_mod

    monkeypatch.setattr(bn, "confirm", lambda bid: notified.append(bid))
    monkeypatch.setattr(pay_mod.alerts, "payment_anomaly", lambda *a, **k: None)

    first = _signed_event(client, _checkout_completed("evt_bk_once", b["id"], 15000))
    second = _signed_event(client, _checkout_completed("evt_bk_twice", b["id"], 15000))

    assert first.json() == {"ok": True}
    assert second.json()["unapplied"] == "already_settled"
    assert notified == [b["id"]]
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
    # live hold → redirects to its one authoritative checkout
    r = client.post(f"/booking/{b['token']}/pay", follow_redirects=False)
    assert r.status_code == 303
    # confirmed booking → no pay route
    db.run("UPDATE bookings SET status='confirmed' WHERE id=?", (b["id"],))
    assert client.post(f"/booking/{b['token']}/pay").status_code == 404
