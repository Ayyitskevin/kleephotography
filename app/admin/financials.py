"""Financials — honest money pages over Mise's REAL invoices + payments.

Adapts the Admin Financials / Client P&L prototypes. Two pages run on real
data: Income (collected cash from the `payments` table, outstanding from open
`invoices`) and Client P&L (per-client collected revenue + project counts).
Mise stores no sales-tax, no Stripe-fee, and no expense/mileage/receipt data,
so those columns are shown honestly ($0.00 / "—" / not-tracked) and the
Expenses, Mileage, and Receipts pages are honest "not built yet" scaffolds
rather than fabricated ledgers. Everything here is read-only — no writes, so
nothing narrates to the Notion Activity Log.
"""

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import db, security
from ..render import templates

router = APIRouter(prefix="/admin/financials",
                   dependencies=[Depends(security.require_admin)])

_RANGES = [("month", "This month"), ("quarter", "Quarter"),
           ("ytd", "YTD"), ("lastyear", "Last year")]
_RANGE_LABELS = dict(_RANGES)

# avatar tints for the Client P&L table, indexed by row position
_AV_COLORS = ["#7C2F38", "#2f6d8a", "#2f7d57", "#9a7a2c",
              "#143C2F", "#5C6A5E", "#b5642e"]


def _usd(cents: int) -> str:
    return "$" + f"{cents / 100:,.2f}"


def _usd0(cents: int) -> str:
    return "$" + f"{round(cents / 100):,}"


def _initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _range_bounds(key: str) -> tuple[str, str]:
    """(start, end) ISO dates, end exclusive. Default quarter."""
    today = dt.date.today()
    if key == "month":
        start = today.replace(day=1)
        end = (dt.date(today.year + 1, 1, 1) if today.month == 12
               else dt.date(today.year, today.month + 1, 1))
    elif key == "ytd":
        start, end = dt.date(today.year, 1, 1), dt.date(today.year + 1, 1, 1)
    elif key == "lastyear":
        start, end = dt.date(today.year - 1, 1, 1), dt.date(today.year, 1, 1)
    else:  # quarter
        q_start_month = 3 * ((today.month - 1) // 3) + 1
        start = dt.date(today.year, q_start_month, 1)
        end_month = q_start_month + 3
        end = (dt.date(today.year + 1, 1, 1) if end_month > 12
               else dt.date(today.year, end_month, 1))
    return start.isoformat(), end.isoformat()


def _collected_rows(start: str, end: str):
    """Real Stripe payment events in range, newest first."""
    return db.all_(
        """SELECT pm.created_at AS d, pm.amount_cents AS cents, pm.kind AS kind,
                  i.id AS inv_id, i.title AS title,
                  c.name AS client, c.company AS company
           FROM payments pm
           JOIN invoices i ON i.id = pm.invoice_id
           JOIN projects p ON p.id = i.project_id
           JOIN clients  c ON c.id = p.client_id
           WHERE pm.created_at >= ? AND pm.created_at < ?
           ORDER BY pm.created_at DESC""", (start, end))


def _outstanding_rows(start: str, end: str):
    """Open invoices created in range — real AR, remaining balance owed."""
    return db.all_(
        """SELECT i.created_at AS d, i.id AS inv_id, i.title AS title,
                  i.status AS status,
                  CASE WHEN i.status='deposit_paid'
                       THEN i.total_cents - i.deposit_cents
                       ELSE i.total_cents END AS cents,
                  c.name AS client, c.company AS company
           FROM invoices i
           JOIN projects p ON p.id = i.project_id
           JOIN clients  c ON c.id = p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
             AND i.created_at >= ? AND i.created_at < ?
           ORDER BY i.created_at DESC""", (start, end))


def _open_total() -> dict:
    """All currently-open invoices (not range-bound) — what's owed right now."""
    return db.one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(CASE
             WHEN status='deposit_paid' THEN total_cents - deposit_cents
             ELSE total_cents END), 0) AS cents
           FROM invoices WHERE status IN ('sent','viewed','deposit_paid')""")


def _ledger(start: str, end: str) -> list[dict]:
    out = []
    for r in _collected_rows(start, end):
        out.append({
            "raw": r["d"], "date": (r["d"] or "")[5:10],
            "client": r["company"] or r["client"],
            "inv": f"#{r['inv_id']:04d}", "service": r["title"],
            "amount": _usd(r["cents"]), "tax": "$0.00", "fee": "—",
            "net": _usd(r["cents"]),
            "status": "Paid", "st": "paid",
        })
    for r in _outstanding_rows(start, end):
        out.append({
            "raw": r["d"], "date": (r["d"] or "")[5:10],
            "client": r["company"] or r["client"],
            "inv": f"#{r['inv_id']:04d}", "service": r["title"],
            "amount": _usd(r["cents"]), "tax": "$0.00", "fee": "—",
            "net": _usd(r["cents"]),
            "status": "Outstanding", "st": "out",
        })
    out.sort(key=lambda x: x["raw"] or "", reverse=True)
    return out


@router.get("", response_class=HTMLResponse)
async def income(request: Request, range: str = "quarter"):
    if range not in _RANGE_LABELS:
        range = "quarter"
    start, end = _range_bounds(range)

    collected = db.one(
        """SELECT COALESCE(SUM(amount_cents),0) AS cents, COUNT(*) AS n
           FROM payments WHERE created_at >= ? AND created_at < ?""",
        (start, end))
    openv = _open_total()
    rows = _ledger(start, end)

    cards = [
        {"label": "Collected", "value": _usd(collected["cents"]),
         "tone": "dark",
         "sub": f"{collected['n']} payment{'' if collected['n'] == 1 else 's'}"
                f" · {_RANGE_LABELS[range].lower()}"},
        {"label": "Outstanding", "value": _usd(openv["cents"]),
         "tone": "warn",
         "sub": f"{openv['n']} open invoice{'' if openv['n'] == 1 else 's'}"
                " · all-time"},
        {"label": "Sales tax", "value": "$0.00", "tone": "muted",
         "sub": "not collected"},
        {"label": "Processing fees", "value": "—", "tone": "danger",
         "sub": "shown in Stripe"},
        {"label": "Net income", "value": _usd(collected["cents"]), "tone": "ok",
         "sub": "fees not deducted here"},
    ]

    ranges = [{"key": k, "label": lbl, "on": k == range}
              for k, lbl in _RANGES]

    return templates.TemplateResponse(request, "admin/financials.html", {
        "active": "income", "cards": cards, "rows": rows,
        "ranges": ranges, "range": range,
        "range_label": _RANGE_LABELS[range],
    })


@router.get("/income.csv", response_class=PlainTextResponse)
async def income_csv(range: str = "quarter"):
    """Collected cash + open AR in range — accountant-ready. Real data only;
    no fabricated tax or processing-fee columns (Mise stores neither)."""
    if range not in _RANGE_LABELS:
        range = "quarter"
    start, end = _range_bounds(range)
    rows = _ledger(start, end)
    out = ["date,client,invoice,service,amount_usd,sales_tax_usd,status"]
    for r in rows:
        amt = r["amount"].replace("$", "").replace(",", "")
        client = '"' + r["client"].replace('"', '""') + '"'
        service = '"' + (r["service"] or "").replace('"', '""') + '"'
        out.append(f"{r['raw'][:10]},{client},{r['inv']},{service},"
                   f"{amt},0.00,{r['status']}")
    return "\n".join(out) + "\n"


@router.get("/clients", response_class=HTMLResponse)
async def client_pnl(request: Request, sort: str = "revenue"):
    if sort not in ("revenue", "projects"):
        sort = "revenue"
    order = ("n_projects DESC, revenue_cents DESC" if sort == "projects"
             else "revenue_cents DESC, last_paid DESC")
    clients = db.all_(
        f"""SELECT c.id, c.name, c.company,
                   COALESCE(SUM(pm.amount_cents),0) AS revenue_cents,
                   COUNT(DISTINCT i.project_id) AS n_projects,
                   MAX(pm.created_at) AS last_paid
            FROM clients c
            JOIN projects p ON p.client_id = c.id
            JOIN invoices i ON i.project_id = p.id
            JOIN payments pm ON pm.invoice_id = i.id
            GROUP BY c.id
            ORDER BY {order}""")

    total = sum(c["revenue_cents"] for c in clients) or 0
    repeat = sum(1 for c in clients if c["n_projects"] >= 2)
    top = clients[0] if clients else None

    rows = []
    for i, c in enumerate(clients):
        share = round(100 * c["revenue_cents"] / total) if total else 0
        rows.append({
            "name": c["company"] or c["name"],
            "initials": _initials(c["company"] or c["name"]),
            "av": _AV_COLORS[i % len(_AV_COLORS)],
            "projects": c["n_projects"],
            "revenue": _usd0(c["revenue_cents"]),
            "share": share, "share_w": f"{share}%",
        })

    cards = [
        {"label": "Total collected", "value": _usd0(total), "tone": "dark",
         "sub": f"across {len(clients)} paying client"
                f"{'' if len(clients) == 1 else 's'}"},
        {"label": "Paying clients", "value": str(len(clients)), "tone": "plain",
         "sub": "have paid an invoice"},
        {"label": "Top client", "value": (top["company"] or top["name"])
         if top else "—", "tone": "ok",
         "sub": _usd0(top["revenue_cents"]) if top else "no payments yet"},
        {"label": "Repeat bookers", "value": str(repeat), "tone": "warn",
         "sub": "2+ paid projects"},
    ]

    sorts = [{"key": "revenue", "label": "By revenue", "on": sort == "revenue"},
             {"key": "projects", "label": "By projects",
              "on": sort == "projects"}]

    return templates.TemplateResponse(request, "admin/financials_clients.html", {
        "active": "clients", "cards": cards, "rows": rows, "sorts": sorts,
    })


_SOON = {
    "expenses": {
        "active": "expenses", "title": "Expenses & deductions",
        "sub": "Receipts, write-offs, and tax set-aside",
        "heading": "Expense tracking isn't built yet",
        "body": "Mise records the money coming in — collected payments and open "
                "invoices — but it doesn't yet track expenses, deductions, or "
                "tax set-aside. Keep those in your accountant's tools for now; "
                "the Income page and CSV export give them the revenue side.",
    },
    "mileage": {
        "active": "mileage", "title": "Mileage",
        "sub": "Business miles for the write-off",
        "heading": "Mileage logging isn't built yet",
        "body": "There's no mileage log in Mise yet. Track business miles in a "
                "dedicated app or spreadsheet for the IRS standard-rate "
                "deduction — Mise stays focused on the revenue side.",
    },
    "receipts": {
        "active": "receipts", "title": "Receipt inbox",
        "sub": "Snap, forward, and match receipts to expenses",
        "heading": "The receipt inbox isn't built yet",
        "body": "Mise has no receipt capture or matching yet. Hold onto "
                "receipts in your accounting tool — when expense tracking "
                "lands here, this is where they'll live.",
    },
}


@router.get("/{page}", response_class=HTMLResponse)
async def soon(request: Request, page: str):
    ctx = _SOON.get(page)
    if ctx is None:
        # unknown sub-page → bounce to Income
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin/financials", status_code=303)
    return templates.TemplateResponse(request, "admin/financials_soon.html",
                                      {**ctx})
