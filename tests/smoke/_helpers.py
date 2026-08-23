"""Shared helpers for the smoke suite."""

import hashlib
import hmac
import io
import json
import os
import tempfile
import time

from fastapi.testclient import TestClient
from PIL import Image

from app import config, db
from app.main import app


def _jpeg_bytes(w=800, h=600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (180, 90, 40)).save(buf, "JPEG")
    return buf.getvalue()


def _logo_png(w=300, h=150, color=(0, 200, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _close(a, b, tol=12) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _mp4_bytes(seconds=2, w=128, h=96) -> bytes:
    """A real tiny mp4 via ffmpeg so the transcode pipeline runs for real (no mocks).
    2s long so the poster grab at -ss 1 has a frame to land on."""
    import subprocess
    from pathlib import Path

    path = tempfile.mktemp(suffix=".mp4")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=duration={seconds}:size={w}x{h}:rate=10",
                "-pix_fmt",
                "yuv420p",
                path,
            ],
            check=True,
            capture_output=True,
        )
        return Path(path).read_bytes()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _ready_photo_gallery(admin, title="Ready Photo Gallery", pin="1234"):
    """Create one gallery with a ready photo asset through the real upload path."""

    r = admin.post(
        "/admin/galleries",
        data={"title": title, "client_name": "Chef"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    with TestClient(app):  # fresh lifespan: job pool may have been stopped upstream
        r = admin.post(
            f"/admin/galleries/{g['id']}/upload",
            files=[("files", ("dish.jpg", _jpeg_bytes(), "image/jpeg"))],
        )
        assert r.status_code == 200 and r.json()["accepted"] == 1
        for _ in range(50):
            a = db.one("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))
            if a and a["status"] == "ready":
                break
            time.sleep(0.2)
    assert a and a["status"] == "ready" and a["width"] == 800
    return g, a


def _ready_video(admin, title="Reel Review", pin="1234"):
    """Create a published gallery with one ready video + one photo; return
    (gallery_row, video_asset_row, photo_asset_row). Shared setup for the
    comment tests — uses the real ffmpeg transcode path like the pipeline test."""

    admin.post("/admin/galleries", data={"title": title}, follow_redirects=False)
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    with TestClient(app):  # fresh lifespan: job pool may have been stopped upstream
        admin.post(
            f"/admin/galleries/{g['id']}/upload",
            files=[("files", ("reel.mp4", _mp4_bytes(), "video/mp4"))],
        )
        admin.post(
            f"/admin/galleries/{g['id']}/upload",
            files=[("files", ("dish.jpg", _jpeg_bytes(), "image/jpeg"))],
        )
        for _ in range(100):
            assets = db.all_("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))
            if assets and all(a["status"] == "ready" for a in assets):
                break
            time.sleep(0.2)
    vid = db.one("SELECT * FROM assets WHERE gallery_id=? AND kind='video'", (g["id"],))
    photo = db.one("SELECT * FROM assets WHERE gallery_id=? AND kind='photo'", (g["id"],))
    assert vid and vid["status"] == "ready" and photo
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={"title": title, "pin": pin, "published": "true"},
    )
    return g, vid, photo


def _stripe_sig(payload: bytes, secret: str) -> str:
    import hashlib as _hl
    import hmac as _hmac
    import time as _t

    t = int(_t.time())
    mac = _hmac.new(secret.encode(), f"{t}.".encode() + payload, _hl.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _checkout_event(
    event_id, invoice_id, kind, amount, payment_status="paid", etype="checkout.session.completed"
):
    import json as _json

    return _json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": "2024-06-20",
            "type": etype,
            "data": {
                "object": {
                    "id": f"cs_{event_id}",
                    "object": "checkout.session",
                    "payment_status": payment_status,
                    "amount_total": amount,
                    "currency": "usd",
                    "metadata": {"invoice_id": str(invoice_id), "kind": kind},
                }
            },
        }
    ).encode()


def _bind_invoice_checkout(invoice_id, event_id, kind, amount):
    """Bind a synthetic Stripe event to the same authoritative snapshot as /pay."""
    db.run(
        """UPDATE invoices
              SET stripe_session_id=?, stripe_checkout_amount_cents=?,
                  stripe_checkout_kind=?, stripe_checkout_currency='usd'
            WHERE id=?""",
        (f"cs_{event_id}", amount, kind, invoice_id),
    )


def _spark_rect_count(html: str) -> int:
    """Count sparkline bars only — nav SVG icons also use <rect>."""
    start = html.index('class="sparklines"')
    end = html.index("</section>", start)
    return html[start:end].count("<rect")


def _seam_license_with_gallery(admin, name, company, slug):
    """Build a client + gallery + a license on that gallery (published OFF,
    'print' channel granted) — the linkage the H3 render seam keys off."""
    admin.post(
        "/admin/studio/clients", data={"name": name, "company": company}, follow_redirects=False
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    gid = db.run(
        "INSERT INTO galleries (client_id, title, slug, pin) VALUES (?,?,?,?)",
        (c["id"], f"{name} Gallery", slug, "0000"),
    )
    admin.post(
        f"/admin/studio/clients/{c['id']}/licenses",
        data={"title": f"{name} license"},
        follow_redirects=False,
    )
    lic_id = db.one("SELECT id FROM licenses ORDER BY id DESC LIMIT 1")["id"]
    db.run(
        """UPDATE licenses SET gallery_id=?, channels='["print"]', published=0
              WHERE id=?""",
        (gid, lic_id),
    )
    return c, gid, lic_id


def _created_id(r) -> int:
    """The id of the row a studio create route just made — its 303 target."""
    assert r.status_code == 303, r.status_code
    return int(r.headers["location"].rsplit("/", 1)[1])


# The chain's proposal is priced $1000.00 + 2 × $75.50 = $1151.00 and its invoice
# splits off a $500.00 deposit; the lifecycle tests assert those exact figures.
_CHAIN_ITEMS = {
    "item_label_0": "Half-day session",
    "item_qty_0": "1",
    "item_price_0": "1000",
    "item_label_1": "Extra dishes",
    "item_qty_1": "2",
    "item_price_1": "75.50",
}


def _studio_chain(admin, title, *, through="project", monkeypatch=None):
    """Build one studio lifecycle chain over the real routes; return its ids.

    ``through`` picks how far to build, so a lifecycle test owns the rows it
    reads instead of inheriting whichever client/project/invoice an earlier test
    happened to leave newest:

      "project"  → client + project (inquiry_received)
      "proposal" → + a sent proposal the client accepted, total $1151.00
      "invoice"  → + an invoice carrying a $500.00 deposit, sent and then paid in
                   full through the Stripe webhook (which lands the project on
                   retainer_paid and enqueues 3 notion_sync_invoice jobs)

    ``monkeypatch`` is required for "invoice": the webhook 503s unless
    config.STRIPE_WEBHOOK_SECRET is set.
    """
    assert through in ("project", "proposal", "invoice"), through
    client_id = _created_id(
        admin.post(
            "/admin/studio/clients",
            data={
                "name": "Dana Chef",
                "company": "Test Bistro",
                "email": "dana@bistro.com",
                "phone": "",
            },
            follow_redirects=False,
        )
    )
    project_id = _created_id(
        admin.post(
            f"/admin/studio/clients/{client_id}/projects",
            data={"title": title},
            follow_redirects=False,
        )
    )
    chain = {
        "client_id": client_id,
        "project_id": project_id,
        "proposal_id": None,
        "invoice_id": None,
    }
    if through == "project":
        return chain

    proposal_id = _created_id(
        admin.post(
            f"/admin/studio/projects/{project_id}/proposals",
            data={"preset": "photo_starter"},
            follow_redirects=False,
        )
    )
    r = admin.post(
        f"/admin/studio/proposals/{proposal_id}", data=_CHAIN_ITEMS, follow_redirects=False
    )
    assert r.status_code == 303
    r = admin.post(f"/admin/studio/proposals/{proposal_id}/send", follow_redirects=False)
    assert r.status_code == 303
    slug = db.one("SELECT slug FROM proposals WHERE id=?", (proposal_id,))["slug"]
    r = admin.post(f"/p/{slug}/accept", follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT status, total_cents FROM proposals WHERE id=?", (proposal_id,))
    assert d["status"] == "accepted" and d["total_cents"] == 115100
    chain["proposal_id"] = proposal_id
    if through == "proposal":
        return chain

    invoice_id = _created_id(
        admin.post(f"/admin/studio/projects/{project_id}/invoices", follow_redirects=False)
    )
    r = admin.post(
        f"/admin/studio/invoices/{invoice_id}",
        data={
            "item_label_0": "Shoot package",
            "item_qty_0": "1",
            "item_price_0": "1151",
            "deposit": "500",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = admin.post(f"/admin/studio/invoices/{invoice_id}/send", follow_redirects=False)
    assert r.status_code == 303
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    for kind, cents in (("deposit", 50000), ("balance", 65100)):
        event_id = f"evt_chain_{kind}_{invoice_id}"
        _bind_invoice_checkout(invoice_id, event_id, kind, cents)
        body = _checkout_event(event_id, invoice_id, kind, cents)
        assert _post_signed(admin, body).status_code == 200
    d = db.one("SELECT status, total_cents, deposit_cents FROM invoices WHERE id=?", (invoice_id,))
    assert d["status"] == "paid" and d["total_cents"] == 115100 and d["deposit_cents"] == 50000
    chain["invoice_id"] = invoice_id
    return chain


def _drop_studio_chain(admin, chain):
    """Delete a chain's rows once its test is done. Sync jobs go first: a queued
    one left pointing at a deleted invoice would fail and show up in the global
    "N jobs failed" badge other files assert on. Projects, documents and payments
    cascade off the client."""
    if chain["invoice_id"]:
        db.run(
            """DELETE FROM jobs WHERE kind='notion_sync_invoice'
                  AND json_extract(payload,'$.invoice_id')=?""",
            (chain["invoice_id"],),
        )
    r = admin.post(
        f"/admin/studio/clients/{chain['client_id']}/delete",
        data={"force": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _quo_sig(secret_b64: str, raw: bytes, ts: str | None = None) -> str:
    """Build a valid openphone-signature header for `raw` (mirrors sms.verify_webhook)."""
    import base64

    ts = ts or str(int(time.time() * 1000))
    key = base64.b64decode(secret_b64)
    sig = base64.b64encode(
        hmac.new(key, ts.encode() + b"." + raw, hashlib.sha256).digest()
    ).decode()
    return f"hmac;1;{ts};{sig}"


def _seed_money_chain(*, project_status, total=90000, deposit=0):
    """A throwaway client → project → invoice chain. project_status pins where the
    funnel sits before the payment lands, so a test can assert the advance — or,
    deliberately, the non-advance. Returns (client_id, project_id, invoice_id)."""
    cid = db.run(
        "INSERT INTO clients (name, email) VALUES (?,?)", ("Webhook Diner", "wh@diner.test")
    )
    pid = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
        (cid, "Tasting menu shoot", project_status),
    )
    iid = db.run(
        "INSERT INTO invoices (project_id, slug, title, total_cents, deposit_cents, status) "
        "VALUES (?,?,?,?,?,?)",
        (pid, f"wh-{pid}", "Tasting invoice", total, deposit, "sent"),
    )
    return cid, pid, iid


def _cleanup_money_chain(cid, pid, iid):
    db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
    db.run("DELETE FROM invoices WHERE id=?", (iid,))
    db.run("DELETE FROM projects WHERE id=?", (pid,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))


def _post_signed(client, body):
    return client.post(
        "/webhooks/stripe",
        content=body,
        headers={"stripe-signature": _stripe_sig(body, "whsec_test")},
    )
