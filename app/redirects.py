"""Redirect targets built from request data.

CodeQL's URL-redirect query treats any request value that reaches
``RedirectResponse`` as an open redirect, including ids interpolated into a
fixed path. The check below is the one the query recognizes: drop backslashes
(browsers accept them as slashes; ``urlparse`` does not), then require an empty
scheme and host. Callers that need an off-site target pass an explicit host
allowlist instead.
"""

from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

# Checkout Session.url is https://checkout.stripe.com/... in production. Tests
# stub the same call with the reserved .test host; nothing else is a checkout.
STRIPE_CHECKOUT_HOSTS = frozenset({"checkout.stripe.com", "checkout.stripe.test"})


def redirect_local(url: str, status_code: int = 303) -> RedirectResponse:
    """Redirect to a same-origin path. Reject scheme, host, and protocol-relative."""
    target = url.replace("\\", "")
    if (
        not urlparse(target).netloc
        and not urlparse(target).scheme
        and target.startswith("/")
        and not target.startswith("//")
        and "\r" not in target
        and "\n" not in target
    ):
        return RedirectResponse(target, status_code=status_code)
    raise HTTPException(status_code=400, detail="invalid redirect")


def redirect_https_allowlist(
    url: str, allowed_hosts: frozenset[str], status_code: int = 303
) -> RedirectResponse:
    """Redirect to https on a host in ``allowed_hosts``. Anything else is a 400."""
    target = url.replace("\\", "")
    if urlparse(target).scheme == "https" and urlparse(target).netloc in allowed_hosts:
        return RedirectResponse(target, status_code=status_code)
    raise HTTPException(status_code=400, detail="invalid redirect")
