import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, security
from ..render import templates

log = logging.getLogger("mise.admin.auth")
router = APIRouter(prefix="/admin")


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    ip = security.client_ip(request)
    if security.pin_locked(ip, 0):
        return templates.TemplateResponse(
            request, "admin/login.html", {"error": "Locked out. Try again later."}, status_code=429
        )
    if not security.check_admin_password(password):
        security.pin_fail(ip, 0)  # gallery_id 0 = admin login bucket
        return templates.TemplateResponse(
            request, "admin/login.html", {"error": "Wrong password."}, status_code=401
        )
    security.pin_clear(ip, 0)
    resp = RedirectResponse("/admin/home", status_code=303)
    resp.set_cookie(
        security.ADMIN_COOKIE,
        security.sign("admin"),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    log.info("admin login from %s", ip)
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(security.ADMIN_COOKIE, path="/")
    return resp
