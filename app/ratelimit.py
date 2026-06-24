"""In-memory per-IP sliding-window rate limiter (single uvicorn worker).

Guards abuse-prone routes (downloads/ZIP builds, public form POSTs, admin) WITHOUT
touching the thumbnail grid — a gallery legitimately bursts dozens of /media/
requests on load, so those are exempt. Logged-in admins are exempt so deploys and
post-deploy testing never trip it. State is in-process: it resets on restart, which
is fine for rate limiting and avoids a DB write on every request (which would itself
be a DoS amplifier). Single worker means one shared view of the window.
"""

import logging
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config, security

log = logging.getLogger("mise.ratelimit")

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_last_gc = 0.0


def _bucket_for(path: str) -> str | None:
    """Bucket name to charge, or None to skip (exempt)."""
    if path == "/healthz" or path.startswith(("/static/", "/media/", "/site/img/", "/work/")):
        return None  # static + media grid: legit bursts, never limited
    if "/download" in path:
        return "download"
    if path.startswith("/admin"):
        return "admin"
    if path.startswith(("/g/", "/portal/", "/i/", "/p/", "/contact", "/book", "/forms/")):
        return "public"
    return None


def _gc(now: float) -> None:
    """Drop empty/stale deques so memory stays bounded across many IPs."""
    global _last_gc
    if now - _last_gc < 300:
        return
    _last_gc = now
    for key in [k for k, dq in _hits.items() if not dq or dq[-1] < now - 3600]:
        _hits.pop(key, None)


def check(request: Request, path: str) -> JSONResponse | None:
    """Return a 429 response if over limit, else None (and record the hit)."""
    bucket = _bucket_for(path)
    if bucket is None or security.is_admin(request):
        return None
    limit, window = config.RATE_LIMITS[bucket]
    ip = security.client_ip(request)
    now = time.time()
    _gc(now)
    dq = _hits[(ip, bucket)]
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= limit:
        retry = int(dq[0] + window - now) + 1
        log.warning("rate limit hit: %s bucket=%s ip=%s", path, bucket, ip)
        return JSONResponse(
            {"detail": "Too many requests — slow down."},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    dq.append(now)
    return None
