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

from app import db
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
                    "metadata": {"invoice_id": str(invoice_id), "kind": kind},
                }
            },
        }
    ).encode()


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


def _quo_sig(secret_b64: str, raw: bytes, ts: str = "1700000000") -> str:
    """Build a valid openphone-signature header for `raw` (mirrors sms.verify_webhook)."""
    import base64

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
