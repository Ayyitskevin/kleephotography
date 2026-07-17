"""
Mise — self-hosted F&B photography delivery · FastAPI + HTMX · port 8400

  uvicorn app.main:app --host 127.0.0.1 --port 8400
"""

import logging
import secrets
from contextlib import asynccontextmanager
from urllib.parse import quote_from_bytes, urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (
    alerts,
    config,
    csrf,
    db,
    jobs,
    ops_monitor,
    ratelimit,
    scheduler,
    service_api,
)
from .admin import (
    activity,
    audit,
    auth,
    content,
    contracts,
    doc_templates,
    email_templates,
    emails,
    financials,
    forms,
    galleries,
    inbox,
    invoices,
    licenses,
    portals,
    presets,
    press,
    proposals,
    recurring,
    reference,
    reports,
    search,
    settings,
    share,
    shotlist,
    studio,
    uploads,
)
from .admin import scheduling as admin_scheduling
from .public import docs, downloads, gallery, media, pay, portal, site, sms_webhook, workspace
from .public import forms as public_forms
from .public import scheduling as public_scheduling
from .render import ROOT, templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mise.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    jobs.start()
    scheduler.start()
    log.info("Mise up on :%s · data=%s", config.PORT, config.DATA_DIR)
    yield
    scheduler.stop()
    jobs.stop()


app = FastAPI(
    title="Mise",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# script-src carries NO 'unsafe-inline': every on*-attribute moved to
# data-attributes handled by /static/behaviors.js, and the few genuinely inline
# <script> blocks (pre-paint theme, page-local widgets with Jinja data) carry a
# per-request nonce — so injected markup can't execute script even if it slips
# past autoescaping. style-src keeps 'unsafe-inline': inline style= attributes
# are pervasive (progress widths, board colors), style injection is a far
# weaker vector than script, and removing it buys little for a large diff.
# Plausible is the only off-origin asset, and only when analytics is enabled.
CSP_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "frame-src 'none'",
        "form-action 'self'",
        "img-src 'self' data: blob:",
        "media-src 'self'",
        "font-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self' 'nonce-{nonce}' https://plausible.io",
        "connect-src 'self' https://plausible.io",
    )
)

# Browser features the site never uses, switched off outright so injected or
# third-party script can't quietly reach them. fullscreen / picture-in-picture /
# autoplay stay at their defaults — the gallery lightbox and reels rely on the
# native <video> controls, which need them.
PERMISSIONS_POLICY = ", ".join(
    (
        "camera=()",
        "microphone=()",
        "geolocation=()",
        "payment=()",  # Stripe runs on stripe.com via redirect, never on-origin
        "usb=()",
        "browsing-topics=()",
    )
)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    blocked = ratelimit.check(request, request.url.path)
    if blocked is not None:
        return blocked
    return await call_next(request)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    blocked = csrf.check(request)
    if blocked is not None:
        return blocked
    return await call_next(request)


_ORIGIN_BYPASS_PATHS = ("/healthz", "/api/")
_DEFAULT_ORIGIN_PORTS = {"http": 80, "https": 443}
_CANONICAL_REDIRECT_METHODS = frozenset({"GET", "HEAD"})
_LOCATION_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_LOCATION_QUERY_SAFE = "%:/?@!$&'()*+,;=-._~"


def _origin_key(scheme: str, hostname: str | None, port: int | None) -> tuple[str, str, int | None]:
    normalized_scheme = scheme.lower()
    return (
        normalized_scheme,
        (hostname or "").lower(),
        port if port is not None else _DEFAULT_ORIGIN_PORTS.get(normalized_scheme),
    )


def _request_origin(request: Request) -> tuple[str, str, int | None] | None:
    """Return a normalized origin, treating a malformed Host as noncanonical."""
    try:
        return _origin_key(request.url.scheme, request.url.hostname, request.url.port)
    except ValueError:
        return None


def _canonical_location(request: Request) -> str:
    raw_path = request.scope.get("raw_path")
    if raw_path is None:
        raw_path = request.scope["path"].encode("utf-8")
    path = quote_from_bytes(raw_path, safe=_LOCATION_PATH_SAFE)
    query = quote_from_bytes(request.scope.get("query_string", b""), safe=_LOCATION_QUERY_SAFE)
    suffix = f"?{query}" if query else ""
    return f"{config.BASE_URL}{path}{suffix}"


@app.middleware("http")
async def canonical_origin(request: Request, call_next):
    """Redirect browser surfaces to the configured HTTPS host.

    Cloudflare must still redirect at the edge: an origin redirect cannot stop a
    direct HTTP POST body from crossing the public network first. This guard is
    defense in depth and keeps www/noncanonical links from serving duplicate
    content. Unsafe methods are rejected instead of redirected so browsers never
    replay a state-changing body across origins. Health and bearer-gated service
    APIs stay available on the private origin so monitoring and fleet integrations
    do not break.
    """
    if not config.CANONICAL_REDIRECTS or request.url.path == _ORIGIN_BYPASS_PATHS[0]:
        return await call_next(request)
    if request.url.path.startswith(_ORIGIN_BYPASS_PATHS[1]):
        return await call_next(request)

    canonical = urlsplit(config.BASE_URL)
    request_origin = _request_origin(request)
    canonical_origin = _origin_key(canonical.scheme, canonical.hostname, canonical.port)
    if request_origin == canonical_origin:
        return await call_next(request)

    if request.method not in _CANONICAL_REDIRECT_METHODS:
        return JSONResponse(
            {"detail": "canonical origin required"},
            status_code=421,
            headers={"Cache-Control": "no-store"},
        )

    return RedirectResponse(
        _canonical_location(request),
        status_code=308,
        headers={"Cache-Control": "no-store"},
    )


@app.middleware("http")
async def common_headers(request: Request, call_next):
    # Fresh CSP nonce per request; templates read it via the csp_nonce context
    # var (render.py) so inline <script nonce=…> blocks match the header below.
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    resp = await call_next(request)
    p = request.url.path
    if not (p in site.INDEXABLE or p.startswith(("/site/img/", "/static/", "/work/"))):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = CSP_POLICY.format(nonce=nonce)
    resp.headers["Permissions-Policy"] = PERMISSIONS_POLICY
    # HSTS only when we know we're served over TLS (same signal as Secure
    # cookies) — sending it for a plain-http dev origin would wrongly pin
    # localhost to https. Start with a reversible five-minute policy; longer
    # retention and subdomain coverage require a separate TLS inventory.
    if config.COOKIE_SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=300"
    # Static assets are safe to cache forever: every /static URL carries a
    # content-derived ?v={{ static_rev }} buster (see app/render.py), so a file
    # change changes the URL. Without this the ~300KB stylesheet + fonts + JS
    # get revalidated on every repeat navigation.
    if 300 <= resp.status_code < 400 and "location" in resp.headers:
        # Redirect targets can change during rollback; never let browsers or the
        # edge pin even a permanent redirect response.
        resp.headers.setdefault("Cache-Control", "no-store")
    elif p.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif resp.headers.get("content-type", "").startswith("text/html"):
        # HTML must revalidate: a page carries ?v={{ static_rev }} asset URLs and a
        # per-request CSP nonce, so a heuristically-cached copy pins stale CSS/JS
        # (e.g. a redeploy's dark-mode fix never appears) or a stale nonce. Only the
        # versioned /static assets above are safe to keep forever.
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


_ERROR_MESSAGES = {
    403: "You need to unlock this page first — use the link and PIN from your email.",
    404: "That link doesn't go anywhere — double-check it, or use the link from your email.",
    410: "This link has expired. Get in touch to have it re-opened.",
}


@app.exception_handler(StarletteHTTPException)
async def branded_errors(request: Request, exc: StarletteHTTPException):
    # browsers get a branded page; JSON/HTMX/media requests keep plain codes
    if exc.status_code in _ERROR_MESSAGES and "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request,
            "public/error.html",
            {"message": _ERROR_MESSAGES[exc.status_code]},
            status_code=exc.status_code,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_errors(request: Request, exc: Exception):
    # An uncaught exception means a 500 the user already hit — make it loud.
    # Log the full traceback for debugging, fire ONE throttled Telegram alert so
    # Kevin hears about the bug while the app is still up, then return a branded
    # 500 (HTML) / plain 500 (API) without leaking the exception detail.
    log.exception("unhandled error: %s %s", request.method, request.url.path)
    alerts.error_alert(
        f"{request.method} {request.url.path}|{type(exc).__name__}",
        f"{type(exc).__name__} on {request.method} {request.url.path}: {str(exc)[:300]}",
    )
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request,
            "public/error.html",
            {
                "message": "Something went wrong on our end. "
                "Try again in a moment, or get in touch if it persists."
            },
            status_code=500,
        )
    return JSONResponse({"detail": "internal server error"}, status_code=500)


@app.get("/healthz")
async def healthz():
    payload = {
        "ok": True,
        "service": "mise",
        "db_connected": False,
        "jobs_pending": None,
        "jobs_failed": None,
        "disk_free_gb": None,
        "disk_low": None,
        "backup_present": None,
        "backup_age_hours": None,
        "backup_stale": None,
    }
    try:
        payload.update(ops_monitor.storage_status())
    except Exception:
        log.exception("healthz storage check failed")
    try:
        db.one("SELECT 1 AS ok")
        payload["db_connected"] = True
        payload["jobs_pending"] = jobs.pending_count()
        payload["jobs_failed"] = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='failed'")["n"]
    except Exception:
        log.exception("healthz database check failed")
        payload["ok"] = False
        return JSONResponse(payload, status_code=503)
    return payload


for r in (
    auth.router,
    galleries.router,
    uploads.router,
    activity.router,
    studio.router,
    proposals.router,
    contracts.router,
    invoices.router,
    licenses.router,
    presets.router,
    press.router,
    recurring.router,
    reports.router,
    email_templates.router,
    doc_templates.router,
    reference.router,
    search.router,
    shotlist.router,
    emails.router,
    share.router,
    forms.router,
    audit.router,
    inbox.router,
    settings.router,
    financials.router,
    content.router,
    portals.router,
    admin_scheduling.router,
    gallery.router,
    media.router,
    downloads.router,
    docs.router,
    pay.router,
    portal.router,
    workspace.router,
    public_forms.router,
    public_scheduling.router,
    site.router,
    sms_webhook.router,
    service_api.router,
):
    app.include_router(r)
