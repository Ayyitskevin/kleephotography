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

An explicit malformed or opaque Origin (including `Origin: null`) is present but
cannot prove same-origin, so it is rejected. This matters after a cross-origin
redirect, where browsers may deliberately serialize the tainted origin as `null`.

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
    try:
        s = urlsplit(url)
        hostname = s.hostname
        port = s.port
    except ValueError:
        return None
    if not s.scheme or not hostname:
        return None
    netloc = hostname + (f":{port}" if port else "")
    return f"{s.scheme}://{netloc}".lower()


def check(request: Request) -> JSONResponse | None:
    """Return a 403 response for a cross-origin state-changing request, else None."""
    if request.method in _SAFE_METHODS:
        return None
    ours = _origin(config.BASE_URL)
    source = request.headers.get("origin")
    if source is None:
        source = request.headers.get("referer")
    if source is None:
        # Server-to-server integrations generally send neither browser header.
        return None
    sent = _origin(source)
    if sent == ours:
        return None
    log.warning(
        "cross-origin %s %s blocked: origin=%s expected=%s",
        request.method,
        request.url.path,
        sent,
        ours,
    )
    return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
