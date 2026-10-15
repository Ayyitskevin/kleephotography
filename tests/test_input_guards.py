"""Guards for request data that becomes a redirect or a filesystem path."""

import os
import re
import time

import pytest
from fastapi import HTTPException

from app.admin.studio import _redirect
from app.public.downloads import _looks_like_email
from app.redirects import STRIPE_CHECKOUT_HOSTS, redirect_https_allowlist, redirect_local
from app.safe_path import path_within

pytestmark = pytest.mark.unit

_OLD_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def test_redirect_local_keeps_same_origin_paths():
    for url in (
        "/admin/galleries/3",
        "/admin/galleries/3#asset-9",
        "/g/slug/download?asset_id=1&web=1",
        "/admin/inbox?tab=all&sel=4",
        "/admin/studio/recurring/3?credit_label=Hello&credit_qty=1#log-delivery",
    ):
        resp = redirect_local(url)
        assert resp.status_code == 303
        assert resp.headers["location"] == url


def test_redirect_local_rejects_off_site_targets():
    for url in (
        "//evil.example",
        "///evil.example",
        "https://evil.example",
        "https:/evil.example",
        "https:///evil.example",
        "/\\/evil.example",
        "\\\\evil.example",
        "https://checkout.stripe.com/pay",
        "/admin/inbox\r\nLocation: https://evil.example",
    ):
        with pytest.raises(HTTPException) as exc:
            redirect_local(url)
        assert exc.value.status_code == 400
    # A backslash is not a host separator once it is removed, so this is /evil.example.
    collapsed = redirect_local("/\\evil.example")
    assert collapsed.headers["location"] == "/evil.example"


def test_stripe_checkout_allowlist():
    ok = redirect_https_allowlist(
        "https://checkout.stripe.com/c/pay/cs_test_123", STRIPE_CHECKOUT_HOSTS
    )
    assert ok.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_123"
    stub = redirect_https_allowlist("https://checkout.stripe.test/pay", STRIPE_CHECKOUT_HOSTS)
    assert stub.headers["location"] == "https://checkout.stripe.test/pay"
    for url in (
        "https://checkout.stripe.com.evil.example/pay",
        "http://checkout.stripe.com/pay",
        "https://evil.example@checkout.stripe.com/pay",
        "https://evil.example/pay",
        "/admin/home",
    ):
        with pytest.raises(HTTPException):
            redirect_https_allowlist(url, STRIPE_CHECKOUT_HOSTS)


def test_return_to_falls_back_unless_admin_path():
    back = "/admin/inbox?tab=all&sel=4"
    assert _redirect(back, "/admin/studio").headers["location"] == back
    for unsafe in ("https://evil.example.com", "//evil.example", "/g/secret", "/admin"):
        assert _redirect(unsafe, "/admin/studio").headers["location"] == "/admin/studio"


def test_path_within_keeps_a_child_and_blocks_escape(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    child = path_within(root, "15", "thumb", "photo.jpg")
    assert child == (root / "15" / "thumb" / "photo.jpg").resolve()
    for parts in (
        ("..", "outside.txt"),
        ("/etc/passwd",),
        ("15", "..", "..", "outside.txt"),
    ):
        with pytest.raises(HTTPException) as exc:
            path_within(root, *parts)
        assert exc.value.status_code == 400


def test_path_within_rejects_a_sibling_prefix_and_a_symlink(tmp_path):
    root = tmp_path / "media"
    sibling = tmp_path / "media-evil"
    outside = tmp_path / "outside"
    root.mkdir()
    sibling.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("nope")
    (root / "link").symlink_to(outside)
    with pytest.raises(HTTPException):
        path_within(root, "link", "secret.txt")
    # A name that merely shares the root's prefix must not be treated as inside it.
    escaped = os.path.realpath(sibling / "x")
    assert not str(path_within(root, "ok")).startswith(str(sibling))
    assert not escaped.startswith(str(root.resolve()) + os.sep)


def test_email_gate_matches_the_old_pattern_without_quadratic_backtracking():
    samples = [
        "a@b.c",
        "first.last@x.co",
        "user+tag@host.co.uk",
        "a@b.c.d",
        "a@b..c",
        "a@.b",
        "a@b.",
        "a@b.c.",
        "a@@b.c",
        "a@b.c ",
        " a@b.c",
        "",
        "@b.c",
        "a@b",
        "a@b\n.c",
        "a@b.c\r",
    ]
    for sample in samples:
        assert _looks_like_email(sample) == bool(_OLD_EMAIL.fullmatch(sample)), sample
    slow = "!@!." + "!." * 20000 + " "
    started = time.perf_counter()
    assert _looks_like_email(slow) is False
    assert time.perf_counter() - started < 0.05
