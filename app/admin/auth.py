import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import security
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
    # Sign a per-login server-side session token into the cookie (not a constant),
    # so this session can be revoked independently at logout.
    token = security.create_admin_session()
    security.set_signed_session_cookie(resp, security.ADMIN_COOKIE, token)
    log.info("admin login from %s", ip)
    return resp


@router.post("/logout")
async def logout(request: Request, everywhere: str = Form("")):
    # Real revocation: delete this session's server-side row so the cookie is dead
    # even if it was copied elsewhere. `everywhere` kills ALL admin sessions (the
    # emergency switch for a known-leaked cookie on a device you can't reach).
    if everywhere:
        security.destroy_all_admin_sessions()
    else:
        security.destroy_admin_session(request)
    resp = RedirectResponse("/admin/login", status_code=303)
    security.delete_session_cookie(resp, security.ADMIN_COOKIE)
    return resp
