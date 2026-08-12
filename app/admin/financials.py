"""Financials — honest money pages over Mise's REAL invoices + payments, plus
the operator's own bookkeeping (expenses, mileage, receipts).

Adapts the Admin Financials / Client P&L / Expenses / Mileage / Receipts
prototypes. Income (collected cash from `payments`, outstanding from open
`invoices`) and Client P&L are read-only over real Stripe data; Mise stores no
sales-tax or processing fee, so those columns read honestly ($0.00 / "—").

Expenses, Mileage, and Receipts are real CRUD over operator-entered data — Kevin's
own bookkeeping, not client money. The prototypes' fabricated features (calendar
auto-detect of trips, email auto-matching of receipts, hardcoded 1099 watch, and
invented tax set-aside goals) are dropped: Mise has no data source for them.
Deductible % and the IRS mileage rate are honest operator inputs.
"""

import csv
import datetime as dt
import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

from .. import config, db, security
from ..render import templates
from . import common, studio

router = APIRouter(prefix="/admin/financials", dependencies=[Depends(security.require_admin)])

_RANGES = [
    ("month", "This month"),
    ("quarter", "Quarter"),
    ("ytd", "YTD"),
    ("lastyear", "Last year"),
]
_RANGE_LABELS = dict(_RANGES)

# avatar tints for the Client P&L table, indexed by row position
_AV_COLORS = ["#7C2F38", "#2f6d8a", "#2f7d57", "#9a7a2c", "#143C2F", "#5C6A5E", "#b5642e"]


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
    """(start, end) ISO dates, end exclusive. Default quarter.

    These come off the studio's wall clock, so every query that spends them
    converts stored-UTC created_at with 'localtime' before comparing. Straight
    string comparison would drop a payment taken late on the last evening of the
    range — already tomorrow in UTC — out of the month it was collected in."""
    today = studio._today()
    if key == "month":
        start = today.replace(day=1)
        end = (
            dt.date(today.year + 1, 1, 1)
            if today.month == 12
            else dt.date(today.year, today.month + 1, 1)
        )
    elif key == "ytd":
        start, end = dt.date(today.year, 1, 1), dt.date(today.year + 1, 1, 1)
    elif key == "lastyear":
        start, end = dt.date(today.year - 1, 1, 1), dt.date(today.year, 1, 1)
    else:  # quarter
        q_start_month = 3 * ((today.month - 1) // 3) + 1
        start = dt.date(today.year, q_start_month, 1)
        end_month = q_start_month + 3
        end = dt.date(today.year + 1, 1, 1) if end_month > 12 else dt.date(today.year, end_month, 1)
    return start.isoformat(), end.isoformat()


def _collected_rows(start: str, end: str):
    """Real Stripe payment events in range, newest first. `d` rides out on the
    studio clock so the date a row prints is the date that selected it."""
    return db.all_(
        """SELECT datetime(pm.created_at, 'localtime') AS d,
                  pm.amount_cents AS cents, pm.kind AS kind,
                  i.id AS inv_id, i.title AS title,
                  c.name AS client, c.company AS company
           FROM payments pm
           JOIN invoices i ON i.id = pm.invoice_id
           JOIN projects p ON p.id = i.project_id
           JOIN clients  c ON c.id = p.client_id
           WHERE date(pm.created_at, 'localtime') >= ?
             AND date(pm.created_at, 'localtime') < ?
           ORDER BY pm.created_at DESC""",
        (start, end),
    )


def _outstanding_rows(start: str, end: str):
    """Open invoices created in range — real AR, remaining balance owed."""
    return db.all_(
        """SELECT datetime(i.created_at, 'localtime') AS d, i.id AS inv_id, i.title AS title,
                  i.status AS status,
                  CASE WHEN i.status='deposit_paid'
                       THEN i.total_cents - i.deposit_cents
                       ELSE i.total_cents END AS cents,
                  c.name AS client, c.company AS company
           FROM invoices i
           JOIN projects p ON p.id = i.project_id
           JOIN clients  c ON c.id = p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
             AND date(i.created_at, 'localtime') >= ?
             AND date(i.created_at, 'localtime') < ?
           ORDER BY i.created_at DESC""",
        (start, end),
    )


def _ledger(start: str, end: str) -> list[dict]:
    out = []
    for r in _collected_rows(start, end):
        out.append(
            {
                "raw": r["d"],
                "date": (r["d"] or "")[5:10],
                "client": r["company"] or r["client"],
                "inv": f"#{r['inv_id']:04d}",
                "service": r["title"],
                "cents": r["cents"],
                "amount": _usd(r["cents"]),
                "tax": "$0.00",
                "fee": "—",
                "net": _usd(r["cents"]),
                "status": "Paid",
                "st": "paid",
                "inv_id": r["inv_id"],
                "act": "Receipt",
            }
        )
    for r in _outstanding_rows(start, end):
        out.append(
            {
                "raw": r["d"],
                "date": (r["d"] or "")[5:10],
                "client": r["company"] or r["client"],
                "inv": f"#{r['inv_id']:04d}",
                "service": r["title"],
                "cents": r["cents"],
                "amount": _usd(r["cents"]),
                "tax": "$0.00",
                "fee": "—",
                "net": _usd(r["cents"]),
                "status": "Outstanding",
                "st": "out",
                "inv_id": r["inv_id"],
                "act": "Nudge",
            }
        )
    out.sort(key=lambda x: x["raw"] or "", reverse=True)
    return out


@router.get("", response_class=HTMLResponse)
def income(request: Request, range: str = "quarter"):
    if range not in _RANGE_LABELS:
        range = "quarter"
    start, end = _range_bounds(range)

    collected = db.one(
        """SELECT COALESCE(SUM(amount_cents),0) AS cents, COUNT(*) AS n
           FROM payments
           WHERE date(created_at, 'localtime') >= ?
             AND date(created_at, 'localtime') < ?""",
        (start, end),
    )
    openv = common.open_invoice_balance()
    rows = _ledger(start, end)

    cards = [
        {
            "label": "Collected",
            "value": _usd(collected["cents"]),
            "tone": "dark",
            "sub": f"{collected['n']} payment{'' if collected['n'] == 1 else 's'}"
            f" · {_RANGE_LABELS[range].lower()}",
        },
        {
            "label": "Outstanding",
            "value": _usd(openv["cents"]),
            "tone": "warn",
            "sub": f"{openv['n']} open invoice{'' if openv['n'] == 1 else 's'} · all-time",
        },
        {"label": "Sales tax", "value": "$0.00", "tone": "muted", "sub": "not collected"},
        {"label": "Processing fees", "value": "—", "tone": "danger", "sub": "shown in Stripe"},
        {
            "label": "Net income",
            "value": _usd(collected["cents"]),
            "tone": "ok",
            "sub": "fees not deducted here",
        },
    ]

    ranges = [{"key": k, "label": lbl, "on": k == range} for k, lbl in _RANGES]

    # Month reel (Screening Room 3i): trailing six months of collected cash,
    # heights proportional to the best month. Read-only. No goal is passed —
    # this reel scales to the months themselves, so a target Kevin sets can
    # never shrink the bars here and make the same six months read differently
    # than they do on Home.
    month_reel, _reel_scale = common.month_money_series(
        studio._today(), 6, lambda m: m.strftime("%b").upper()
    )

    return templates.TemplateResponse(
        request,
        "admin/financials.html",
        {
            "active": "income",
            "cards": cards,
            "rows": rows,
            "ranges": ranges,
            "range": range,
            "range_label": _RANGE_LABELS[range],
            "row_count": len(rows),
            "month_reel": month_reel,
        },
    )


@router.get("/income.csv", response_class=PlainTextResponse)
def income_csv(
    range: str = "quarter",
    inc_paid: str = "",
    inc_out: str = "",
    inc: str = "",
    fmt: str = "itemized",
):
    """Collected cash + open AR in range — accountant-ready. Real data only;
    no fabricated tax or processing-fee columns (Mise stores neither). The
    Include checkboxes (Paid / Outstanding) and Format toggle (Itemized /
    Summary) from the export panel genuinely shape the output."""
    if range not in _RANGE_LABELS:
        range = "quarter"
    if fmt not in ("itemized", "summary"):
        fmt = "itemized"
    # HTML omits unchecked checkboxes, so absent Include params are ambiguous:
    # the export panel always sends the `inc` marker (an unchecked box there
    # really means exclude), while a bare link — the topbar's — carries neither
    # and means "everything". Without this an unmarked link exports only headers.
    if not inc and not inc_paid and not inc_out:
        inc_paid = inc_out = "on"
    start, end = _range_bounds(range)
    rows = [
        r
        for r in _ledger(start, end)
        if (inc_paid and r["st"] == "paid") or (inc_out and r["st"] == "out")
    ]

    buf = io.StringIO()
    w = csv.writer(buf)

    if fmt == "summary":
        paid = [r for r in rows if r["st"] == "paid"]
        outstanding = [r for r in rows if r["st"] == "out"]
        w.writerow(["category", "count", "amount_usd"])
        for label, group in (("Paid", paid), ("Outstanding", outstanding)):
            w.writerow([label, len(group), f"{sum(r['cents'] for r in group) / 100:.2f}"])
        return buf.getvalue()

    w.writerow(["date", "client", "invoice", "service", "amount_usd", "sales_tax_usd", "status"])
    for r in rows:
        w.writerow(
            [
                r["raw"][:10],
                r["client"],
                r["inv"],
                r["service"] or "",
                f"{r['cents'] / 100:.2f}",
                "0.00",
                r["status"],
            ]
        )
    return buf.getvalue()


@router.get("/clients", response_class=HTMLResponse)
def client_pnl(request: Request, sort: str = "revenue"):
    if sort not in ("revenue", "projects"):
        sort = "revenue"
    order = (
        "n_projects DESC, revenue_cents DESC"
        if sort == "projects"
        else "revenue_cents DESC, last_paid DESC"
    )
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
            ORDER BY {order}"""
    )

    total = sum(c["revenue_cents"] for c in clients) or 0
    repeat = sum(1 for c in clients if c["n_projects"] >= 2)
    top = clients[0] if clients else None

    rows = []
    for i, c in enumerate(clients):
        share = round(100 * c["revenue_cents"] / total) if total else 0
        rows.append(
            {
                "name": c["company"] or c["name"],
                "initials": _initials(c["company"] or c["name"]),
                "av": _AV_COLORS[i % len(_AV_COLORS)],
                "projects": c["n_projects"],
                "revenue": _usd0(c["revenue_cents"]),
                "share": share,
                "share_w": f"{share}%",
            }
        )

    cards = [
        {
            "label": "Total collected",
            "value": _usd0(total),
            "tone": "dark",
            "sub": f"across {len(clients)} paying client{'' if len(clients) == 1 else 's'}",
        },
        {
            "label": "Paying clients",
            "value": str(len(clients)),
            "tone": "plain",
            "sub": "have paid an invoice",
        },
        {
            "label": "Top client",
            "value": (top["company"] or top["name"]) if top else "—",
            "tone": "ok",
            "sub": _usd0(top["revenue_cents"]) if top else "no payments yet",
        },
        {
            "label": "Repeat bookers",
            "value": str(repeat),
            "tone": "warn",
            "sub": "2+ paid projects",
        },
    ]

    sorts = [
        {"key": "revenue", "label": "By revenue", "on": sort == "revenue"},
        {"key": "projects", "label": "By projects", "on": sort == "projects"},
    ]

    return templates.TemplateResponse(
        request,
        "admin/financials_clients.html",
        {
            "active": "clients",
            "cards": cards,
            "rows": rows,
            "sorts": sorts,
        },
    )


# ── Bookkeeping: expenses · receipts · mileage (operator-entered CRUD) ──

# Honest fixed category set (the prototype's catMeta), each with its dot color.
_EXP_CATS = [
    ("Equipment", "#143C2F"),
    ("Software", "#2f6d8a"),
    ("Travel", "#EDB23C"),
    ("Props & supplies", "#2f7d57"),
    ("Meals", "#7C2F38"),
    ("Contract labor", "#9a7a2c"),
    ("Insurance", "#5C6A5E"),
    ("Other", "#8A9183"),
]
_EXP_CAT_COLOR = dict(_EXP_CATS)
_RECEIPT_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".pdf"}


def _to_cents(raw: str) -> int:
    s = (raw or "").replace("$", "").replace(",", "").strip()
    return round(float(s) * 100)  # raises ValueError on garbage → caller 400s


def _ded(amount_cents: int, pct: int) -> int:
    return round(amount_cents * pct / 100)


@router.get("/expenses", response_class=HTMLResponse)
def expenses(request: Request, cat: str = "all", offset: int = 0):
    all_rows = db.all_("SELECT * FROM expenses ORDER BY spent_on DESC, id DESC")
    if cat not in _EXP_CAT_COLOR:
        cat = "all"
    rows = all_rows if cat == "all" else [r for r in all_rows if r["category"] == cat]
    with_receipt = {
        r["expense_id"]
        for r in db.all_("SELECT expense_id FROM receipts WHERE expense_id IS NOT NULL")
    }

    # Summary totals reflect the whole ledger, not the active category filter.
    total = sum(r["amount_cents"] for r in all_rows)
    deductible = sum(_ded(r["amount_cents"], r["deductible_pct"]) for r in all_rows)
    n_receipts = sum(1 for r in all_rows if r["id"] in with_receipt)

    cat_tot: dict[str, int] = {}
    for r in all_rows:
        cat_tot[r["category"]] = cat_tot.get(r["category"], 0) + _ded(
            r["amount_cents"], r["deductible_pct"]
        )
    max_cat = max(cat_tot.values(), default=0)
    by_cat = [
        {
            "label": k,
            "amount": _usd(v),
            "w": f"{round(100 * v / max_cat) if max_cat else 0}%",
            "color": _EXP_CAT_COLOR.get(k, "#5C6A5E"),
        }
        for k, v in sorted(cat_tot.items(), key=lambda x: -x[1])
    ]

    cards = [
        {
            "label": "Total expenses",
            "value": _usd(total),
            "tone": "muted",
            "sub": f"{len(all_rows)} logged",
        },
        {"label": "Deductible", "value": _usd(deductible), "tone": "ok", "sub": "after per-item %"},
        {
            "label": "Est. tax saved",
            "value": _usd(round(deductible * 0.3)),
            "tone": "warn",
            "sub": "~30% bracket estimate",
        },
        {
            "label": "Receipts on file",
            "value": f"{n_receipts} / {len(all_rows)}",
            "tone": "dark",
            "sub": f"{len(all_rows) - n_receipts} missing" if all_rows else "none yet",
        },
    ]
    # A bookkeeping ledger only grows, so the table renders one page of it. The
    # summary cards, the category bars and expenses.csv stay whole-ledger.
    offset = max(0, offset)
    page_size = 50
    table = [
        {
            "id": r["id"],
            "date": (r["spent_on"] or "")[5:10],
            "vendor": r["vendor"],
            "cat": r["category"],
            "cat_color": _EXP_CAT_COLOR.get(r["category"], "#5C6A5E"),
            "amount": _usd(r["amount_cents"]),
            "ded": _usd(_ded(r["amount_cents"], r["deductible_pct"]))
            + (f" ({r['deductible_pct']}%)" if r["deductible_pct"] < 100 else ""),
            "has_receipt": r["id"] in with_receipt,
        }
        for r in rows[offset : offset + page_size]
    ]

    # Ledger category filter pills — All + the categories actually in use.
    used = [c for c, _ in _EXP_CATS if c in cat_tot]
    pills = [{"key": "all", "label": "All", "on": cat == "all"}] + [
        {"key": c, "label": c, "on": cat == c} for c in used
    ]

    return templates.TemplateResponse(
        request,
        "admin/financials_expenses.html",
        {
            "active": "expenses",
            "cards": cards,
            "rows": table,
            "by_cat": by_cat,
            "cats": [c for c, _ in _EXP_CATS],
            "today": studio._today().isoformat(),
            "pills": pills,
            "cat": cat,
            "total": len(rows),
            "offset": offset,
            "page_size": page_size,
        },
    )


@router.post("/expenses")
def expense_create(
    spent_on: str = Form(...),
    vendor: str = Form(...),
    category: str = Form("Other"),
    amount: str = Form(...),
    deductible_pct: int = Form(100),
    notes: str = Form(""),
):
    vendor = vendor.strip()
    if not vendor:
        raise HTTPException(status_code=400, detail="vendor required")
    # ORDER BY spent_on is a string comparison — a non-ISO date sorts the row
    # into the wrong place in the ledger and the CSV, silently.
    try:
        spent_on = dt.date.fromisoformat(spent_on).isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date")
    try:
        cents = _to_cents(amount)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid amount")
    if cents <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    if category not in _EXP_CAT_COLOR:
        category = "Other"
    pct = max(0, min(100, deductible_pct))
    db.run(
        """INSERT INTO expenses (spent_on, vendor, category, amount_cents,
                                    deductible_pct, notes)
              VALUES (?,?,?,?,?,?)""",
        (spent_on, vendor, category, cents, pct, notes.strip() or None),
    )
    return RedirectResponse("/admin/financials/expenses", status_code=303)


@router.post("/expenses/{expense_id}/delete")
def expense_delete(expense_id: int):
    db.run("DELETE FROM expenses WHERE id=?", (expense_id,))
    return RedirectResponse("/admin/financials/expenses", status_code=303)


@router.get("/expenses.csv", response_class=PlainTextResponse)
def expenses_csv():
    rows = db.all_("SELECT * FROM expenses ORDER BY spent_on DESC, id DESC")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["date", "vendor", "category", "amount_usd", "deductible_pct", "deductible_usd", "notes"]
    )
    for r in rows:
        w.writerow(
            [
                r["spent_on"],
                r["vendor"] or "",
                r["category"],
                f"{r['amount_cents'] / 100:.2f}",
                r["deductible_pct"],
                f"{_ded(r['amount_cents'], r['deductible_pct']) / 100:.2f}",
                r["notes"] or "",
            ]
        )
    return buf.getvalue()


@router.get("/receipts", response_class=HTMLResponse)
def receipts(request: Request, filter: str = "all", offset: int = 0):
    if filter not in ("all", "linked", "unlinked"):
        filter = "all"
    all_rows = db.all_(
        """SELECT rc.*, e.vendor AS exp_vendor, e.spent_on AS exp_date
           FROM receipts rc LEFT JOIN expenses e ON e.id = rc.expense_id
           ORDER BY rc.created_at DESC, rc.id DESC"""
    )
    linked = sum(1 for r in all_rows if r["expense_id"])
    if filter == "linked":
        rows = [r for r in all_rows if r["expense_id"]]
    elif filter == "unlinked":
        rows = [r for r in all_rows if not r["expense_id"]]
    else:
        rows = all_rows
    cards = [
        {"label": "Captured", "value": str(len(all_rows)), "tone": "muted", "sub": "receipt scans"},
        {"label": "Linked", "value": str(linked), "tone": "dark", "sub": "matched to an expense"},
        {
            "label": "Unlinked",
            "value": str(len(all_rows) - linked),
            "tone": "warn",
            "sub": "attach to an expense",
        },
    ]
    pills = [
        {"key": "all", "label": "All", "n": len(all_rows), "on": filter == "all"},
        {"key": "linked", "label": "Linked", "n": linked, "on": filter == "linked"},
        {
            "key": "unlinked",
            "label": "Unlinked",
            "n": len(all_rows) - linked,
            "on": filter == "unlinked",
        },
    ]
    # Scans accumulate for the life of the studio; the grid renders one page.
    # The cards and the filter pill counts still speak for the whole shoebox.
    offset = max(0, offset)
    page_size = 50
    cells = [
        {
            "id": r["id"],
            "filename": r["filename"],
            "is_pdf": (r["content_type"] or "").endswith("pdf")
            or r["filename"].lower().endswith(".pdf"),
            "linked": bool(r["expense_id"]),
            "meta": (
                f"{r['exp_vendor']} · {(r['exp_date'] or '')[:10]}"
                if r["expense_id"]
                else (r["created_at"] or "")[:10] + " · unlinked"
            ),
        }
        for r in rows[offset : offset + page_size]
    ]
    # open expenses to offer in the attach dropdown (newest first)
    exp_opts = [
        {
            "id": e["id"],
            "label": f"{(e['spent_on'] or '')[5:10]} · {e['vendor']} · {_usd(e['amount_cents'])}",
        }
        for e in db.all_(
            "SELECT id, spent_on, vendor, amount_cents FROM expenses "
            "ORDER BY spent_on DESC, id DESC LIMIT 200"
        )
    ]
    return templates.TemplateResponse(
        request,
        "admin/financials_receipts.html",
        {
            "active": "receipts",
            "cards": cards,
            "rows": cells,
            "expenses": exp_opts,
            "pills": pills,
            "filter": filter,
            "total": len(rows),
            "offset": offset,
            "page_size": page_size,
        },
    )


@router.post("/receipts")
async def receipt_upload(file: UploadFile = File(...), expense_id: int | None = Form(None)):
    """Streaming stays on the loop; the lookup, the mkdir and the INSERT do not.

    A phone-camera receipt is a few megabytes, and the database calls either side
    of it were blocking the loop for every other request. Extension checking runs
    here because it touches nothing but the filename.
    """
    name = Path(file.filename or "receipt").name
    ext = Path(name).suffix.lower()
    if ext not in _RECEIPT_EXTS:
        raise HTTPException(status_code=400, detail="receipt must be an image or PDF")
    stored = await run_in_threadpool(_receipt_preflight, expense_id, ext)
    size = await common.save_upload(file, config.RECEIPTS_DIR / stored)
    await run_in_threadpool(_record_receipt, name, stored, file.content_type, size, expense_id)
    return RedirectResponse("/admin/financials/receipts", status_code=303)


def _receipt_preflight(expense_id: int | None, ext: str) -> str:
    if expense_id and not db.one("SELECT id FROM expenses WHERE id=?", (expense_id,)):
        raise HTTPException(status_code=400, detail="unknown expense")
    config.RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    return f"{uuid.uuid4().hex}{ext}"


def _record_receipt(name, stored, content_type, size, expense_id) -> None:
    db.run(
        """INSERT INTO receipts (filename, stored, content_type, size_bytes, expense_id)
              VALUES (?,?,?,?,?)""",
        (name, stored, content_type, size, expense_id or None),
    )


@router.post("/receipts/{receipt_id}/delete")
def receipt_delete(receipt_id: int):
    rc = db.one("SELECT stored FROM receipts WHERE id=?", (receipt_id,))
    if rc:
        (config.RECEIPTS_DIR / rc["stored"]).unlink(missing_ok=True)
        db.run("DELETE FROM receipts WHERE id=?", (receipt_id,))
    return RedirectResponse("/admin/financials/receipts", status_code=303)


@router.get("/receipts/{receipt_id}/file")
def receipt_file(receipt_id: int):
    rc = db.one("SELECT * FROM receipts WHERE id=?", (receipt_id,))
    if not rc:
        raise HTTPException(status_code=404)
    path = config.RECEIPTS_DIR / rc["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type=rc["content_type"] or "application/octet-stream", filename=rc["filename"]
    )


@router.get("/mileage", response_class=HTMLResponse)
def mileage(request: Request, offset: int = 0):
    rows = db.all_("SELECT * FROM mileage ORDER BY drove_on DESC, id DESC")
    miles = sum(r["miles"] for r in rows)
    deduction = sum(round(r["miles"] * r["rate_cents"]) for r in rows)
    cards = [
        {
            "label": "Business miles",
            "value": f"{miles:,.0f}",
            "tone": "muted",
            "sub": f"{len(rows)} trips",
        },
        {
            "label": "Deduction",
            "value": _usd(deduction),
            "tone": "dark",
            "sub": f"at {config.MILEAGE_RATE_CENTS}¢/mi",
        },
        {
            "label": "Trips logged",
            "value": str(len(rows)),
            "tone": "ok",
            "sub": "all confirmed" if rows else "none yet",
        },
        {
            "label": "Est. tax saved",
            "value": _usd(round(deduction * 0.3)),
            "tone": "warn",
            "sub": "~30% bracket estimate",
        },
    ]
    # The IRS wants every trip kept, so the log only grows: render one page of it
    # while the cards and mileage.csv stay over every trip on file.
    offset = max(0, offset)
    page_size = 50
    table = [
        {
            "id": r["id"],
            "date": (r["drove_on"] or "")[5:10],
            "route": f"{r['from_place']} → {r['to_place']}",
            "purpose": r["purpose"] or "—",
            "miles": f"{r['miles']:,.1f}",
            "ded": _usd(round(r["miles"] * r["rate_cents"])),
        }
        for r in rows[offset : offset + page_size]
    ]
    return templates.TemplateResponse(
        request,
        "admin/financials_mileage.html",
        {
            "active": "mileage",
            "cards": cards,
            "rows": table,
            "count": len(rows),
            "today": studio._today().isoformat(),
            "rate_cents": config.MILEAGE_RATE_CENTS,
            "offset": offset,
            "page_size": page_size,
        },
    )


@router.post("/mileage")
def mileage_create(
    drove_on: str = Form(...),
    from_place: str = Form(...),
    to_place: str = Form(...),
    purpose: str = Form(""),
    miles: float = Form(...),
):
    from_place, to_place = from_place.strip(), to_place.strip()
    if not from_place or not to_place:
        raise HTTPException(status_code=400, detail="from and to required")
    # same string-ordered ledger as expenses: only ISO dates sort honestly
    try:
        drove_on = dt.date.fromisoformat(drove_on).isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date")
    if miles <= 0:
        raise HTTPException(status_code=400, detail="miles must be positive")
    db.run(
        """INSERT INTO mileage (drove_on, from_place, to_place, purpose, miles, rate_cents)
              VALUES (?,?,?,?,?,?)""",
        (drove_on, from_place, to_place, purpose.strip() or None, miles, config.MILEAGE_RATE_CENTS),
    )
    return RedirectResponse("/admin/financials/mileage", status_code=303)


@router.post("/mileage/{trip_id}/delete")
def mileage_delete(trip_id: int):
    db.run("DELETE FROM mileage WHERE id=?", (trip_id,))
    return RedirectResponse("/admin/financials/mileage", status_code=303)


@router.get("/mileage.csv", response_class=PlainTextResponse)
def mileage_csv():
    rows = db.all_("SELECT * FROM mileage ORDER BY drove_on DESC, id DESC")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "from", "to", "purpose", "miles", "rate_usd", "deduction_usd"])
    for r in rows:
        w.writerow(
            [
                r["drove_on"],
                r["from_place"] or "",
                r["to_place"] or "",
                r["purpose"] or "",
                f"{r['miles']:.1f}",
                f"{r['rate_cents'] / 100:.2f}",
                f"{round(r['miles'] * r['rate_cents']) / 100:.2f}",
            ]
        )
    return buf.getvalue()
