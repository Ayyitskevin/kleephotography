"""Settings — a read-only status surface over Mise's REAL configuration.

Honest adaptation of the Admin Settings prototype. The mock has clickable
Connect buttons and on/off automation toggles, but Mise's integrations are
provisioned by env vars in /opt/mise/.env (managed on the server, mode 600) and
its "automations" are fixed operational policy, not UI-flippable switches. So
this reports what is actually wired — presence only, NEVER a secret value — in
the prototype's two-section layout: an integrations grid (configured / not set,
with Google Calendar linking to its real OAuth connect flow) and a read-only
"How the studio runs" panel of the genuine operational settings. Nothing here
writes; the .env is the single source of truth.
"""

import datetime as dt
import shutil

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import caption_ai, config, gcal, security, sms
from ..render import templates

router = APIRouter(prefix="/admin/settings",
                   dependencies=[Depends(security.require_admin)])


def _badge(connected: bool) -> dict:
    if connected:
        return {"status": "connected", "label": "Connected"}
    return {"status": "off", "label": "Not set"}


def _integrations() -> list[dict]:
    g = gcal.status()
    if g["connected"]:
        google = {"status": "connected", "label": "Connected"}
    elif g["configured"]:
        google = {"status": "ready", "label": "Connect", "href": "/admin/scheduling"}
    else:
        google = {"status": "off", "label": "Not set"}

    notion_on = bool(config.NOTION_TOKEN)
    notion_desc = "Mirror booking & session status to your workspace (one-way)"
    if notion_on:
        armed = [n for n, v in (("Sessions", config.NOTION_SESSIONS_DB),
                                ("Bookings", config.NOTION_BOOKINGS_DB)) if v]
        notion_desc = ("One-way mirror · " + (", ".join(armed) + " armed"
                       if armed else "token set, no database armed yet"))

    return [
        {"name": "Stripe", "mark": "S", "icon_bg": "#ece9ff", "icon_color": "#5b54e0",
         "desc": "Invoices, deposits & payouts",
         **_badge(bool(config.STRIPE_SECRET_KEY))},
        {"name": "Gmail", "mark": "M", "icon_bg": "#fae3e0", "icon_color": "#c5372c",
         "desc": "Send proposals & invoices from your address — manual send only",
         **_badge(bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD))},
        {"name": "Quo SMS", "mark": "Q", "icon_bg": "#e3edfb", "icon_color": "#2f6d8a",
         "desc": "Two-way texting in your unified inbox",
         **_badge(sms.configured())},
        {"name": "Notion", "mark": "N", "icon_bg": "#e7ecdd", "icon_color": "#143C2F",
         "desc": notion_desc, **_badge(notion_on)},
        {"name": "Google Calendar", "mark": "GC", "icon_bg": "#e3edfb", "icon_color": "#2f6d8a",
         "desc": "Two-way availability — block busy times, push confirmed shoots",
         **google},
        {"name": "Odysseus AI", "mark": "AI", "icon_bg": "#f3e3e5", "icon_color": "#7C2F38",
         "desc": "Draft captions with your local model — never auto-posts",
         **_badge(caption_ai.is_enabled())},
        {"name": "Telegram alerts", "mark": "TG", "icon_bg": "#e1eef7", "icon_color": "#2f6d8a",
         "desc": "Security anomaly alerts — outbound only",
         **_badge(bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID))},
        {"name": "Shot-list API", "mark": "SL", "icon_bg": "#f7ecd2", "icon_color": "#9a7a2c",
         "desc": "Odysseus preshoot pack reads shot lists — inbound, token-gated",
         **_badge(bool(config.SHOTS_TOKEN))},
        {"name": "Cloudflare Access", "mark": "CF", "icon_bg": "#f7ecd2", "icon_color": "#9a7a2c",
         "desc": "Gates /admin behind SSO — managed at the edge, not in Mise",
         "status": "edge", "label": "Edge layer"},
    ]


def _operations() -> list[dict]:
    rl = config.RATE_LIMITS
    mins = config.RECURRING_TICK_SECONDS // 60
    return [
        {"label": "Retainer drafts",
         "value": f"every {mins} min" if mins else "hourly",
         "desc": "Sweeps for due retainer content and writes DRAFTS only — never auto-sends or charges"},
        {"label": "Gallery PIN lockout",
         "value": f"{config.PIN_MAX_FAILS} tries → {config.PIN_LOCKOUT_MIN} min",
         "desc": "Per-IP brute-force lockout on the 4-digit gallery PIN"},
        {"label": "Gallery expiry reminder",
         "value": f"{config.GALLERY_EXPIRY_REMINDER_DAYS} days before",
         "desc": "Emails the client a download reminder as a gallery nears expiry — once per gallery, client must have an email on file"},
        {"label": "Proofing nudge",
         "value": f"after {config.GALLERY_PROOF_NUDGE_DAYS} days",
         "desc": "Nudges the client to finish picking favorites when a gallery still has unmet proof targets — once per gallery"},
        {"label": "Upload disk floor",
         "value": f"{config.MIN_FREE_GB} GB free",
         "desc": "Refuses new uploads below this — fails loud, never fills the disk"},
        {"label": "Rate limits",
         "value": f"{rl['download'][0]}/{rl['public'][0]}/{rl['admin'][0]} per {config.RATE_LIMIT_WINDOW}s",
         "desc": "Per-IP download / public / admin request ceilings (logged-in admin exempt)"},
        {"label": "Business timezone",
         "value": config.TIMEZONE,
         "desc": "Availability is authored here; booking instants stored UTC, converted per-day (DST-safe)"},
        {"label": "Secure cookies",
         "value": "on" if config.COOKIE_SECURE else "off",
         "desc": "HTTPS-only session cookies — on once served behind the tunnel"},
    ]


def _storage() -> dict:
    """Live disk + backup heartbeat (re-homed here from the galleries dashboard in
    the strict-1:1 rebuild — an operational safety signal, not a gallery stat).
    Silence is not evidence: a missing snapshot reads 'none found', loudly."""
    free_gb = shutil.disk_usage(config.DATA_DIR).free / 1e9
    bdir = config.DATA_DIR / "backups"
    snaps = sorted(bdir.glob("*.db.gz")) if bdir.exists() else []
    if snaps:
        age_h = (dt.datetime.now().timestamp() - snaps[-1].stat().st_mtime) / 3600
        if age_h < 1:
            backup = "under an hour ago"
        elif age_h < 24:
            backup = f"{int(age_h)}h ago"
        else:
            backup = f"{int(age_h // 24)}d ago"
    else:
        backup = "none found"
    return {"free_gb": free_gb, "min_free_gb": config.MIN_FREE_GB,
            "low": free_gb < config.MIN_FREE_GB, "backup": backup}


@router.get("", response_class=HTMLResponse)
async def settings(request: Request):
    return templates.TemplateResponse(request, "admin/settings.html", {
        "integrations": _integrations(),
        "operations": _operations(),
        "storage": _storage(),
    })
