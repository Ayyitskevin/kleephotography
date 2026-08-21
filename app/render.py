import datetime as dt
import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from . import config, db, features, specialties
from .public.site_catalog import marketing_meta

ROOT = Path(__file__).resolve().parent.parent
_STATIC_REV_TTL_SECONDS = 5.0
_static_rev_cache = {"expires": 0.0, "value": 0}


def _static_rev() -> int:
    """Newest mtime among top-level /static files — the cache-buster templates
    append to their versioned static asset URLs. Cached briefly because CSS/JS
    deploys land via `git pull` without a service restart; changed files are
    detected within the five-second TTL while ordinary template renders avoid
    repeated directory scans."""
    now = time.monotonic()
    if now < _static_rev_cache["expires"]:
        return _static_rev_cache["value"]
    value = int(
        max((f.stat().st_mtime for f in (ROOT / "static").glob("*") if f.is_file()), default=0)
    )
    _static_rev_cache.update(value=value, expires=now + _STATIC_REV_TTL_SECONDS)
    return value


templates = Jinja2Templates(
    directory=ROOT / "templates",
    # csp_nonce: set per-request by the common_headers middleware (app/main.py);
    # inline <script nonce="{{ csp_nonce }}"> blocks must echo it to satisfy the
    # nonce'd script-src. Empty fallback keeps non-HTTP render paths working.
    context_processors=[
        lambda request: {
            "static_rev": _static_rev(),
            "csp_nonce": getattr(request.state, "csp_nonce", ""),
        }
    ],
)
templates.env.globals["site_name"] = config.SITE_NAME
templates.env.globals["base_url"] = config.BASE_URL
# Startup fallback for any render path that doesn't run context processors
# (the per-render value above overrides this for normal TemplateResponses).
templates.env.globals["static_rev"] = _static_rev()


# These two run per render of the marketing base, and a plan once existed to
# TTL-cache them (brief item B6) because each opened its own connection —
# ~0.9 ms apiece, so ~1.8 ms a page. Thread-local read connections removed that:
# measured together they now cost 0.005 ms per render.
#
# So do not cache them. A TTL would buy five microseconds and cost a staleness
# window on exactly the writes an operator checks immediately — a newly starred
# portfolio photo, a press hit cleared for the footer — which is why the first
# attempt broke three smoke tests. Making it safe needs invalidation wired into
# the admin write paths: a cross-cutting change, now for no measurable gain.


def _og_image_id() -> int | None:
    """Newest starred ready photo — stronger share cards than the oldest id."""
    row = db.one("""SELECT id FROM assets WHERE portfolio=1 AND status='ready'
                    AND kind='photo' ORDER BY id DESC LIMIT 1""")
    return row["id"] if row else None


def _has_press_features() -> bool:
    """True when at least one press hit is cleared for the public footer/nav."""
    return (
        db.one(
            """SELECT 1 AS x FROM press
               WHERE deleted_at IS NULL AND show_on_site=1
                 AND publish_date IS NOT NULL
                 AND publish_date <= date('now','localtime')
               LIMIT 1"""
        )
        is not None
    )


templates.env.globals["og_image_id"] = _og_image_id
templates.env.globals["has_press_features"] = _has_press_features
templates.env.globals["instagram_url"] = config.INSTAGRAM_URL
templates.env.globals["google_business_url"] = config.GOOGLE_BUSINESS_URL
templates.env.globals["contact_email"] = config.CONTACT_EMAIL
templates.env.globals["plausible_domain"] = config.PLAUSIBLE_DOMAIN
# Callables (not frozen bools) so the flags read current config per render —
# tests monkeypatch app.config.SCREENING_ROOM / AERIALS_LIVE and see it stick.
templates.env.globals["sr_enabled"] = features.screening_room
templates.env.globals["aerials_live"] = features.aerials_live
# specialty key → film-stock code ('re' → '250D') for mono frame metadata
templates.env.globals["sp_stock"] = {k: m["stock"] for k, m in specialties.SPECIALTIES.items()}
templates.env.globals["aerial_pass_display"] = specialties.aerial_pass_display
templates.env.globals["marketing_meta"] = marketing_meta


ALT_MAX_CHARS = 160


def _asset_field(asset, key: str) -> str:
    """One column off a dict or a sqlite3.Row (which lacks .get), or ""."""
    try:
        keys = asset.keys() if hasattr(asset, "keys") else ()
        if key in keys:
            return (asset[key] or "").strip()
    except (TypeError, KeyError, IndexError):
        pass
    return ""


def _described_alt(asset) -> str:
    """The Argus per-frame description, normalised — or "" when there is none.

    Whitespace is collapsed and the result capped at ALT_MAX_CHARS: assistive
    tech announces alt as one unbroken run, and Argus is a remote service whose
    output length is not ours to trust.
    """
    described = _asset_field(asset, "argus_alt_text")
    if not described:
        return ""
    described = " ".join(described.split())
    if len(described) > ALT_MAX_CHARS:
        described = described[: ALT_MAX_CHARS - 1].rstrip(" ,;:.—-") + "…"
    return described


def _frame_alt(asset, fallback: str) -> str:
    """Alt text for one client-gallery frame: the description, else `fallback`.

    The client gallery's fallback is positional ("Title — frame 0042"), which
    is why it cannot share _portfolio_alt's craft-phrase fallback.
    """
    return _described_alt(asset) or fallback


def _portfolio_alt(asset, site_name: str | None = None) -> str:
    """Accessible alt text: the per-frame description when Argus wrote one,
    otherwise the portfolio_tag craft phrase.

    Argus already writes a real description of each analyzed frame to
    `assets.argus_alt_text` (app/argus_writeback.py), and until now the only
    thing that read it was an admin hover overlay — so every food photo on the
    public site shipped the identical string. A tag-derived phrase describes a
    *category*, not a picture; it is the fallback, not the answer.

    The studio name is deliberately NOT appended to a real description: it is
    already in the page's LocalBusiness JSON-LD and the <title>, and repeating
    it on every frame is alt-text keyword stuffing that a screen-reader user
    hears once per image. The tag fallback keeps it, because a bare craft
    phrase is generic enough to need the attribution.
    """
    name = site_name or config.SITE_NAME
    if described := _described_alt(asset):
        return described
    tag = _asset_field(asset, "portfolio_tag")
    if tag:
        key, label = specialties.split_tag(tag)
        craft = specialties.SPECIALTIES[key]["craft"]
        if label:
            return f"{label.capitalize()} — {craft} by {name}"
        return f"{craft[0].upper()}{craft[1:]} by {name}"
    return f"Food & beverage photography by {name}"


templates.env.filters["portfolio_alt"] = _portfolio_alt
templates.env.filters["frame_alt"] = _frame_alt


def _tag_label(tag: str | None) -> str:
    """'re/exteriors' → 'Exteriors' — chip/caption display for portfolio tags
    (specialty prefix stripped, capitalized like the old |capitalize chips)."""
    return specialties.tag_label(tag).capitalize()


templates.env.filters["tag_label"] = _tag_label


def _reel_title(reel, site_name: str | None = None) -> str:
    """Display name for one portfolio reel.

    Shared with the sitemap: /reels emits a VideoObject and sitemap.xml emits a
    <video:video> for the same asset, and Google cross-checks the two. A forked
    formula would make them disagree the first time either is edited.
    """
    name = site_name or config.SITE_NAME
    tag = _asset_field(reel, "portfolio_tag")
    return f"{_tag_label(tag) if tag else 'Reel'} — {name}"


def _reel_description(reel, site_name: str | None = None) -> str:
    """Description for one portfolio reel — see _reel_title on why this is shared."""
    name = site_name or config.SITE_NAME
    tag = _asset_field(reel, "portfolio_tag")
    kind = _tag_label(tag).lower() if tag else "social"
    return f"Short-form {kind} video by {name}, Asheville NC."


templates.env.filters["reel_title"] = _reel_title
templates.env.filters["reel_description"] = _reel_description
# 're/exteriors' → 're'; unprefixed → 'fb' (legacy F&B). Drives the data-sp
# specialty-filter attribute on portfolio/reels tiles.
templates.env.filters["tag_specialty"] = specialties.specialty_key


def _diff_tokens(value):
    """Presentation-only: flatten an audit-diff value into display tokens.
    JSON-array strings (how territory/channels are stored) become their elements;
    real lists pass through; scalars become a single token. Read-side cosmetics
    only — never re-encodes or mutates the stored diff."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [str(value)]


templates.env.filters["diff_tokens"] = _diff_tokens


def _localtime(utc_str, fmt="%a %b %-d · %-I:%M %p", tz=None):
    """Format a stored 'YYYY-MM-DD HH:MM:SS' UTC instant in a display timezone.
    Defaults to the business timezone (admin/manage views read in Kevin's local
    time); pass tz=<IANA name> to render in the visitor's zone — e.g. a booking
    confirmation shown in the same zone the client picked the slot in. An
    invalid/empty tz falls back to the business timezone."""
    if not utc_str:
        return ""
    try:
        zone = ZoneInfo(tz) if tz else ZoneInfo(config.TIMEZONE)
    except Exception:
        zone = ZoneInfo(config.TIMEZONE)
    try:
        d = (
            dt.datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=dt.UTC)
            .astimezone(zone)
        )
    except (ValueError, TypeError):
        return utc_str
    return d.strftime(fmt)


templates.env.filters["localtime"] = _localtime


def _hms(seconds) -> str:
    """94.6 → '1:35' (or 'h:mm:ss') — duration badge for delivered video tiles.
    Empty string for missing/zero durations so the badge simply doesn't render."""
    try:
        s = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    m, sec = divmod(s, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


templates.env.filters["hms"] = _hms


def _usd(cents) -> str:
    """Cents → '1234.56' for client documents (invoices/proposals/receipts).
    No leading '$' — templates own the currency glyph so '-$' etc. stay literal."""
    return f"{(cents or 0) / 100:.2f}"


def _usd0(cents) -> str:
    """Cents → '1,234' (whole dollars) for the studio dashboard's glanceable KPIs."""
    return f"{(cents or 0) / 100:,.0f}"


templates.env.filters["usd"] = _usd
templates.env.filters["usd0"] = _usd0


def _field(row, key: str) -> str:
    """Read a column from a sqlite3.Row OR a plain dict, tolerating absent keys.
    The gallery delivery-email form passes a synthetic dict that only carries
    client_name/client_email, so company/title legitimately don't exist there —
    they must resolve to "" rather than blowing up the page (sqlite3.Row raises
    IndexError on a missing column; dict raises KeyError)."""
    if not row:
        return ""
    try:
        return row[key] or ""
    except (KeyError, IndexError):
        return ""


def _merge_ctx(d, p, doc_url: str) -> dict:
    """Merge-field values for an email template, resolved against a doc + project."""
    client_name = _field(p, "client_name")
    return {
        "first_name": client_name.split(" ")[0] if client_name else "there",
        "client_name": client_name,
        "company": _field(p, "company"),
        "project_title": _field(p, "title"),
        "doc_title": _field(d, "title"),
        "doc_url": doc_url or "",
        "site_name": config.SITE_NAME,
    }


def _apply_merge(text: str, ctx: dict) -> str:
    """Resolve {field} placeholders. Unknown braces pass through untouched."""
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def email_template_options(d, p, doc_url: str) -> list[dict]:
    """Active email templates with merge fields resolved for this doc — feeds the
    one-click picker on the send form. Read-only; sends stay manual."""
    rows = db.all_(
        "SELECT name, subject, body FROM email_templates WHERE deleted_at IS NULL ORDER BY name"
    )
    ctx = _merge_ctx(d, p, doc_url)
    return [
        {
            "name": r["name"],
            "subject": _apply_merge(r["subject"], ctx),
            "body": _apply_merge(r["body"], ctx),
        }
        for r in rows
    ]


templates.env.globals["email_template_options"] = email_template_options
