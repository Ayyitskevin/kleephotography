"""Audit log — read-only view over the append-only audit_log table.

Every money & delivery mutation writes one audit_log row through audit.log()
(the single INSERT-only write path). This page renders those rows newest-first,
grouped into four human categories for filtering, plus a CSV export for disputes
/ chargebacks / an accountant. Nothing here writes — it only reads the trail.
"""

import csv
import io
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import db, security
from ..render import _localtime, templates

log = logging.getLogger("mise.admin.audit")
router = APIRouter(prefix="/admin/audit",
                   dependencies=[Depends(security.require_admin)])

_LIMIT = 500  # newest N events — this is a trail viewer, not a full dump

# entity_type -> one of the four prototype categories. Unknown types fall back
# to "document" so a newly-audited entity still appears (under All + Documents)
# rather than vanishing.
_CATEGORY = {
    "invoice": "money", "recurring_plan": "money", "payment": "money",
    "proposal": "document", "contract": "document",
    "license": "document", "press": "document",
    "gallery": "gallery", "shot_list": "gallery",
    "crop_preset": "gallery", "video_comment": "gallery",
    "availability": "auth", "date_override": "auth",
    "google_calendar": "auth", "event_type": "auth",
}

# label/icon/colors lifted 1:1 from the Admin Audit prototype DCLogic.cat map.
_CAT_META = {
    "money":    {"label": "Money",     "icon": "$", "bg": "#e1f2e9", "color": "#2f7d57"},
    "document": {"label": "Documents", "icon": "✎", "bg": "#ddeef0", "color": "#2f6d8a"},
    "gallery":  {"label": "Galleries", "icon": "▦", "bg": "#f7ecd2", "color": "#9a7a2c"},
    "auth":     {"label": "Access",    "icon": "⚷", "bg": "#f3e3e5", "color": "#7C2F38"},
}
_FILTERS = ["all", "money", "document", "gallery", "auth"]

_ACTION_PAST = {"create": "created", "update": "updated",
                "soft_delete": "deleted", "delete": "deleted"}
_CENTS_KEYS = ("total_cents", "fee_cents", "deposit_cents", "amount_cents")


def _category(entity_type: str) -> str:
    return _CATEGORY.get(entity_type, "document")


def _title(entity_type: str, action: str) -> str:
    entity = entity_type.replace("_", " ").capitalize()
    verb = _ACTION_PAST.get(action, action.replace("_", " "))
    return f"{entity} {verb}"


def _amount(diff: dict | None) -> str:
    """Surface a dollar figure when the diff carries a cents field — create diffs
    store a scalar, update diffs store [old, new]; we show the new value."""
    if not isinstance(diff, dict):
        return ""
    for k in _CENTS_KEYS:
        if k in diff:
            v = diff[k]
            if isinstance(v, list):  # [old, new]
                v = v[-1] if v else None
            if isinstance(v, (int, float)):
                return f"${v / 100:,.2f}"
    return ""


def _detail(entity_type: str, entity_id, action: str, diff: dict | None) -> str:
    base = f"{entity_type.replace('_', ' ')} #{entity_id}"
    if action == "update" and isinstance(diff, dict) and diff:
        return f"{base} · changed: {', '.join(diff.keys())}"
    return base


def _rows(category: str) -> list[dict]:
    import json
    raw = db.all_(
        """SELECT entity_type, entity_id, action, actor, diff_json, created_at
           FROM audit_log ORDER BY created_at DESC, id DESC LIMIT ?""", (_LIMIT,))
    out = []
    for r in raw:
        cat = _category(r["entity_type"])
        if category != "all" and cat != category:
            continue
        try:
            diff = json.loads(r["diff_json"]) if r["diff_json"] else None
        except (ValueError, TypeError):
            diff = None
        meta = _CAT_META[cat]
        out.append({
            "title": _title(r["entity_type"], r["action"]),
            "detail": _detail(r["entity_type"], r["entity_id"], r["action"], diff),
            "amount": _amount(diff),
            "created_at": r["created_at"],
            "actor": r["actor"] or "admin",
            "icon": meta["icon"], "icon_bg": meta["bg"], "icon_color": meta["color"],
        })
    return out


def _counts() -> dict:
    """Per-category counts for the filter chips, computed over the same window the
    list shows (LIMIT _LIMIT) so a chip number always matches what All renders."""
    raw = db.all_(
        "SELECT entity_type FROM audit_log ORDER BY created_at DESC, id DESC LIMIT ?",
        (_LIMIT,))
    counts = {"all": len(raw), "money": 0, "document": 0, "gallery": 0, "auth": 0}
    for r in raw:
        counts[_category(r["entity_type"])] += 1
    return counts


@router.get("", response_class=HTMLResponse)
async def audit_log_view(request: Request, cat: str = "all"):
    if cat not in _FILTERS:
        cat = "all"
    counts = _counts()
    filters = [{"key": k, "label": "All" if k == "all" else _CAT_META[k]["label"],
                "n": counts[k], "active": k == cat} for k in _FILTERS]
    return templates.TemplateResponse(request, "admin/audit.html", {
        "events": _rows(cat), "filters": filters, "cat": cat,
        "total": counts["all"],
    })


@router.get(".csv", response_class=PlainTextResponse)
async def audit_csv():
    """Full window as CSV — append-only evidence for a dispute or accountant."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Time", "Event", "Detail", "Amount", "Actor"])
    for e in _rows("all"):
        w.writerow([_localtime(e["created_at"]), e["title"], e["detail"],
                    e["amount"], e["actor"]])
    return PlainTextResponse(buf.getvalue(), headers={
        "Content-Disposition": 'attachment; filename="kleephotography_audit_log.csv"'})
