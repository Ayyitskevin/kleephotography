"""Central feature flags.

All "enabled" checks mean the feature is fully armed (keys present).
Empty/missing config values keep the feature dormant (graceful 503s or hidden UI).

Existing per-module is_enabled/configured() are kept for now but callers
should prefer features.* for consistency. Modules can delegate to here.
"""

from . import config


def stripe_enabled() -> bool:
    return bool(config.STRIPE_SECRET_KEY)


def stripe_webhook_enabled() -> bool:
    return bool(config.STRIPE_WEBHOOK_SECRET)


def odysseus_caption_enabled() -> bool:
    return bool(config.ODYSSEUS_CAPTION_URL and config.ODYSSEUS_CAPTION_TOKEN)


def gmail_enabled() -> bool:
    return bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD)


def telegram_enabled() -> bool:
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID)


def sms_enabled() -> bool:
    """Quo / OpenPhone SMS."""
    return bool(config.QUO_API_KEY and config.QUO_NUMBER)


def google_calendar_enabled() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def reopen_notify_enabled() -> bool:
    return bool(config.REOPEN_NOTIFY_URL and config.REOPEN_NOTIFY_TOKEN)


def hermes_enabled() -> bool:
    return bool(config.HERMES_ARM_URL)


def shots_api_enabled() -> bool:
    return bool(config.SHOTS_TOKEN)


def notion_enabled() -> bool:
    return bool(config.NOTION_TOKEN)


def notion_bookings_enabled() -> bool:
    return bool(config.NOTION_TOKEN and config.NOTION_BOOKINGS_DB)


def notion_sessions_enabled() -> bool:
    return bool(config.NOTION_TOKEN and config.NOTION_SESSIONS_DB)


def plausible_enabled() -> bool:
    return bool(config.PLAUSIBLE_DOMAIN)


def demo_gallery_enabled() -> bool:
    return bool(config.DEMO_GALLERY_SLUG and config.DEMO_GALLERY_PIN)


def screening_room() -> bool:
    """Screening Room redesign — whether converted templates emit body.sr."""
    return bool(config.SCREENING_ROOM)


def aerials_live() -> bool:
    """Aerial (drone) deliverables — flips on when the Part 107 cert lands."""
    return bool(config.AERIALS_LIVE)
