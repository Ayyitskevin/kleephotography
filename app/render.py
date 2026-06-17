import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from . import config, db

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.globals["site_name"] = config.SITE_NAME
templates.env.globals["base_url"] = config.BASE_URL
# cache-buster for /static/ URLs — Cloudflare edge-caches them for hours,
# so deploys must change the URL, not wait out the TTL
templates.env.globals["static_rev"] = int(max(
    (f.stat().st_mtime for f in (ROOT / "static").glob("*") if f.is_file()),
    default=0))


def _og_image_id() -> int | None:
    row = db.one("""SELECT id FROM assets WHERE portfolio=1 AND status='ready'
                    AND kind='photo' ORDER BY id LIMIT 1""")
    return row["id"] if row else None


templates.env.globals["og_image_id"] = _og_image_id
templates.env.globals["instagram_url"] = config.INSTAGRAM_URL
templates.env.globals["contact_email"] = config.CONTACT_EMAIL
templates.env.globals["plausible_domain"] = config.PLAUSIBLE_DOMAIN


def _portfolio_alt(asset, site_name: str | None = None) -> str:
    """Accessible alt text from portfolio_tag when present."""
    name = site_name or config.SITE_NAME
    tag = ""
    if isinstance(asset, dict):
        tag = (asset.get("portfolio_tag") or "").strip()
    if tag:
        return f"{tag.capitalize()} — food & beverage photography by {name}"
    return f"Food & beverage photography by {name}"


templates.env.filters["portfolio_alt"] = _portfolio_alt


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


def _localtime(utc_str, fmt="%a %b %-d · %-I:%M %p"):
    """Format a stored 'YYYY-MM-DD HH:MM:SS' UTC instant in the business timezone.
    Scheduler bookings store UTC; admin/manage views read in Kevin's local time."""
    if not utc_str:
        return ""
    try:
        d = dt.datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc).astimezone(ZoneInfo(config.TIMEZONE))
    except (ValueError, TypeError):
        return utc_str
    return d.strftime(fmt)


templates.env.filters["localtime"] = _localtime


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
    rows = db.all_("SELECT name, subject, body FROM email_templates "
                   "WHERE deleted_at IS NULL ORDER BY name")
    ctx = _merge_ctx(d, p, doc_url)
    return [{"name": r["name"],
             "subject": _apply_merge(r["subject"], ctx),
             "body": _apply_merge(r["body"], ctx)} for r in rows]


templates.env.globals["email_template_options"] = email_template_options
