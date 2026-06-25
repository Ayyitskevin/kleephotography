"""Mise configuration — env-driven, .env loaded if present (systemd uses EnvironmentFile)."""

import os
from pathlib import Path

_ENV_FILE = os.environ.get("MISE_ENV_FILE", "/opt/mise/.env")


def _load_env_file(path: str) -> None:
    p = Path(path)
    if not p.is_file():
        return
    try:
        text = p.read_text()
    except PermissionError:
        # Under systemd the .env is root/owner-readable only and already
        # injected via EnvironmentFile — nothing to do here.
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(_ENV_FILE)


def _b(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


HOST = os.environ.get("MISE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MISE_PORT", "8400"))
BASE_URL = os.environ.get("MISE_BASE_URL", f"http://localhost:{PORT}")

DATA_DIR = Path(os.environ.get("MISE_DATA_DIR", "/opt/mise/data"))
DB_PATH = DATA_DIR / "mise.db"
MEDIA_DIR = DATA_DIR / "media"
ZIP_DIR = DATA_DIR / "zips"
TMP_DIR = DATA_DIR / "tmp"
BRAND_DIR = DATA_DIR / "brand"
RECEIPTS_DIR = DATA_DIR / "receipts"  # uploaded expense-receipt scans (photo/PDF)

SECRET_KEY = os.environ.get("MISE_SECRET_KEY", "")  # required in prod
ADMIN_PASSWORD = os.environ.get("MISE_ADMIN_PASSWORD", "")  # required in prod

SITE_NAME = os.environ.get("MISE_SITE_NAME", "Kevin Lee Photography")

# Public marketing (optional — empty = feature off / sensible default)
INSTAGRAM_URL = os.environ.get("MISE_INSTAGRAM_URL") or None
CONTACT_EMAIL = os.environ.get("MISE_GMAIL_USER", "")  # public mailto when set
PLAUSIBLE_DOMAIN = os.environ.get("MISE_PLAUSIBLE_DOMAIN", "")  # e.g. kleephotography.com
# Sample client gallery for prospects (/g/{slug}). Slug auto-detected when unset.
DEMO_GALLERY_SLUG = os.environ.get("MISE_DEMO_GALLERY_SLUG", "")
DEMO_GALLERY_PIN = os.environ.get("MISE_DEMO_GALLERY_PIN", "")  # show on site when set
# Idempotent demo-showcase backfill so a fresh prototype site isn't blank (see
# bootstrap.ensure_public_showcase). Off in the test suite so empty-baseline
# assertions exercise the real empty→populated path instead of seeded rows.
SHOWCASE_SEED = _b("MISE_SHOWCASE_SEED", "true")

# Business-local timezone for the scheduler. Availability is authored in this
# zone; booking instants are stored UTC and converted per-day (DST-safe). Change
# via env without a code edit if Kevin relocates the business.
TIMEZONE = os.environ.get("MISE_TIMEZONE", "America/New_York")

# Studio (Phase 4) — empty means the feature is off; routes degrade gracefully
STRIPE_SECRET_KEY = os.environ.get("MISE_STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("MISE_STRIPE_WEBHOOK_SECRET", "")
GMAIL_USER = os.environ.get("MISE_GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("MISE_GMAIL_APP_PASSWORD", "")
NOTION_TOKEN = os.environ.get("MISE_NOTION_TOKEN", "")
# One-way Notion "Bookings" calendar writeback (WINDOW doctrine). Empty = dormant:
# the scheduler still works end-to-end, it just skips the Notion mirror. Arming
# needs Kevin to create a Bookings database, share it with the Mise integration,
# and drop its id here — same posture as Stripe/Telegram (off until provisioned).
NOTION_BOOKINGS_DB = os.environ.get("MISE_NOTION_BOOKINGS_DB", "")
# One-way booking→Notion Session spine. Empty = dormant: a flagged event type's
# confirmed booking is a no-op until this holds the Sessions database id (Mise
# integration must be shared on it). The per-event-type creates_notion_session
# flag is the second gate. Sessions DB id: see the Notion "Sessions" database.
NOTION_SESSIONS_DB = os.environ.get("MISE_NOTION_SESSIONS_DB", "")

# Google Calendar (Phase B) — OAuth web-app creds for the single business account.
# Empty client id/secret = dormant: the scheduler works without calendar sync and
# the admin shows a "Connect" prompt. The id/secret come from Google Cloud Console
# into .env; the per-install REFRESH TOKEN is obtained via the admin OAuth flow and
# stored in the DB (google_oauth), never here. Redirect URI is derived from BASE_URL
# and must match the one registered on the OAuth client exactly. GOOGLE_CALENDAR_ID
# defaults to the account's primary calendar.
GOOGLE_CLIENT_ID = os.environ.get("MISE_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("MISE_GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "MISE_GOOGLE_REDIRECT_URI", f"{BASE_URL}/admin/scheduling/google/callback"
)
GOOGLE_CALENDAR_ID = os.environ.get("MISE_GOOGLE_CALENDAR_ID", "primary")

# Odysseus caption-drafting endpoint (Domain G slices 6b/6c). BOTH url+token must be
# set to arm the "Draft with AI" button (see caption_ai.is_enabled); either unset =
# drafting off and the button stays cleanly dormant. Odysseus owns model selection;
# Mise only POSTs context + a bearer token and reads back {"caption","model"}.
# Timeout is 210s — deliberately ABOVE Odysseus's ~180s caption budget so the ENDPOINT
# decides failure (returns a clean 502) and this synchronous client never fires first,
# orphaning an in-flight generation on mickey.
ODYSSEUS_CAPTION_URL = os.environ.get("MISE_ODYSSEUS_CAPTION_URL", "")
ODYSSEUS_CAPTION_TOKEN = os.environ.get("MISE_ODYSSEUS_CAPTION_TOKEN", "")
ODYSSEUS_TIMEOUT = int(os.environ.get("MISE_ODYSSEUS_TIMEOUT", "210"))

# Argus vision analyze (Phase 6). BOTH url+token must be set to arm publish hooks and
# the gallery admin "Analyze now" button (see argus_analyze.is_enabled); either unset =
# dormant, no outbound call. Mise POSTs mise_gallery_id to Argus /analyze-folder; Argus
# resolves originals via ARGUS_MISE_MEDIA_ROOT on its side. The same bearer token also
# arms GET /api/galleries for Argus to list published galleries (inbound read surface).
ARGUS_URL = os.environ.get("MISE_ARGUS_URL", "").rstrip("/")
ARGUS_TOKEN = os.environ.get("MISE_ARGUS_TOKEN", "")
ARGUS_TIMEOUT = int(os.environ.get("MISE_ARGUS_TIMEOUT", "30"))
ARGUS_ANALYZE_LIMIT = int(os.environ.get("MISE_ARGUS_ANALYZE_LIMIT", "0"))

# Plutus print upsell (Phase 1). BOTH url+token arm post-Argus recommend hooks.
PLUTUS_URL = os.environ.get("MISE_PLUTUS_URL", "").rstrip("/")
PLUTUS_TOKEN = os.environ.get("MISE_PLUTUS_TOKEN", "")
PLUTUS_TIMEOUT = int(os.environ.get("MISE_PLUTUS_TIMEOUT", "30"))

# studio-notify-on-reopen: best-effort push to Odysseus when a client reply
# auto-reopens a resolved video-comment thread. Both unset -> dormant, no outbound
# call. Timeout is SHORT (5s) — opposite of caption: a slow/down Odysseus must never
# stall the client's comment response, and a notify failure is swallowed, never raised.
REOPEN_NOTIFY_URL = os.environ.get("MISE_REOPEN_NOTIFY_URL", "")
REOPEN_NOTIFY_TOKEN = os.environ.get("MISE_REOPEN_NOTIFY_TOKEN", "")
REOPEN_NOTIFY_TIMEOUT = int(os.environ.get("MISE_REOPEN_NOTIFY_TIMEOUT", "5"))

# Two-way SMS inbox (Quo, formerly OpenPhone). All three empty = feature INERT:
# sms.configured() is false, no outbound texts are sent, the /webhooks/quo route
# returns 503, and the Inbox stays email-only. Arming needs Kevin to provision a Quo
# number + API key into flow's .env. QUO_NUMBER is the E.164 business line Quo sends
# FROM (and the inbound webhook's "to"); QUO_WEBHOOK_SECRET is Quo's signing secret
# used to verify inbound webhook HMACs (sms.verify_webhook). API base is overridable
# in case Quo's host changes post-rebrand. No money/legal state.
QUO_API_KEY = os.environ.get("MISE_QUO_API_KEY", "")
QUO_NUMBER = os.environ.get("MISE_QUO_NUMBER", "")
QUO_WEBHOOK_SECRET = os.environ.get("MISE_QUO_WEBHOOK_SECRET", "")
QUO_API_BASE = os.environ.get("MISE_QUO_API_BASE", "https://api.openphone.com/v1")
QUO_TIMEOUT = int(os.environ.get("MISE_QUO_TIMEOUT", "20"))

# Shot-list read API (Domain F / B-Direct integration). Odysseus's preshoot_pack
# reads Mise's local shot list over GET /api/shots?session=<notion_page_id> with a
# bearer token. Empty = endpoint DISARMED: every request returns 503 (not 401), so the
# route ships dormant and only goes live once Kevin provisions MISE_SHOTS_TOKEN into
# flow's .env. This is the ONLY inbound service-bearer surface in Mise.
SHOTS_TOKEN = os.environ.get("MISE_SHOTS_TOKEN", "")

# Platekit/Dionysus bridge. Empty values keep the admin panel and Argus hook dormant.
# When armed, GET packs for the client admin card; Argus job callbacks also POST
# /api/mise/organizations/{slug}/argus-pack to draft keyword-enriched captions.
PLATEKIT_API_BASE = os.environ.get(
    "MISE_PLATEKIT_API_BASE", os.environ.get("MISE_DIONYSUS_API_BASE", "")
)
PLATEKIT_API_TOKEN = os.environ.get(
    "MISE_PLATEKIT_API_TOKEN", os.environ.get("MISE_DIONYSUS_API_TOKEN", "")
)
PLATEKIT_TIMEOUT = int(
    os.environ.get("MISE_PLATEKIT_TIMEOUT", os.environ.get("MISE_DIONYSUS_TIMEOUT", "10"))
)

WEB_MAX_PX = int(os.environ.get("MISE_WEB_MAX_PX", "2048"))
THUMB_MAX_PX = int(os.environ.get("MISE_THUMB_MAX_PX", "480"))
JPEG_QUALITY = int(os.environ.get("MISE_JPEG_QUALITY", "85"))
VIDEO_MAX_W = int(os.environ.get("MISE_VIDEO_MAX_W", "1920"))
VIDEO_CRF = int(os.environ.get("MISE_VIDEO_CRF", "23"))

JOB_WORKERS = int(os.environ.get("MISE_JOB_WORKERS", "2"))

# Recurring-plan scheduler: how often the in-process thread sweeps for due
# retainer drafts. Generates DRAFTS only (never sends/charges). The sweep is
# idempotent, so the only effect of the interval is how soon after a restart a
# due monthly draft is caught up — an hour is plenty for a monthly event.
RECURRING_TICK_SECONDS = int(os.environ.get("MISE_RECURRING_TICK_SECONDS", "3600"))

# Gallery client reminders — fired off the same recurring sweep (gallery_reminders).
# Both are idempotent per gallery via the reminded_* flags, so the interval only
# governs latency, not duplication. Email only; Kevin isn't re-pinged. Expiry: warn
# the client this many days before expires_at. Proofing: nudge once a gallery with
# unmet proof targets has been published-and-waiting this many days (by created_at).
GALLERY_EXPIRY_REMINDER_DAYS = int(os.environ.get("MISE_GALLERY_EXPIRY_REMINDER_DAYS", "3"))
GALLERY_PROOF_NUDGE_DAYS = int(os.environ.get("MISE_GALLERY_PROOF_NUDGE_DAYS", "5"))

# Internal Telegram nudge: a contract sent this many days ago and still unsigned
# gets one heads-up to Kevin (ops_monitor/contract_reminders). One-shot per
# contract via the nudged_unsigned flag; never a message to the client.
CONTRACT_NUDGE_DAYS = int(os.environ.get("MISE_CONTRACT_NUDGE_DAYS", "3"))

# Operational heartbeat (ops_monitor): a backup older than this many hours is
# flagged. Backups run daily (~02:3x via mise-backup.timer), so 26h gives a small
# grace past the normal 24h gap before it reads as stale/missing.
BACKUP_STALE_HOURS = int(os.environ.get("MISE_BACKUP_STALE_HOURS", "26"))

# Event-driven reminder net (hermes_arm): at a Mise event instant, fire-and-forget
# an "arm a deferred owner reminder" push to Hermes (flow :7020), which owns the
# persistent late-safe precise-time engine. One-way (R-doctrine): Mise never reads
# back, and the whole path is dormant unless MISE_HERMES_ARM_URL is set. Two arms:
# a gallery delivered → +N day "did the review land?" check, and a shoot finished →
# +N day "pull/cull/back-up the cards" ops nudge. Hermes dedups by key, so a job
# retry or a re-scan can't double-arm.
HERMES_ARM_URL = os.environ.get("MISE_HERMES_ARM_URL", "")
HERMES_ARM_TOKEN = os.environ.get("MISE_HERMES_ARM_TOKEN", "")
REVIEW_CHECK_DAYS = int(os.environ.get("MISE_REVIEW_CHECK_DAYS", "7"))
POSTSHOOT_CULL_DAYS = int(os.environ.get("MISE_POSTSHOOT_CULL_DAYS", "1"))

PIN_MAX_FAILS = int(os.environ.get("MISE_PIN_MAX_FAILS", "5"))
PIN_LOCKOUT_MIN = int(os.environ.get("MISE_PIN_LOCKOUT_MIN", "15"))

# Per-IP request rate limits (max requests / window seconds). Deliberately generous
# — real galleries never approach these; the media/thumbnail grid is exempt entirely
# (see ratelimit._bucket_for) and logged-in admins are exempt so deploys/testing
# never trip them. Tune via env without a code change if a bucket proves too tight.
RATE_LIMIT_WINDOW = int(os.environ.get("MISE_RL_WINDOW", "60"))
RATE_LIMITS = {
    "download": (int(os.environ.get("MISE_RL_DOWNLOAD", "30")), RATE_LIMIT_WINDOW),
    "public": (int(os.environ.get("MISE_RL_PUBLIC", "120")), RATE_LIMIT_WINDOW),
    "admin": (int(os.environ.get("MISE_RL_ADMIN", "120")), RATE_LIMIT_WINDOW),
}

# Telegram security alerts (anomaly-only). Both unset -> dormant, no outbound call.
# Sending is direct sendMessage (never getUpdates), so it does not conflict with the
# single fleet polling consumer. A separate "alerts" bot token is fine here.
TELEGRAM_TOKEN = os.environ.get("MISE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("MISE_TELEGRAM_CHAT_ID", "")

# Refuse uploads when free disk drops below this (GB) — fail loud, not full.
MIN_FREE_GB = int(os.environ.get("MISE_MIN_FREE_GB", "10"))

# IRS standard mileage rate (cents per mile) stamped onto NEW trip rows. Frozen
# per-row at creation so prior trips keep the rate they were logged at. 2026 = 70¢/mi.
MILEAGE_RATE_CENTS = int(os.environ.get("MISE_MILEAGE_RATE_CENTS", "70"))

# Revenue snapshot monthly goal (display-only bar on Home). Dollars via env;
# 0 = no goal line — the widget just shows collected vs outstanding.
MONTHLY_GOAL_CENTS = int(os.environ.get("MISE_MONTHLY_GOAL", "0")) * 100

SESSION_MAX_AGE = int(os.environ.get("MISE_SESSION_MAX_AGE", str(60 * 60 * 24 * 90)))

COOKIE_SECURE = _b("MISE_COOKIE_SECURE", "false")  # true once behind the tunnel


def ensure_dirs() -> None:
    for d in (DATA_DIR, MEDIA_DIR, ZIP_DIR, TMP_DIR, BRAND_DIR, RECEIPTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
