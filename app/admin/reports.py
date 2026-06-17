import datetime as dt
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import db, security
from ..render import templates
from .studio import PROJECT_STATUSES

log = logging.getLogger("mise.admin.reports")
router = APIRouter(prefix="/admin/reports",
                   dependencies=[Depends(security.require_admin)])


def _year_bounds(year):
    return f"{year}-01-01", f"{year + 1}-01-01"


def _months_back(n=12):
    """List of (YYYY-MM, 'Mon YY') from oldest to newest, ending this month."""
    today = dt.date.today().replace(day=1)
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append((f"{y:04d}-{m:02d}", dt.date(y, m, 1).strftime("%b %y")))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    out.reverse()
    return out


def _collected_by_month():
    """Cash collected per YYYY-MM from Stripe payment events (source of truth)."""
    rows = db.all_(
        """SELECT strftime('%Y-%m', created_at) AS ym,
                  COALESCE(SUM(amount_cents), 0) AS cents
           FROM payments GROUP BY ym""")
    return {r["ym"]: r["cents"] for r in rows}


@router.get("", response_class=HTMLResponse)
async def reports(request: Request, year: int | None = None):
    """Read-only business analytics — the HoneyBook 'Reports' tab. Cash from
    the payments (Stripe webhook) table is the truth for collected revenue;
    invoices give booked value and AR; inquiries give leads/conversion.
    No writes, so nothing narrates to the Notion Activity Log."""
    this_year = dt.date.today().year
    if year is None:
        year = this_year
    y_start, y_end = _year_bounds(year)

    collected = db.one(
        """SELECT COALESCE(SUM(amount_cents), 0) AS cents, COUNT(*) AS n
           FROM payments WHERE created_at >= ? AND created_at < ?""",
        (y_start, y_end))
    outstanding = db.one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(CASE
             WHEN status='deposit_paid' THEN total_cents - deposit_cents
             ELSE total_cents END), 0) AS cents
           FROM invoices WHERE status IN ('sent','viewed','deposit_paid')""")
    booked = db.one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(total_cents), 0) AS cents
           FROM invoices
           WHERE status != 'draft' AND created_at >= ? AND created_at < ?""",
        (y_start, y_end))
    leads_year = db.one(
        """SELECT COUNT(*) AS n FROM inquiries
           WHERE created_at >= ? AND created_at < ?
             AND dismissed_at IS NULL""", (y_start, y_end))["n"]

    # rolling 12-month collected revenue (Python buckets so empty months show 0)
    by_month = _collected_by_month()
    months = _months_back(12)
    chart = [{"label": lbl, "cents": by_month.get(ym, 0)} for ym, lbl in months]
    chart_max = max((m["cents"] for m in chart), default=0) or 1

    # pipeline: projects by status with booked (non-draft) invoice value, plus
    # the longest-sitting project per stage. stage_changed_at (migration 032) is
    # set on every advance, so this is true time-in-stage; COALESCE to created_at
    # covers rows that haven't moved since the column was added / since creation.
    pstat = {r["status"]: r for r in db.all_(
        """SELECT p.status,
                  COUNT(DISTINCT p.id) AS n,
                  COALESCE(SUM(CASE WHEN i.status != 'draft'
                                    THEN i.total_cents ELSE 0 END), 0) AS cents,
                  CAST(MAX(julianday('now')
                           - julianday(COALESCE(p.stage_changed_at, p.created_at)))
                       AS INT) AS oldest_days
           FROM projects p LEFT JOIN invoices i ON i.project_id = p.id
           GROUP BY p.status""")}

    def _cur(s, k):
        return pstat[s][k] if s in pstat else 0

    # Funnel over the active sales stages (archived is terminal/lost, excluded).
    # Stage advances are forward-only (see studio/docs/pay), so a project's
    # current stage implies it passed through every earlier stage. That lets us
    # treat "currently at or beyond stage i" as a proxy for "reached stage i".
    funnel_stages = [s for s in PROJECT_STATUSES if s != "archived"]
    total_active = sum(_cur(s, "n") for s in funnel_stages)
    funnel = []
    prev_reach = None
    for i, s in enumerate(funnel_stages):
        reach = sum(_cur(funnel_stages[j], "n")
                    for j in range(i, len(funnel_stages)))
        funnel.append({
            "status": s,
            "current": _cur(s, "n"),
            "cents": _cur(s, "cents"),
            "reach": reach,
            "pct": round(100 * reach / total_active) if total_active else 0,
            "conv": (round(100 * reach / prev_reach)
                     if prev_reach else None),
            "oldest_days": _cur(s, "oldest_days"),
        })
        prev_reach = reach

    won = _cur("project_closed", "n")
    archived = {"n": _cur("archived", "n"), "cents": _cur("archived", "cents")}
    total_all = total_active + archived["n"]
    win_rate = round(100 * won / total_all) if total_all else 0

    # leads & conversion (all-time)
    leads_total = db.one(
        "SELECT COUNT(*) AS n FROM inquiries WHERE dismissed_at IS NULL")["n"]
    leads_converted = db.one(
        "SELECT COUNT(*) AS n FROM inquiries "
        "WHERE converted_at IS NOT NULL AND dismissed_at IS NULL")["n"]
    conv_rate = round(100 * leads_converted / leads_total) if leads_total else 0
    leads_by_kind = db.all_(
        """SELECT COALESCE(kind, 'contact') AS kind, COUNT(*) AS n
           FROM inquiries WHERE dismissed_at IS NULL
           GROUP BY COALESCE(kind, 'contact')
           ORDER BY n DESC""")

    # delivery & engagement (all-time)
    delivery = {
        "galleries": db.one("SELECT COUNT(*) AS n FROM galleries")["n"],
        "delivered": db.one(
            "SELECT COUNT(*) AS n FROM projects WHERE status='project_closed'")["n"],
        "downloads": db.one("SELECT COUNT(*) AS n FROM downloads")["n"],
        "emails": db.one(
            "SELECT COUNT(DISTINCT email) AS n FROM visitors "
            "WHERE email IS NOT NULL")["n"],
        "submissions": db.one("SELECT COUNT(*) AS n FROM form_submissions")["n"],
    }

    # Top clients by lifetime cash collected (all-time). Cash from payments is the
    # truth (R21); n_paid_projects counts distinct projects that actually paid, so
    # >=2 flags a repeat booker. Only paying clients appear — it's a value table.
    top_clients = db.all_(
        """SELECT c.id, c.name, c.company,
                  COALESCE(SUM(pm.amount_cents), 0) AS collected_cents,
                  COUNT(DISTINCT i.project_id) AS n_paid_projects,
                  MAX(pm.created_at) AS last_paid
           FROM clients c
           JOIN projects p ON p.client_id = c.id
           JOIN invoices i ON i.project_id = p.id
           JOIN payments pm ON pm.invoice_id = i.id
           GROUP BY c.id
           ORDER BY collected_cents DESC, last_paid DESC
           LIMIT 10""")

    years = list(range(this_year, this_year - 4, -1))

    return templates.TemplateResponse(request, "admin/reports.html", {
        "year": year, "this_year": this_year, "years": years,
        "collected": collected, "outstanding": outstanding,
        "booked": booked, "leads_year": leads_year,
        "chart": chart, "chart_max": chart_max,
        "funnel": funnel, "win_rate": win_rate,
        "won": won, "archived": archived, "total_active": total_active,
        "leads_total": leads_total, "leads_converted": leads_converted,
        "conv_rate": conv_rate, "leads_by_kind": leads_by_kind,
        "delivery": delivery, "top_clients": top_clients,
    })


@router.get("/revenue.csv", response_class=PlainTextResponse)
async def revenue_csv():
    """Collected cash per month, all-time — for the accountant/spreadsheet."""
    rows = db.all_(
        """SELECT strftime('%Y-%m', created_at) AS month,
                  COALESCE(SUM(amount_cents), 0) AS cents
           FROM payments GROUP BY month ORDER BY month""")
    lines = ["month,collected_usd"]
    lines += [f"{r['month']},{r['cents'] / 100:.2f}" for r in rows]
    return "\n".join(lines) + "\n"
