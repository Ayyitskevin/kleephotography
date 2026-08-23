"""`sent` → `viewed` must mean a person opened the document, not a probe.

Corporate link scanners and preview unfurlers GET every URL in an email the
moment it lands. The method branch also refuses any non-GET, so a future
uptime checker sends a HEAD to whatever URL it is pointed at. Kevin acts on
"they've viewed it", so the flip is gated on app/public/telemetry.py.

Two things this file has to do that the first attempt did not:

* **Isolate every signal.** Each of the four suppression branches gets a case
  that sends that signal ALONE, so deleting the branch makes a named test fail.
  A negative case that trips two branches at once proves nothing about either.
* **Pin the over-match hazards with strings that actually collide.** A real
  browser wrongly read as a machine loses a view invisibly and forever, so the
  in-app-browser corpus below is the load-bearing half of the file.
"""

import itertools

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import db
from app.main import app
from app.public import telemetry

# ── user-agent corpus ───────────────────────────────────────────────────────

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Safari only shipped Sec-Fetch-* in 16.4 (March 2023), so "no Sec-Fetch at all"
# is an ordinary iPhone, not an exotic client. IE11 stands in for the same bucket.
IE11 = "Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko"
NAVIGATE = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}

# Ordinary browsers. CUBOT is the live over-matching hazard for any `bot`
# substring rule: it is a phone brand, the model string is upper-case, and the
# word boundary after "BOT" is a plain space. (An earlier draft pinned
# "CUBOT_NOTE_40", which cannot collide at all — `_` is a word character, so
# `bot\b` never matches it. That test could not fail; this one can.)
REAL_BROWSERS = {
    "chrome desktop": CHROME,
    "firefox desktop": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0"
    ),
    "safari ios 17": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
    "edge desktop": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
    ),
    "pre-sec-fetch browser": IE11,
    "cubot phone, space-delimited BOT": (
        "Mozilla/5.0 (Linux; Android 13; CUBOT MAX 5) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36"
    ),
    "cubot phone, underscored model": (
        "Mozilla/5.0 (Linux; Android 13; CUBOT_NOTE_40) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36"
    ),
}

# In-app browsers: a REAL PERSON tapping the link inside a messaging app, with
# the vendor's name injected into an otherwise ordinary UA. This is where a bare
# `linkedin` / `slack` / `discord` / `telegram` / `whatsapp` token silently
# deletes a commercial client's view — the defect that got attempt 1 rejected.
IN_APP_BROWSERS = {
    "linkedin ios": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 [LinkedInApp]/9.29.2"
    ),
    "linkedin android": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36 com.linkedin.android/4.1.900"
    ),
    "facebook ios": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 "
        "[FBAN/FBIOS;FBAV/470.0.0.29.108;FBBV/604513264;FBDV/iPhone15,2]"
    ),
    "instagram android": (
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36 Instagram 335.0.0.34.95 Android"
    ),
    "slack ios": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 Slack/24.06.10 iOS/17.5"
    ),
    "teams desktop": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Electron/28.1.0 Safari/537.36 Teams/24060.2623.2790.8046"
    ),
    "whatsapp in-app browser": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 WhatsApp/2.24.15.78"
    ),
    "discord ios": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 Discord/220.0"
    ),
    "telegram android": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36 Telegram-Android/10.14.5"
    ),
    "gmail ios viewer": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 Gmail/6.0.240609"
    ),
}

# The unfurlers and scanners the vendor tokens above were supposed to catch —
# every one of them still caught, without a token that can hit a human. Each is
# tested WITH a perfect navigate/document pair so only the UA branch can fire.
MACHINE_AGENTS = {
    "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "linkedin unfurler": (
        "LinkedInBot/1.0 (compatible; Mozilla/5.0; Apache-HttpClient +http://www.linkedin.com)"
    ),
    "slack unfurler": "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "discord unfurler": "Discordbot/2.0 (+https://discordapp.com)",
    "telegram unfurler": "TelegramBot (like TwitterBot)",
    "facebook unfurler": (
        "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
    ),
    "whatsapp unfurler": "WhatsApp/2.24.15.78 A",
    "skype/teams unfurler": "SkypeUriPreview Preview/0.5 skype-url-preview@microsoft.com",
    "mail scanner": "Mozilla/5.0 (compatible; Barracuda Scanner)",
    "mimecast scanner": "Mozilla/5.0 (compatible; mimecast-scanner/1.0)",
    "headless chromium": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "HeadlessChrome/126.0.0.0 Safari/537.36"
    ),
    "scripted python": "python-requests/2.32.3",
    "curl": "curl/8.5.0",
    "go client": "Go-http-client/2.0",
}


def _req(method: str = "GET", **headers) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/p/x",
            "query_string": b"",
            "client": ("203.0.113.9", 41234),
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
    )


# ── one test per suppression branch, each signal sent ALONE ─────────────────
#
# Mutation-checked: delete any one branch in app/public/telemetry.py and exactly
# the matching test below fails. See the package summary for the recorded runs.


@pytest.mark.unit
def test_head_is_never_a_view_even_from_a_perfect_browser():
    """Branch 1 (method). What any future HEAD support has to rest on.

    An uptime checker sends HEAD with a plausible browser UA and no Sec-Fetch
    header at all — every other branch waves it through, so only the method test
    can stop it flipping an invoice to "viewed".
    """
    assert telemetry.is_human_view(_req("HEAD", **{"user-agent": CHROME})) is False
    assert telemetry.is_human_view(_req("HEAD", **{"user-agent": CHROME, **NAVIGATE})) is False
    # Same request as a GET is a view — the method is doing the work, not the UA.
    assert telemetry.is_human_view(_req("GET", **{"user-agent": CHROME})) is True


@pytest.mark.unit
@pytest.mark.parametrize(("case", "ua"), sorted(MACHINE_AGENTS.items()))
def test_machine_user_agents_are_caught_with_no_help_from_sec_fetch(case, ua):
    """Branch 2 (User-Agent), isolated: headers say navigate/document throughout.

    This is the branch that matters against the real threat — mail scanners send
    no Sec-Fetch-* at all, so nothing else in the module can see them.
    """
    assert telemetry.is_human_view(_req(**{"user-agent": ua, **NAVIGATE})) is False, case


@pytest.mark.unit
@pytest.mark.parametrize("mode", ("cors", "no-cors", "same-origin", "websocket"))
def test_sec_fetch_mode_alone_suppresses(mode):
    """Branch 3, isolated: a real browser UA and NO Sec-Fetch-Dest header.

    Attempt 1's negatives always sent a non-document dest alongside the mode, so
    branch 4 covered for branch 3 and branch 3 could be deleted with the suite
    still green. Nothing here but the mode says "not a navigation".
    """
    assert telemetry.is_human_view(_req(**{"user-agent": CHROME, "sec-fetch-mode": mode})) is False


@pytest.mark.unit
@pytest.mark.parametrize("dest", ("empty", "iframe", "image", "script"))
def test_sec_fetch_dest_alone_suppresses(dest):
    """Branch 4, isolated: a real browser UA and NO Sec-Fetch-Mode header."""
    assert telemetry.is_human_view(_req(**{"user-agent": CHROME, "sec-fetch-dest": dest})) is False


@pytest.mark.unit
def test_an_iframe_embed_is_caught_by_dest_even_though_mode_says_navigate():
    """Branch 4 again, with mode present and innocent — mode cannot cover for it."""
    headers = {"user-agent": CHROME, "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}
    assert telemetry.is_human_view(_req(**headers)) is False


# ── the recording side: everything that must still count ────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(("case", "ua"), sorted(REAL_BROWSERS.items()))
def test_ordinary_browsers_record_a_view(case, ua):
    assert telemetry.is_human_view(_req(**{"user-agent": ua, **NAVIGATE})) is True, case
    # …and again with no Sec-Fetch headers, which is what Safari < 16.4 sends.
    assert telemetry.is_human_view(_req(**{"user-agent": ua})) is True, case


@pytest.mark.unit
@pytest.mark.parametrize(("case", "ua"), sorted(IN_APP_BROWSERS.items()))
def test_in_app_browsers_are_people_and_record_a_view(case, ua):
    """The audit, as a test: no token may match a UA a real browser sends.

    A client reading a proposal inside LinkedIn or Slack messaging is a normal
    channel for this business. Suppressing them leaves the document reading
    "sent" with nothing to show a person ever looked — the error nobody can
    notice. Each string here carries the vendor name that a naive token list
    would trip on.
    """
    assert telemetry.is_human_view(_req(**{"user-agent": ua, **NAVIGATE})) is True, case


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "headers"),
    (
        ("navigate mode alone", {"user-agent": CHROME, "sec-fetch-mode": "navigate"}),
        ("document dest alone", {"user-agent": CHROME, "sec-fetch-dest": "document"}),
        ("no sec-fetch headers", {"user-agent": IE11}),
        # Resolved inconsistency: absence is never evidence, for Sec-Fetch-* AND
        # for User-Agent. Corporate proxies blank the UA; treating that as proof
        # of a machine was the same inference answered two different ways.
        ("no user-agent at all", dict(NAVIGATE)),
        ("blank user-agent", {"user-agent": "   ", **NAVIGATE}),
        # A prefetch is reused when the user clicks, with no second request —
        # dropping it would drop the real open that follows.
        (
            "prefetch",
            {"user-agent": CHROME, "sec-purpose": "prefetch", **NAVIGATE},
        ),
    ),
)
def test_absent_or_innocent_signals_still_record(case, headers):
    assert telemetry.is_human_view(_req(**headers)) is True, case


# ── the three document routes, end to end ───────────────────────────────────

_counter = itertools.count()
_DOC_TABLES = ("proposals", "contracts", "invoices")
SCANNER = {
    "user-agent": "Mozilla/5.0 (compatible; Barracuda Scanner)",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-dest": "empty",
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _seed(kind: str):
    """One sent document of `kind`, plus the client/project it hangs off."""
    tag = f"{kind}{next(_counter)}"
    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)", (f"Scan {tag}", f"{tag}@s.test"))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, f"Scan {tag}"))
    slug = f"scan-{tag}"
    if kind == "proposal":
        table, url = "proposals", f"/p/{slug}"
        did = db.run(
            "INSERT INTO proposals (project_id, slug, title, line_items, status) "
            "VALUES (?,?,?,?,'sent')",
            (pid, slug, "Tasting menu", "[]"),
        )
    elif kind == "contract":
        table, url = "contracts", f"/c/{slug}"
        did = db.run(
            "INSERT INTO contracts (project_id, slug, title, body, body_sha256, status) "
            "VALUES (?,?,?,?,?,'sent')",
            (pid, slug, "Services Agreement", "BODY", "a" * 64),
        )
    else:
        table, url = "invoices", f"/i/{slug}"
        did = db.run(
            "INSERT INTO invoices (project_id, slug, title, total_cents, status) "
            "VALUES (?,?,?,?,'sent')",
            (pid, slug, "Shoot", 90000),
        )
    return table, url, did, pid, cid


def _cleanup(table, did, pid, cid):
    db.run(f"DELETE FROM {db.ident(table, _DOC_TABLES)} WHERE id=?", (did,))
    db.run("DELETE FROM projects WHERE id=?", (pid,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))


def _status(table, did):
    return db.one(
        f"SELECT status, viewed_at FROM {db.ident(table, _DOC_TABLES)} WHERE id=?", (did,)
    )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ("proposal", "contract", "invoice"))
def test_client_navigation_records_the_view(client, kind):
    table, url, did, pid, cid = _seed(kind)
    try:
        assert client.get(url, headers={"user-agent": CHROME, **NAVIGATE}).status_code == 200
        row = _status(table, did)
        assert row["status"] == "viewed" and row["viewed_at"]
    finally:
        _cleanup(table, did, pid, cid)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ("proposal", "contract", "invoice"))
def test_scanner_fetch_serves_the_page_without_recording_a_view(client, kind):
    table, url, did, pid, cid = _seed(kind)
    try:
        r = client.get(url, headers=SCANNER)
        # The document must still serve — suppression is telemetry, not a block.
        assert r.status_code == 200
        row = _status(table, did)
        assert row["status"] == "sent" and row["viewed_at"] is None
    finally:
        _cleanup(table, did, pid, cid)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ("proposal", "contract", "invoice"))
def test_browser_without_sec_fetch_headers_still_records_the_view(client, kind):
    """The no-signal case, pinned: missing Sec-Fetch-* is not evidence of a bot.

    Safari shipped those headers in 16.4 (March 2023), so an iPhone on an older
    OS lands here. Dropping the whole bucket would lose real views with nothing
    to show it happened.
    """
    table, url, did, pid, cid = _seed(kind)
    try:
        assert client.get(url, headers={"user-agent": IE11}).status_code == 200
        assert _status(table, did)["status"] == "viewed"
    finally:
        _cleanup(table, did, pid, cid)


@pytest.mark.integration
def test_suppressed_view_leaves_the_document_open_to_action(client):
    """A scanner touching the URL must not change what the client can still do."""
    table, url, did, pid, cid = _seed("proposal")
    try:
        client.get(url, headers=SCANNER)
        assert _status(table, did)["status"] == "sent"
        r = client.post(f"{url}/accept", follow_redirects=False)
        assert r.status_code == 303
        assert _status(table, did)["status"] == "accepted"
    finally:
        _cleanup(table, did, pid, cid)
