"""Same-origin enforcement for state-changing requests — CSRF defense-in-depth.

Cookie-authenticated POSTs already lean on SameSite=lax. This adds the second,
explicit layer most frameworks ship: reject an unsafe-method request whose
Origin (or, as a fallback, Referer) names a DIFFERENT site than our own.

We reject ONLY on a present-and-mismatched header. A request with neither header
is allowed through — so server-to-server webhooks (no Origin, and HMAC-verified
in their own handlers), curl, and the test client are unaffected. The real attack
this stops is a malicious page auto-submitting a form to us: the browser stamps it
with `Origin: https://evil.example`, which mismatches and gets a 403. This can only
tighten an existing legitimate flow, never break one (R3, R12).

Origin is compared against config.BASE_URL — the PUBLIC origin — not the request's
host, because behind the Cloudflare tunnel the peer is localhost while the browser's
Origin is https://kleephotography.com.
"""

import logging
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config

log = logging.getLogger("mise.csrf")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _origin(url: str) -> str | None:
    """Normalize a URL to scheme://host[:port], or None if it has no usable origin."""
    if not url:
        return None
    s = urlsplit(url)
    if not s.scheme or not s.hostname:
        return None
    netloc = s.hostname + (f":{s.port}" if s.port else "")
    return f"{s.scheme}://{netloc}".lower()


def check(request: Request) -> JSONResponse | None:
    """Return a 403 response for a cross-origin state-changing request, else None."""
    if request.method in _SAFE_METHODS:
        return None
    ours = _origin(config.BASE_URL)
    sent = _origin(request.headers.get("origin", "")) \
        or _origin(request.headers.get("referer", ""))
    if sent is None or sent == ours:
        return None
    log.warning("cross-origin %s %s blocked: origin=%s expected=%s",
                request.method, request.url.path, sent, ours)
    return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
