"""Shared admin helpers (start of module splits for large admin files)."""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from .. import db


async def save_upload(file, dest: Path) -> int:
    """Stream an UploadFile to `dest` in 1 MiB chunks; return bytes written.

    The read is the await — it cannot leave the loop. Each disk write hops to
    the threadpool so a multi-megabyte receipt or gallery original cannot stall
    every other request. The gallery, brand-asset, brand-kit-logo, and
    expense-receipt handlers all share this.
    """
    size = 0
    out = await run_in_threadpool(dest.open, "wb")
    try:
        while chunk := await file.read(1 << 20):
            await run_in_threadpool(out.write, chunk)
            size += len(chunk)
    finally:
        await run_in_threadpool(out.close)
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


def doc_emails_on_record(doc_kind: str, doc_id: int) -> int:
    """How many sends Mise has on record for one studio doc.

    For doc_kind 'proposal'/'contract'/'invoice', emails_log rows come only
    from the in-app "Email to client" send (after SMTP succeeds) — the table
    itself has other writers (gallery delivery, inbox replies) but they write
    doc_kind 'other', so the filter below is load-bearing. A copy Kevin mailed
    from his own client, or a link he texted, leaves no row here. Callers
    therefore say "no send recorded", never "never sent".
    The three doc detail pages and the Home no-send nudge all read this one
    query so they can never disagree about the same doc."""
    return db.one(
        "SELECT COUNT(*) AS n FROM emails_log WHERE doc_kind=? AND doc_id=?",
        (doc_kind, doc_id),
    )["n"]


def collected_by_month() -> dict[str, dict[str, int]]:
    """Cash collected per YYYY-MM from Stripe payment events (the source of
    truth for revenue — an invoice total stamped by paid_at counts a
    deposit-paid-but-still-open invoice as zero).

    Maps 'YYYY-MM' to {"cents": ..., "n": ...}. The count rides along because
    Home prints "N payments" beside the figure and used to fetch it with a second
    query carrying its own copy of the bucketing expression below — the fourth
    copy of a rule that had already drifted once (see month_money_series).

    created_at is stored UTC, but the month is decided on the operator's wall
    clock ('localtime'): a 9 PM payment on the last of the month is already the
    first of the next month in UTC, and it belongs to the month he collected
    it. Months with no cash are simply absent from the mapping."""
    return {
        r["ym"]: {"cents": r["cents"], "n": r["n"]}
        for r in db.all_(
            """SELECT strftime('%Y-%m', created_at, 'localtime') AS ym,
                      COALESCE(SUM(amount_cents), 0) AS cents,
                      COUNT(*) AS n
               FROM payments GROUP BY ym"""
        )
    }


# Months with no cash are absent from the mapping; this is what they look like.
NO_CASH = {"cents": 0, "n": 0}


def month_money_series(
    today: dt.date,
    months: int,
    label: Callable[[dt.date], str],
    *,
    goal_cents: int = 0,
    bars: bool = True,
    by_ym: dict[str, dict[str, int]] | None = None,
) -> tuple[list[dict], int]:
    """The trailing `months` months of collected cash ending with `today`'s
    month, oldest first, plus the scale their bars are drawn against.

    Home's revenue reel, the Financials month reel and the Reports chart are all
    this computation. They were three copies once and drifted, so what genuinely
    differs between them is a parameter here: `months` is the window, `label`
    turns a first-of-month date into that caller's bar caption, `goal_cents`
    folds a target into the scale so a goal silhouette stays the tallest thing
    on the card (Home alone sets one), and `bars` adds the per-month height and
    current-month flag the two six-month reels read — Reports draws its own
    bars from the returned scale instead.

    Both halves land on the operator's calendar: the window is built from the
    caller's `today` (a studio wall-clock date) and the buckets convert with
    'localtime'. Bucketing on UTC instead would move an evening payment into
    the following month. An empty month still gets a 4% stub so it draws as a
    flat bar rather than vanishing from the reel."""
    first_of_month = today.replace(day=1)
    window, cursor = [], first_of_month
    for _ in range(months):
        window.append(cursor)
        cursor = (cursor - dt.timedelta(days=1)).replace(day=1)
    window.reverse()
    # `by_ym` lets a caller that already holds the mapping pass it in — Home
    # renders both this reel and the month-to-date figure, and would otherwise
    # run the same GROUP BY twice for one page.
    by_ym = collected_by_month() if by_ym is None else by_ym
    cents = [by_ym.get(m.strftime("%Y-%m"), NO_CASH)["cents"] for m in window]
    # The 1 floor keeps a studio with no cash yet from dividing by zero.
    scale = max(cents + [goal_cents or 0, 1])
    series = []
    for month, amount in zip(window, cents, strict=True):
        row = {"label": label(month), "cents": amount}
        if bars:
            row["pct"] = max(4, round(amount * 100 / scale))
            row["current"] = month == first_of_month
        series.append(row)
    return series, scale


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def original_bytes_by_gallery(gallery_type: str) -> dict[int, int]:
    """Per-gallery SUM of assets.bytes for one gallery type, in one grouped query.

    assets.bytes is the ORIGINAL upload size, and nothing records the size of a
    derivative (web, thumb, crops, renditions). So this is the size of what a
    client actually downloads — the ZIP is built from the originals — and NOT
    on-disk usage: the library strips that show it say "originals" for that
    reason. A gallery with no assets is absent from the mapping."""
    return {
        r["gallery_id"]: r["bytes"]
        for r in db.all_(
            """SELECT a.gallery_id AS gallery_id, COALESCE(SUM(a.bytes), 0) AS bytes
               FROM assets a JOIN galleries g ON g.id = a.gallery_id
               WHERE g.type = ? GROUP BY a.gallery_id""",
            (gallery_type,),
        )
    }


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
