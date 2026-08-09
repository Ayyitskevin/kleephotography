"""Does a client-document request mean a person actually opened the page?

Proposals, contracts and invoices flip `sent` → `viewed` on a plain GET of
`/p/{slug}`, `/c/{slug}` and `/i/{slug}`. That is a funnel signal Kevin acts on
("they've read it — follow up"), but corporate mail scanners and link-preview
bots fetch every URL in an email the moment it lands, so the flip can just as
easily be a robot. A fabricated "viewed" costs a real decision on a contract or
a money document.

Why the method test is here even though nothing sends HEAD yet
--------------------------------------------------------------
These routes answer 405 to a HEAD today, so signal 1 cannot fire in production.
It is kept because it is a prerequisite for ever changing that. A companion
change widened every GET route to also answer HEAD; on a build carrying that
and not this guard, `HEAD /i/{slug}` from an uptime checker moved an invoice
from `sent` to `viewed` — a monitoring ping writing to the money path. The
widening was dropped for a larger reason (it reached 117 other GET routes that
were never audited for write-on-read, several of which insert rows), but when
HEAD is revisited it needs this test already in place, per route that writes.

What counts as evidence
-----------------------
A view is suppressed only on POSITIVE evidence that the requester is not a
person navigating to the page. Four signals, in the order they are checked:

1. **The method is not GET.** HEAD asks for headers and the body is discarded
   by definition, so nobody read anything.
2. **The User-Agent names a machine** — a crawler, a preview unfurler, a mail
   scanner or a scripted HTTP client.
3. **Sec-Fetch-Mode is present and is not `navigate`** — an XHR/fetch or a
   `no-cors` probe, not someone clicking their way to the page.
4. **Sec-Fetch-Dest is present and is not `document`** — a subresource or an
   iframe, not a top-level page.

Signals 3–4 and signal 2 cover different populations and neither subsumes the
other, but against the *actual* threat here the User-Agent list carries nearly
all the weight: scripted mail scanners (Proofpoint, Mimecast, Barracuda …) are
not browsers and send no Sec-Fetch-* header at all, so 3 and 4 never fire on
them. What 3 and 4 buy is the other case — a genuine browser engine fetching
the URL as an XHR, a subresource or an iframe, where the User-Agent is real and
tells us nothing.

Absence is never evidence — the same rule for every header
----------------------------------------------------------
A missing header is never read as suspicion, Sec-Fetch-* and User-Agent alike:
there is no way to tell "stripped in transit" from "the client is a robot".

For Sec-Fetch-* the no-signal bucket is much larger than it first looks.
Chrome has sent those headers since 76 (2019) and Firefox since 90 (2021), but
**Safari only shipped them in 16.4, March 2023** — so every iPhone, iPad or Mac
client on an older OS arrives with none of them, and "keeps a working phone for
years" describes a lot of real clients. Had absence been read as suspicion,
this app would have silently dropped a large slice of genuine Apple views. The
late Safari date strengthens the choice of default rather than weakening it.

The same reasoning has to apply to a missing User-Agent, and an earlier draft
of this module got that wrong: it treated an absent UA as proof of a machine
while arguing that an absent Sec-Fetch-* proved nothing — one inference with
two answers. Corporate proxies and privacy tooling blank the UA as well.
Resolved in favour of consistency: no User-Agent is no signal, and the request
records a view. The cost is small in both directions, because practically every
real fetcher self-identifies (that is what `_MACHINE_UA` is for) and
practically every real browser sends a UA, so the bucket is nearly empty.

Why the default leans towards recording
---------------------------------------
The two errors are not symmetrical in whether a human can ever catch them:

* A bot-caused false "viewed" writes a `viewed_at` seconds after `sent_at` —
  visible, dated, and sanity-checkable by the person acting on it.
* A dropped real view leaves the document reading `sent`, indistinguishable
  from "nobody opened it". Nobody can notice it, and the client on an old iPad
  would never register a view at all.

So a view is discarded only when there is a reason that can be named, and the
reason is logged.

Deliberately NOT suppressed: `Sec-Purpose: prefetch`/`prerender`. A browser
reuses the prefetched response when the user then clicks, without issuing a
second request, so treating a prefetch as a machine hit would silently drop the
real open that follows it.
"""

import logging
import re

from fastapi import Request

from .. import security

log = logging.getLogger("mise.public.telemetry")

# "bot" is the most useful token in this file and the most dangerous one. It
# catches every crawler and unfurler without naming any of them — Googlebot/2.1,
# bingbot/2.0, LinkedInBot/1.0, Slackbot-LinkExpanding, Discordbot/2.0,
# TelegramBot, Twitterbot/1.0, AhrefsBot/7.0 — and it also sits inside real
# Android model names:
#
#   Mozilla/5.0 (Linux; Android 13; CUBOT MAX 5) … Chrome/126 Mobile Safari/537.36
#
# which is a phone a client may genuinely be holding. Case is the discriminator:
# crawlers spell it "bot" or "Bot", Android model strings shout "BOT". Hence a
# case-SENSITIVE pattern, deliberately kept out of the IGNORECASE list below.
# Residual risk, accepted: an all-caps crawler ("…BOT/1.0") records a view —
# that is the error class a human can still see (see the module docstring).
_BOT_UA = re.compile(r"[Bb]ot\b")

# Over-matching is the failure this list can never show you: the client opens
# the document, nothing records, and it reads "sent" forever. So every token has
# to survive the question "does any REAL browser send this string?" — and in-app
# browsers inject their vendor's name into an otherwise ordinary UA:
#
#   Mozilla/5.0 (iPhone; …) Mobile/15E148 [LinkedInApp]/9.29.2
#   Mozilla/5.0 (Linux; Android 14; …) Mobile Safari/537.36 com.linkedin.android/4.1.900
#   Mozilla/5.0 (iPhone; …) Mobile/15E148 [FBAN/FBIOS;FBAV/470.0.0.29.108]
#   Mozilla/5.0 (iPhone; …) Mobile/15E148 Slack/24.06.10 iOS/17.5
#
# A bare vendor token therefore suppresses a real commercial client tapping a
# proposal link inside app messaging — a normal channel for this business. So
# `linkedin`, `slack`, `discord` and `telegram` are NOT here: each one silenced
# a human, and each bought nothing, because the actual unfurlers (LinkedInBot,
# Slackbot-LinkExpanding, Discordbot, TelegramBot) are already caught by
# _BOT_UA. WhatsApp's fetcher is anchored — its whole UA is `WhatsApp/2.24.15.78
# A`, while any in-app browser starts with `Mozilla/5.0`. Everything that
# remains is a vendor or product name no browser emits. tests/
# test_doc_view_telemetry.py pins that claim against real in-app-browser UAs.
_MACHINE_UA = re.compile(
    r"""
      crawler | spider | scrapy | slurp | archiver               # generic crawlers
    | facebookexternalhit | skypeuripreview | bingpreview        # link-preview unfurlers
    | embedly | ^whatsapp/
    | proofpoint | mimecast | barracuda | messagelabs            # mail-security scanners
    | forcepoint | zscaler | safelinks | urldefense | symantec | trendmicro
    | headless | phantomjs | puppeteer | playwright              # scripted browsers
    | curl/ | wget | python-(requests|httpx|urllib) | okhttp     # scripted HTTP clients
    | go-http-client | java/ | libwww | axios | node-fetch | httpie
    | apache-httpclient | guzzle | powershell
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _machine_signal(request: Request) -> str:
    """Name the evidence that this is not a person opening the page, or '' if none."""
    if request.method != "GET":
        # A metadata probe is never a read. Nothing sends HEAD here today (these
        # routes 405 it), but this is the line that would keep a future HEAD
        # widening off the money and contract state machines — see the module
        # docstring.
        return f"method={request.method}"
    ua = request.headers.get("user-agent", "")
    if _BOT_UA.search(ua) or _MACHINE_UA.search(ua):
        return f"machine user-agent {ua[:120]!r}"
    # Each Sec-Fetch header is judged only when present, so a client that sends
    # one but not the other is not penalised for the missing half.
    mode = request.headers.get("sec-fetch-mode")
    if mode and mode != "navigate":
        return f"sec-fetch-mode={mode}"
    dest = request.headers.get("sec-fetch-dest")
    if dest and dest != "document":
        return f"sec-fetch-dest={dest}"
    return ""


def is_human_view(request: Request) -> bool:
    """True when this request should count as the client opening the document.

    Presentation/telemetry only — callers gate the `sent` → `viewed` flip on it
    and nothing else. The page renders either way.
    """
    signal = _machine_signal(request)
    if not signal:
        return True
    log.info("view not recorded (%s) from %s", signal, security.client_ip(request))
    return False
