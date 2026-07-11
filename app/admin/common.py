"""Shared admin helpers (start of module splits for large admin files)."""

import datetime as dt
from pathlib import Path

from .. import db


async def save_upload(file, dest: Path) -> int:
    """Stream an UploadFile to `dest` in 1 MiB chunks; return bytes written.
    The gallery, brand-asset, brand-kit-logo, and expense-receipt upload handlers
    all repeated this exact loop — one implementation keeps the streaming + size
    accounting in a single place."""
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
            size += len(chunk)
    return size


# Dark-panel tints (editorial-dark, Revamp PR-E) — these feed the .gx-badge
# inline style directly, so they can't be reached by CSS; match the same
# ok/honey/neutral/clay status tokens the rest of the admin shell uses.
_STATUS_STYLE = {
    "Delivered": ("#9cc178", "#20271a"),
    "Proofing": ("#d8a857", "#2b2413"),
    "Draft": ("#aba9a3", "#242424"),
    "Expiring": ("#d98a78", "#2e1a18"),
}


def parse_form_cents(form, key: str) -> int:
    """Form dollar field → integer cents (empty/missing → 0). Raises ValueError on
    non-numeric input so each route can 400 with its own field-specific message."""
    return round(float(form.get(key) or "0") * 100)


def open_invoice_balance():
    """Count + total cents still owed across all currently-open invoices — the AR
    figure behind the studio/reports/activity/financials 'outstanding' widgets.
    A deposit_paid invoice owes (total - deposit); sent/viewed owe the full total."""
    return db.one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(CASE
             WHEN status='deposit_paid' THEN total_cents - deposit_cents
             ELSE total_cents END), 0) AS cents
           FROM invoices WHERE status IN ('sent','viewed','deposit_paid')"""
    )


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def fmt_size(n: int) -> str:
    if n <= 0:
        return "—"
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e3:.0f} KB"


def short_date(stored: str) -> str:
    """'2026-06-18 12:00:00' → 'Jun 18'. Tolerates a bare date or junk."""
    if not stored:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(stored[:19], fmt).strftime("%b %-d")
        except ValueError:
            continue
    return stored


def today() -> dt.date:
    """Studio wall-clock today (local). Monkeypatchable."""
    return dt.date.today()


def gallery_card(g, today_iso: str, soon_iso: str) -> dict:
    exp = g["expires_at"]
    expired = bool(exp and exp < today_iso)
    expiring_soon = bool(exp and not expired and exp <= soon_iso)
    if not g["published"]:
        status = "Draft"
    elif expired or expiring_soon:
        status = "Expiring"
    elif g["n_proof"] and g["n_proof_pending"]:
        status = "Proofing"
    else:
        status = "Delivered"
    color, bg = _STATUS_STYLE[status]
    if status == "Expiring":
        if expired:
            date_label = "expired"
        else:
            days = (dt.date.fromisoformat(exp) - dt.date.fromisoformat(today_iso)).days
            date_label = f"{days} day{'s' if days != 1 else ''}"
        date_color = "#d98a78"
    else:
        date_label = short_date(g["created_at"])
        date_color = "#bfc3c8"
    n = g["n_assets"]
    photos = f"{n} photo{'s' if n != 1 else ''}" if n else "No photos yet"
    return {
        "id": g["id"],
        "title": g["title"],
        "client": g["client_name"] or "—",
        "cover_asset_id": g["cover_asset_id"],
        "pin": g["pin"],
        "status": status,
        "status_lc": status.lower(),
        "status_color": color,
        "status_bg": bg,
        "photos": photos,
        "favs": g["n_fav"],
        "date": date_label,
        "date_color": date_color,
    }


def _clients_with_hints() -> tuple[list, dict]:
    """Clients with per-client project counts + portal engagement, plus a friendly
    "visited Xh ago" / "never visited" hint keyed by client id so the template
    stays declarative. Portal engagement (Phase 2) is otherwise invisible from the
    studio. last_visit is stored UTC; compared against a UTC 'now' here."""
    clients = db.all_("""SELECT c.*,
                         (SELECT COUNT(*) FROM projects p WHERE p.client_id=c.id) AS n_projects,
                         (SELECT po.published FROM portals po WHERE po.client_id=c.id) AS portal_published,
                         (SELECT po.visits FROM portals po WHERE po.client_id=c.id) AS portal_visits,
                         (SELECT po.last_visit FROM portals po WHERE po.client_id=c.id) AS portal_last_visit
                         FROM clients c ORDER BY c.name""")
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    hints = {}
    for c in clients:
        if c["portal_published"] is None:
            hints[c["id"]] = ("muted", "no portal")
        elif not c["portal_last_visit"]:
            hints[c["id"]] = ("muted", "never visited")
        else:
            try:
                last = dt.datetime.fromisoformat(c["portal_last_visit"])
            except ValueError:
                hints[c["id"]] = ("muted", "visited (date unknown)")
                continue
            delta = now - last
            if delta.total_seconds() < 60:
                hint = "just now"
            elif delta.total_seconds() < 3600:
                hint = f"{int(delta.total_seconds() // 60)}m ago"
            elif delta.total_seconds() < 86400:
                hint = f"{int(delta.total_seconds() // 3600)}h ago"
            elif delta.days < 30:
                hint = f"{delta.days}d ago"
            else:
                hint = last.date().isoformat()
            hints[c["id"]] = ("ok", f"👁 {hint}")
    return clients, hints
