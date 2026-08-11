"""The de-async contract for handlers that cannot become plain `def`.

Commit 2879636 moved 251 handlers to sync `def` so Starlette runs them in its
threadpool. The handlers left over are the ones that must `await` a form or a
body, and awaiting is the only thing they are allowed to do on the event loop:
everything past the parse blocks (SQLite carries a 30s busy_timeout, jobs
enqueue, templates render), and the loop is what writes bytes to every open
socket. One long lock wait on the loop stalls responses the threadpool already
finished computing.

So each of those handlers hands its post-parse work to `run_in_threadpool`. This
module holds that line from three sides: a live test that a slow write on one
route leaves the loop free to answer another, output pins for the converted
handlers the rest of the suite never reached, and a structural fence that fails
the moment an async handler in a swept module calls back into the app directly.
"""

import ast
import asyncio
import hashlib
import hmac
import json
import pathlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

# Long enough that a blocked loop is unmistakable, short enough to stay cheap.
SLOW_WRITE_SEC = 1.0
# When the loop is free the probe returns after this pause plus a few ms.
PROBE_AFTER_SEC = 0.25


@pytest.fixture
def probe_form():
    """A questionnaire form — the submit path that touches no mail or job queue."""
    form_id = db.run(
        "INSERT INTO forms (slug, title, kind, active) "
        "VALUES ('deasync-probe','De-async Probe','questionnaire',1)"
    )
    yield "deasync-probe"
    db.run("DELETE FROM form_submissions WHERE form_id=?", (form_id,))
    db.run("DELETE FROM forms WHERE id=?", (form_id,))


@pytest.mark.integration
def test_slow_form_write_does_not_stall_the_event_loop(probe_form, monkeypatch):
    """A slow write inside POST /forms/{slug} must not delay a concurrent request.

    The submission INSERT stands in for any blocking SQLite call — a WAL
    checkpoint or a fat transaction from a job thread makes a real one take just
    as long. /healthz is the probe because it deliberately stays on the loop
    (see main.healthz), so it can only answer while the loop is free.

    Driven through httpx's ASGI transport rather than TestClient: TestClient
    hands each request to its own blocking portal, which would hide exactly the
    interference this asserts.
    """
    real_run = db.run

    def slow_run(sql, params=()):
        if "form_submissions" in sql:
            time.sleep(SLOW_WRITE_SEC)
        return real_run(sql, params)

    monkeypatch.setattr(db, "run", slow_run)

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as c:
            started = time.perf_counter()
            submit = asyncio.create_task(
                c.post(f"/forms/{probe_form}", data={"name": "P", "email": "p@example.com"})
            )
            await asyncio.sleep(PROBE_AFTER_SEC)
            health = await c.get("/healthz")
            elapsed = time.perf_counter() - started
            return elapsed, health, await submit

    elapsed, health, submitted = asyncio.run(drive())

    assert submitted.status_code == 200
    assert health.status_code == 200
    # A blocked loop cannot even resume the sleep above until the write is done,
    # so elapsed would land at or past SLOW_WRITE_SEC instead of just past the pause.
    assert elapsed < PROBE_AFTER_SEC + SLOW_WRITE_SEC / 2, (
        f"/healthz answered {elapsed:.2f}s in — the form write blocked the event loop"
    )


@pytest.mark.integration
def test_slow_stripe_webhook_does_not_stall_the_event_loop(monkeypatch):
    """Same proof as the form write, for the money route.

    This is the one handler whose caller is a machine that retries. Blocking the
    loop here means every other in-flight request stalls while Stripe is already
    redelivering into a server that is struggling — the failure feeding itself.

    The invoice lookup stands in for any of the handler's ~6 round-trips; a WAL
    checkpoint or a fat transaction from a job thread makes a real one this slow.
    """
    real_one = db.one

    def slow_one(sql, params=()):
        if "FROM invoices" in sql:
            time.sleep(SLOW_WRITE_SEC)
        return real_one(sql, params)

    monkeypatch.setattr(db, "one", slow_one)
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_probe")

    body = json.dumps(
        {
            "id": "evt_loop_probe",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_loop_probe",
                    "object": "checkout.session",
                    "payment_status": "paid",
                    "amount_total": 1000,
                    "metadata": {"invoice_id": "99123457", "kind": "full"},
                }
            },
        }
    )
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec_probe", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as c:
            started = time.perf_counter()
            hook = asyncio.create_task(
                c.post(
                    "/webhooks/stripe",
                    content=body,
                    headers={"stripe-signature": f"t={ts},v1={sig}"},
                )
            )
            await asyncio.sleep(PROBE_AFTER_SEC)
            health = await c.get("/healthz")
            elapsed = time.perf_counter() - started
            return elapsed, health, await hook

    elapsed, health, hooked = asyncio.run(drive())

    assert hooked.status_code == 200  # unknown invoice → acknowledged, not retried
    assert health.status_code == 200
    assert elapsed < PROBE_AFTER_SEC + SLOW_WRITE_SEC / 2, (
        f"/healthz answered {elapsed:.2f}s in — the Stripe webhook blocked the event loop"
    )
    db.run("DELETE FROM audit_log WHERE entity_type='invoice' AND entity_id=99123457")


# ── Output pins for the handlers the sweep touched that nothing else covers ──
# The rest of the converted routes are exercised by the smoke suite, which is
# what proves the threadpool hop changed nothing. These three had no test at
# all, so they get one against their behaviour as it stands.


@pytest.fixture
def admin_client():
    with TestClient(app) as client:
        response = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
        assert response.status_code == 303
        yield client


@pytest.mark.integration
def test_save_availability_replaces_the_global_week(admin_client):
    """POST /admin/scheduling/availability rewrites every global rule in one go.

    toggle=<wd> flips the day whose switch was clicked; the other six keep the
    state the hidden inputs posted. Days that end up off contribute no row.
    """
    kept = db.all_(
        "SELECT weekday, start_min, end_min FROM availability_rules WHERE event_type_id IS NULL"
    )
    try:
        data = {f"on_{wd}": "1" for wd in (1, 2, 3)}
        data.update({f"start_{wd}": "10:00" for wd in (1, 2, 3)})
        data.update({f"end_{wd}": "16:30" for wd in (1, 2, 3)})
        # Wednesday's switch was the one clicked, so it inverts to off. Friday
        # posts off and is not the toggled day, so it stays off.
        data["toggle"] = "3"
        data["on_5"] = ""
        response = admin_client.post(
            "/admin/scheduling/availability", data=data, follow_redirects=False
        )
        assert response.status_code == 303

        rows = db.all_(
            "SELECT weekday, start_min, end_min FROM availability_rules "
            "WHERE event_type_id IS NULL ORDER BY weekday"
        )
        assert [tuple(r) for r in rows] == [(1, 600, 990), (2, 600, 990)]

        audit_row = db.one(
            "SELECT action, diff_json FROM audit_log "
            "WHERE entity_type='availability' ORDER BY id DESC LIMIT 1"
        )
        assert audit_row["action"] == "set_global"
        assert json.loads(audit_row["diff_json"]) == {"days": ["Tue", "Wed"]}
    finally:
        db.run("DELETE FROM availability_rules WHERE event_type_id IS NULL")
        for row in kept:
            db.run(
                "INSERT INTO availability_rules (event_type_id, weekday, start_min, end_min) "
                "VALUES (NULL,?,?,?)",
                (row["weekday"], row["start_min"], row["end_min"]),
            )


@pytest.mark.integration
def test_update_event_writes_the_whitelisted_columns_and_audits_the_diff(admin_client):
    event_id = db.run(
        "INSERT INTO event_types (slug, name, duration_min) VALUES (?,?,?)",
        ("deasync-evt", "Before", 30),
    )
    try:
        response = admin_client.post(
            f"/admin/scheduling/event/{event_id}",
            data={
                "name": "After",
                "description": "  trimmed  ",
                "duration_min": "45",
                "location": "Studio",
                "color": "#123456",
                "buffer_before_min": "10",
                "max_per_day": "3",
                "creates_notion_session": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        row = db.one("SELECT * FROM event_types WHERE id=?", (event_id,))
        assert row["name"] == "After"
        assert row["description"] == "trimmed"
        assert (row["duration_min"], row["location"], row["color"]) == (45, "Studio", "#123456")
        assert (row["buffer_before_min"], row["max_per_day"]) == (10, 3)
        assert row["creates_notion_session"] == 1
        # min_notice_hours/booking_window_days carry their own defaults when the
        # form omits them — blank does not mean zero for those two.
        assert (row["min_notice_hours"], row["booking_window_days"]) == (12, 60)

        audit_row = db.one(
            "SELECT entity_id, action FROM audit_log "
            "WHERE entity_type='event_type' ORDER BY id DESC LIMIT 1"
        )
        assert (audit_row["entity_id"], audit_row["action"]) == (event_id, "update")

        blank = admin_client.post(
            f"/admin/scheduling/event/{event_id}", data={"name": " "}, follow_redirects=False
        )
        assert blank.status_code == 400
        assert db.one("SELECT name FROM event_types WHERE id=?", (event_id,))["name"] == "After"
    finally:
        db.run("DELETE FROM event_types WHERE id=?", (event_id,))


@pytest.mark.integration
def test_plutus_callback_records_the_handoff_result(monkeypatch):
    monkeypatch.setattr(config, "ARGUS_TOKEN", "argus-test-token")
    auth = {"Authorization": "Bearer argus-test-token"}
    gallery_id = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("DeasyncPlutus", "Plutus probe", "4321"),
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/plutus/callback?gallery_id={gallery_id}",
                json={"status": "done", "run_id": 7, "review_url": "https://plutus/r/7"},
                headers=auth,
            )
            assert response.status_code == 200
            assert response.json() == {"ok": True, "gallery_id": gallery_id}
            row = db.one(
                "SELECT plutus_last_run_id, plutus_last_status, plutus_last_offer_url "
                "FROM galleries WHERE id=?",
                (gallery_id,),
            )
            assert tuple(row) == (7, "done", "https://plutus/r/7")

            # The gallery check runs before the body is read, so an unknown id is
            # a 404 whatever the payload looks like.
            missing = client.post("/api/plutus/callback?gallery_id=99999999", json={}, headers=auth)
            assert missing.status_code == 404
            not_an_object = client.post(
                f"/api/plutus/callback?gallery_id={gallery_id}", json=[1, 2], headers=auth
            )
            assert not_an_object.status_code == 400
    finally:
        db.run("DELETE FROM galleries WHERE id=?", (gallery_id,))


# ── Structural fence ────────────────────────────────────────────────────────

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Every module whose async handlers were swept. app/main.py is out by design
# (/healthz and the error handler are degradation paths that must answer while
# the threadpool is saturated), and so are app/admin/financials.py and
# app/admin/uploads.py, which still carry the defect and are owned elsewhere.
# Adding a module here is how the fence grows to cover them — app/public/pay.py
# joined when the Stripe webhook was split.
SWEPT = (
    "app/public/forms.py",
    "app/public/pay.py",
    "app/public/sms_webhook.py",
    "app/service_api.py",
    "app/admin/galleries.py",
    "app/admin/gallery_sections.py",
    "app/admin/invoices.py",
    "app/admin/licenses.py",
    "app/admin/presets.py",
    "app/admin/press.py",
    "app/admin/proposals.py",
    "app/admin/recurring.py",
    "app/admin/scheduling.py",
    "app/admin/shotlist.py",
    "app/admin/studio_brand.py",
)

# The only app-level calls an async handler may make on the loop. save_upload is
# the streaming read itself — it cannot move, it IS the await.
LOOP_SAFE_CALLS = frozenset({"common.save_upload"})


def _route_decorated(node) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and isinstance(d.func.value, ast.Name)
        and d.func.value.id == "router"
        for d in node.decorator_list
    )


def _app_bound_names(tree: ast.Module) -> set[str]:
    """Names in this module that reach app code — imported from the package, or
    defined here. A call through any of them can block; a call through `int`,
    `HTTPException` or a method on a local cannot be spotted this way and does
    not need to be, because none of them touch the database."""
    names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:  # relative == in-package
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _called_names(node) -> list[str]:
    """Dotted head of every call in this function body: `db.run(...)` -> 'db.run',
    `get_invoice(...)` -> 'get_invoice', `x.strip()` -> 'x.strip'."""
    out = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Name):
            out.append(sub.func.id)
        elif isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name):
            out.append(f"{sub.func.value.id}.{sub.func.attr}")
    return out


@pytest.mark.unit
@pytest.mark.parametrize("module_path", SWEPT)
def test_async_handlers_reach_app_code_only_through_a_threadpool_hop(module_path):
    """No async route handler in a swept module may call app code on the loop.

    A `db.` prefix alone would not have caught the original defect: most of these
    handlers reached the database through a lookup helper (`get_invoice`,
    `_get_event`) rather than through `db` directly. So the fence is drawn wider
    — any name that is defined in the module or imported from inside the package
    is app code, and app code only runs behind `run_in_threadpool`.
    """
    tree = ast.parse((ROOT / module_path).read_text())
    app_names = _app_bound_names(tree)

    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or not _route_decorated(node):
            continue
        for call in _called_names(node):
            head = call.split(".")[0]
            if head in app_names and call not in LOOP_SAFE_CALLS:
                offenders.append(f"{node.name} calls {call}")

    assert offenders == [], f"{module_path}: app code on the event loop — {'; '.join(offenders)}"


@pytest.mark.unit
@pytest.mark.parametrize("module_path", SWEPT)
def test_threadpool_targets_in_swept_modules_are_sync(module_path):
    """run_in_threadpool runs its target with no loop under it, so an `async def`
    there would hand back an un-awaited coroutine and silently do nothing."""
    tree = ast.parse((ROOT / module_path).read_text())
    async_defs = {node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}

    targets = []
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "run_in_threadpool"
            and call.args
            and isinstance(call.args[0], ast.Name)
        ):
            targets.append(call.args[0].id)

    assert [t for t in targets if t in async_defs] == []
