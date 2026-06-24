"""Cookies, PIN lockout, slugs, client IP resolution."""

import logging
import secrets
import string
import time

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import alerts, config, db, features

log = logging.getLogger("mise.security")

_BASE62 = string.ascii_letters + string.digits


def _serializer() -> URLSafeTimedSerializer:
    if not config.SECRET_KEY:
        raise RuntimeError("MISE_SECRET_KEY is not set")
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="mise")


def new_slug(n: int = 14) -> str:
    return "".join(secrets.choice(_BASE62) for _ in range(n))


def new_pin() -> str:
    return f"{secrets.randbelow(10000):04d}"


def sign(value: str) -> str:
    return _serializer().dumps(value)


def unsign(token: str) -> str | None:
    try:
        return _serializer().loads(token, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        return None


def client_ip(request: Request) -> str:
    """Peer IP, or CF-Connecting-IP ONLY when the peer is local (cloudflared)."""
    peer = request.client.host if request.client else "?"
    if peer in ("127.0.0.1", "::1"):
        return request.headers.get("cf-connecting-ip", peer)
    return peer


# ── PIN lockout ────────────────────────────────────────────────────────────


def pin_locked(ip: str, gallery_id: int) -> bool:
    cutoff = time.time() - config.PIN_LOCKOUT_MIN * 60
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (ip, gallery_id, cutoff),
    )
    return row["n"] >= config.PIN_MAX_FAILS


def pin_fail(ip: str, gallery_id: int) -> None:
    db.run(
        "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)",
        (ip, gallery_id, time.time()),
    )
    db.run("DELETE FROM pin_attempts WHERE ts < ?", (time.time() - 86400,))
    log.warning("bad PIN for gallery %s from %s", gallery_id, ip)
    # Anomaly-only alert: fire the instant the lockout threshold is crossed (not on
    # every typo, not on Kevin's normal login). gallery_id 0 = admin login bucket.
    cutoff = time.time() - config.PIN_LOCKOUT_MIN * 60
    n = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (ip, gallery_id, cutoff),
    )["n"]
    if n == config.PIN_MAX_FAILS:
        what = "admin login" if gallery_id == 0 else f"gallery {gallery_id}"
        alerts.security_alert(
            f"{config.PIN_MAX_FAILS} failed {what} attempts from {ip} — "
            f"locked out {config.PIN_LOCKOUT_MIN}m"
        )


def pin_clear(ip: str, gallery_id: int) -> None:
    db.run("DELETE FROM pin_attempts WHERE ip=? AND gallery_id=?", (ip, gallery_id))


# ── Inquiry-form throttle (per IP, per hour) ──────────────────────────────
# Piggybacks on the pin_attempts table with negative pseudo-gallery-id sentinels
# so legitimate clients trying to PIN into a gallery never collide with the
# inquiry-form bucket. -2 = /contact bucket, -3 = /book bucket. (PIN-attempt
# rows for portals already use negative ids — different magnitude range.)
INQUIRY_BUCKET_CONTACT = -2
INQUIRY_BUCKET_BOOK = -3
INQUIRY_BUCKET_FORM = -4  # public custom forms (/forms/{slug})
INQUIRY_WINDOW_SEC = 3600
INQUIRY_MAX_PER_WINDOW = 3


def inquiry_throttled(ip: str, bucket: int) -> bool:
    cutoff = time.time() - INQUIRY_WINDOW_SEC
    row = db.one(
        "SELECT COUNT(*) AS n FROM pin_attempts WHERE ip=? AND gallery_id=? AND ts>?",
        (ip, bucket, cutoff),
    )
    return row["n"] >= INQUIRY_MAX_PER_WINDOW


def inquiry_record(ip: str, bucket: int) -> None:
    db.run(
        "INSERT INTO pin_attempts (ip, gallery_id, ts) VALUES (?,?,?)", (ip, bucket, time.time())
    )
    db.run("DELETE FROM pin_attempts WHERE ts < ?", (time.time() - max(86400, INQUIRY_WINDOW_SEC),))


# ── Visitor cookies (per gallery) ──────────────────────────────────────────


def visitor_cookie_name(gallery_id: int) -> str:
    return f"mise_g{gallery_id}"


def get_visitor(request: Request, gallery_id: int) -> "db.sqlite3.Row | None":
    raw = request.cookies.get(visitor_cookie_name(gallery_id))
    if not raw:
        return None
    token = unsign(raw)
    if not token:
        return None
    v = db.one("SELECT * FROM visitors WHERE token=? AND gallery_id=?", (token, gallery_id))
    if v:
        db.run("UPDATE visitors SET last_seen=datetime('now') WHERE id=?", (v["id"],))
    return v


def create_visitor(gallery_id: int) -> tuple[int, str]:
    """Returns (visitor_id, signed cookie value)."""
    token = secrets.token_urlsafe(24)
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (gallery_id, token))
    return vid, sign(token)


def require_visitor(request: Request, gallery_id: int) -> "db.sqlite3.Row":
    v = get_visitor(request, gallery_id)
    if not v:
        raise HTTPException(status_code=403, detail="gallery access required")
    return v


def set_session_cookie(
    response: Response,
    name: str,
    value: str,
    *,
    max_age: int | None = None,
    path: str = "/",
) -> None:
    """Set a site session cookie with one hardened policy shared by every route."""
    response.set_cookie(
        name,
        value,
        max_age=config.SESSION_MAX_AGE if max_age is None else max_age,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path=path,
    )


def set_signed_session_cookie(
    response: Response,
    name: str,
    payload: str,
    *,
    max_age: int | None = None,
    path: str = "/",
) -> None:
    set_session_cookie(response, name, sign(payload), max_age=max_age, path=path)


def delete_session_cookie(response: Response, name: str, *, path: str = "/") -> None:
    response.delete_cookie(name, path=path)


# ── Admin session ──────────────────────────────────────────────────────────

ADMIN_COOKIE = "mise_admin"


def is_admin(request: Request) -> bool:
    raw = request.cookies.get(ADMIN_COOKIE)
    return bool(raw) and unsign(raw) == "admin"


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def check_admin_password(password: str) -> bool:
    if not config.ADMIN_PASSWORD:
        return False
    return secrets.compare_digest(password, config.ADMIN_PASSWORD)


def require_argus_token(request: Request) -> None:
    """Bearer gate for the published-gallery index (config.ARGUS_TOKEN).

    Token unset -> 503: disarmed until MISE_ARGUS_TOKEN is provisioned on flow.
    """
    if not config.ARGUS_TOKEN:
        raise HTTPException(status_code=503, detail="galleries api disarmed")
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {config.ARGUS_TOKEN}"
    if not secrets.compare_digest(header, expected):
        raise HTTPException(status_code=401, detail="bad token")


def require_shots_token(request: Request) -> None:
    """Bearer gate for the shot-list read API (config.SHOTS_TOKEN).

    Token unset -> 503: the endpoint is disarmed, not merely unauthorized. This is the
    deliberate dormant state on flow until Kevin provisions MISE_SHOTS_TOKEN, and it
    reads differently from a real auth failure (401) for an arming caller.
    """
    if not features.shots_api_enabled():
        raise HTTPException(status_code=503, detail="shots api disarmed")
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {config.SHOTS_TOKEN}"
    if not secrets.compare_digest(header, expected):
        raise HTTPException(status_code=401, detail="bad token")
