"""
Mise — self-hosted F&B photography delivery · FastAPI + HTMX · port 8400

  uvicorn app.main:app --host 127.0.0.1 --port 8400
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, db, jobs, ratelimit, scheduler, service_api
from .admin import (activity, audit, auth, contracts, doc_templates,
                    email_templates, emails, forms, galleries, invoices,
                    licenses, presets, press, proposals, recurring, reference,
                    reports, search, share, shotlist, studio, uploads)
from .admin import scheduling as admin_scheduling
from .public import docs, downloads, gallery, media, pay, portal, site, workspace
from .public import forms as public_forms
from .public import scheduling as public_scheduling
from .render import ROOT, templates

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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


app = FastAPI(title="Mise", version="0.1.0", lifespan=lifespan, docs_url=None,
              redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    blocked = ratelimit.check(request, request.url.path)
    if blocked is not None:
        return blocked
    return await call_next(request)


@app.middleware("http")
async def common_headers(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if not (p in site.INDEXABLE or p.startswith(("/site/img/", "/static/", "/work/"))):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


_ERROR_MESSAGES = {
    403: "You need to unlock this page first — use the link and PIN from your email.",
    404: "That link doesn't go anywhere — double-check it, or use the link from your email.",
    410: "This link has expired. Get in touch to have it re-opened.",
}


@app.exception_handler(StarletteHTTPException)
async def branded_errors(request: Request, exc: StarletteHTTPException):
    # browsers get a branded page; JSON/HTMX/media requests keep plain codes
    if exc.status_code in _ERROR_MESSAGES and \
            "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request, "public/error.html",
            {"message": _ERROR_MESSAGES[exc.status_code]},
            status_code=exc.status_code)
    return await http_exception_handler(request, exc)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "mise", "jobs_pending": jobs.pending_count()}


for r in (auth.router, galleries.router, uploads.router, activity.router,
          studio.router, proposals.router, contracts.router, invoices.router,
          licenses.router, presets.router, press.router, recurring.router,
          reports.router, email_templates.router, doc_templates.router,
          reference.router, search.router,
          shotlist.router, emails.router, share.router, forms.router,
          audit.router,
          admin_scheduling.router,
          gallery.router, media.router,
          downloads.router, docs.router, pay.router, portal.router, workspace.router,
          public_forms.router, public_scheduling.router, site.router,
          service_api.router):
    app.include_router(r)
