"""Licenses — usage-rights records (the F&B moat).

Every mutation (create / update / status change / soft-delete) writes through
db.tx() so the row change and its audit_log entry commit together. audit.log is
the only write path to audit_log; nothing here UPDATEs or DELETEs a license row
(soft-delete sets deleted_at). 'holder' = who licensed it; coverage_scope +
license_clients model who else the grant reaches (hierarchy-ready for Domain A).
"""

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import audit, clients, config, db, pricing, security
from ..render import templates
from ..usage_vocab import CHANNELS
from .studio import get_client

log = logging.getLogger("mise.admin.licenses")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

# Days-out at which an active, dated license starts flagging "expiring soon".
# Single source of truth — the list strip and the detail page both read it
# through expiry_cue() so the two surfaces never disagree.
EXPIRY_WARN_DAYS = 45

USAGE_TIERS = ["standard", "extended", "exclusive", "unpublished_commercial"]
EXCLUSIVITY = ["non_exclusive", "exclusive"]
COVERAGE_SCOPES = ["holder_only", "holder_and_descendants", "specific"]
STATUSES = ["draft", "active", "expired", "renewed", "terminated"]
# CHANNELS (the F&B usage-channel vocab) now lives in app/usage_vocab so Domain H
# (press) shares the exact list — imported above, re-exported here unchanged.
TERRITORIES = ["worldwide", "US", "north_america", "EU", "UK", "local_metro"]

# Columns the diff/audit machinery tracks. Order = form/display order.
_FIELDS = [
    "title",
    "scope",
    "coverage_scope",
    "usage_tier",
    "exclusivity",
    "territory",
    "channels",
    "published",
    "fee_cents",
    "starts_on",
    "ends_on",
    "perpetual",
    "notes",
    "project_id",
    "gallery_id",
]


def get_license(license_id: int) -> "db.sqlite3.Row":
    return db.get_or_404(
        """SELECT l.*, c.name AS holder_name, c.company AS holder_company,
                  CAST(julianday(l.ends_on) - julianday(date('now', 'localtime')) AS INTEGER) AS days_left
                  FROM licenses l JOIN clients c ON c.id=l.holder_client_id
                  WHERE l.id=? AND l.deleted_at IS NULL""",
        (license_id,),
    )


def expiry_cue(row) -> dict | None:
    """Display-only urgency cue for a license end date. Reads status, perpetual,
    and days_left (INTEGER julianday(ends_on) - today). Returns None when there is
    nothing to flag — perpetual, undated, not active, or further out than the
    threshold. Otherwise {"state": "lapsed"|"expiring", "days": <abs days>}.
    Both the list strip and the detail page call this so they always agree."""
    if row["status"] != "active" or row["perpetual"] or row["days_left"] is None:
        return None
    dl = row["days_left"]
    if dl < 0:
        return {"state": "lapsed", "days": -dl}
    if dl <= EXPIRY_WARN_DAYS:
        return {"state": "expiring", "days": dl}
    return None


def effective_coverage(row) -> list[int]:
    """Resolve which client ids a license actually reaches, holder first. The
    holder is always covered. holder_and_descendants walks the Domain A client
    tree (clients.descendant_ids, top-down); specific adds the explicitly listed
    license_clients; holder_only is just the holder. Reads coverage_scope,
    holder_client_id, id off the license row."""
    holder = row["holder_client_id"]
    if row["coverage_scope"] == "holder_and_descendants":
        return [holder] + clients.descendant_ids(holder)
    if row["coverage_scope"] == "specific":
        extra = [
            r["client_id"]
            for r in db.all_(
                "SELECT client_id FROM license_clients WHERE license_id=? ORDER BY client_id",
                (row["id"],),
            )
        ]
        return [holder] + [c for c in extra if c != holder]
    return [holder]


def licenses_covering(client_id: int) -> list[dict]:
    """The bottom-up inverse of effective_coverage: licenses that REACH this
    client without it being the holder. Two arms, each row tagged with `rel` +
    the holder's name for display:
      group    — a holder_and_descendants license held by an ANCESTOR cascades
                 down (clients.ancestor_ids → the Domain A walk, upside down)
      specific — a 'specific' license that explicitly lists this client in
                 license_clients (and is held by someone else)
    Holder-held licenses are shown separately on the client page, so the holder
    is excluded from the 'specific' arm. Soft-deleted licenses are skipped."""
    out: list[dict] = []
    ancestors = clients.ancestor_ids(client_id)
    if ancestors:
        ph = ",".join("?" * len(ancestors))  # only ever "?,?,..." placeholders
        rows = db.all_(
            "SELECT l.id, l.title, l.usage_tier, l.exclusivity, l.status, l.published,"
            "       l.holder_client_id, h.name AS holder_name"
            "  FROM licenses l JOIN clients h ON h.id=l.holder_client_id"
            " WHERE l.coverage_scope='holder_and_descendants'"
            f"   AND l.holder_client_id IN ({ph}) AND l.deleted_at IS NULL"
            " ORDER BY l.status, l.title",
            tuple(ancestors),
        )
        out += [{**{k: r[k] for k in r.keys()}, "rel": "group"} for r in rows]
    rows = db.all_(
        "SELECT l.id, l.title, l.usage_tier, l.exclusivity, l.status, l.published,"
        "       l.holder_client_id, h.name AS holder_name"
        "  FROM licenses l JOIN license_clients lc ON lc.license_id=l.id"
        "       JOIN clients h ON h.id=l.holder_client_id"
        " WHERE lc.client_id=? AND l.coverage_scope='specific'"
        "   AND l.holder_client_id<>? AND l.deleted_at IS NULL"
        " ORDER BY l.status, l.title",
        (client_id, client_id),
    )
    out += [{**{k: r[k] for k in r.keys()}, "rel": "specific"} for r in rows]
    return out


def _parse_form(form) -> dict:
    """Pull license fields out of a submitted form into a normalized dict.
    Multi-value territory/channels arrive as repeated checkboxes → JSON arrays."""

    def cents(key: str) -> int:
        try:
            return round(float(form.get(key) or "0") * 100)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad amount: {key}")

    coverage = form.get("coverage_scope") or "holder_only"
    if coverage not in COVERAGE_SCOPES:
        raise HTTPException(status_code=400, detail="bad coverage_scope")
    tier = form.get("usage_tier") or "standard"
    if tier not in USAGE_TIERS:
        raise HTTPException(status_code=400, detail="bad usage_tier")
    excl = form.get("exclusivity") or "non_exclusive"
    if excl not in EXCLUSIVITY:
        raise HTTPException(status_code=400, detail="bad exclusivity")
    territory = [t for t in form.getlist("territory") if t in TERRITORIES]
    channels = [c for c in form.getlist("channels") if c in CHANNELS]

    def fk(key: str):
        v = (form.get(key) or "").strip()
        return int(v) if v.isdigit() else None

    return {
        "title": (form.get("title") or "").strip(),
        "scope": (form.get("scope") or "").strip(),
        "coverage_scope": coverage,
        "usage_tier": tier,
        "exclusivity": excl,
        "territory": json.dumps(territory),
        "channels": json.dumps(channels),
        "published": 1 if form.get("published") else 0,
        "fee_cents": cents("fee"),
        "starts_on": (form.get("starts_on") or "").strip() or None,
        "ends_on": (form.get("ends_on") or "").strip() or None,
        "perpetual": 1 if form.get("perpetual") else 0,
        "notes": (form.get("notes") or "").strip() or None,
        "project_id": fk("project_id"),
        "gallery_id": fk("gallery_id"),
    }


@router.post("/clients/{client_id}/licenses")
async def create_license(client_id: int, title: str = Form(...)):
    get_client(client_id)
    if not title.strip():
        raise HTTPException(status_code=400, detail="title required")
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO licenses (holder_client_id, title) VALUES (?,?)",
            (client_id, title.strip()),
        )
        lid = cur.lastrowid
        audit.log(
            con,
            "license",
            lid,
            "create",
            diff={"holder_client_id": client_id, "title": title.strip()},
        )
    log.info("license %s created (holder client %s)", lid, client_id)
    return RedirectResponse(f"/admin/studio/licenses/{lid}", status_code=303)


@router.get("/licenses", response_class=HTMLResponse)
async def licenses_list(request: Request):
    rows = db.all_(
        """SELECT l.id, l.title, l.usage_tier, l.exclusivity, l.status,
                  l.published, l.fee_cents, l.starts_on, l.ends_on, l.perpetual,
                  l.coverage_scope,
                  c.name AS holder_name, c.company AS holder_company,
                  CAST(julianday(l.ends_on) - julianday(date('now', 'localtime')) AS INTEGER) AS days_left
           FROM licenses l JOIN clients c ON c.id=l.holder_client_id
           WHERE l.deleted_at IS NULL
           ORDER BY CASE l.status WHEN 'active' THEN 0 WHEN 'draft' THEN 1
                                  WHEN 'renewed' THEN 2 ELSE 3 END,
                    l.ends_on IS NULL, l.ends_on"""
    )
    # Expiring/expired surfacing via the shared cue: active, dated, not perpetual,
    # within the threshold (or already past). Silent when empty.
    expiring = [r for r in rows if expiry_cue(r)]
    return templates.TemplateResponse(
        request, "admin/licenses.html", {"licenses": rows, "expiring": expiring}
    )


@router.get("/licenses/{license_id}", response_class=HTMLResponse)
async def license_detail(request: Request, license_id: int):
    # Deferred import breaks the licenses<->press cycle: press.py imports
    # effective_coverage from this module at load time, so this module can't
    # import press at the top. By request time both are fully loaded.
    from .press import press_for_license

    d = get_license(license_id)
    covered = db.all_(
        """SELECT c.id, c.name, c.company FROM license_clients lc
           JOIN clients c ON c.id=lc.client_id WHERE lc.license_id=?
           ORDER BY c.name""",
        (license_id,),
    )
    all_clients = db.clients_for_select()
    # Effective coverage resolved through the Domain A tree (holder first), so a
    # holder_and_descendants grant's real reach is visible, not just the
    # 'specific' license_clients list.
    _by_id = {c["id"]: c for c in all_clients}
    coverage = [_by_id[i] for i in effective_coverage(d) if i in _by_id]
    projects = db.all_(
        """SELECT id, title FROM projects WHERE client_id=? ORDER BY created_at DESC""",
        (d["holder_client_id"],),
    )
    galleries = db.all_(
        """SELECT id, title FROM galleries WHERE client_id=? ORDER BY created_at DESC""",
        (d["holder_client_id"],),
    )
    trail = db.all_(
        """SELECT action, actor, diff_json, created_at FROM audit_log
           WHERE entity_type='license' AND entity_id=?
           ORDER BY id DESC LIMIT 50""",
        (license_id,),
    )
    # Price the suggestion in the holder client's home market (Domain B). The
    # multipliers are market-independent; only the base rate card changes.
    holder = db.one("SELECT market FROM clients WHERE id=?", (d["holder_client_id"],))
    return templates.TemplateResponse(
        request,
        "admin/license.html",
        {
            "d": d,
            "cue": expiry_cue(d),
            "press": press_for_license(d),
            "covered": covered,
            "covered_ids": {c["id"] for c in covered},
            "coverage": coverage,
            "suggested": pricing.suggest_license_fee(d, market=holder["market"]),
            "all_clients": all_clients,
            "projects": projects,
            "galleries": galleries,
            "trail": [
                {**dict(t), "diff": json.loads(t["diff_json"]) if t["diff_json"] else None}
                for t in trail
            ],
            "usage_tiers": USAGE_TIERS,
            "exclusivity_opts": EXCLUSIVITY,
            "coverage_scopes": COVERAGE_SCOPES,
            "statuses": STATUSES,
            "channels_vocab": CHANNELS,
            "territories_vocab": TERRITORIES,
            "sel_territory": set(json.loads(d["territory"] or "[]")),
            "sel_channels": set(json.loads(d["channels"] or "[]")),
            "base_url": config.BASE_URL,
        },
    )


@router.post("/licenses/{license_id}")
async def update_license(request: Request, license_id: int):
    d = get_license(license_id)
    form = await request.form()
    new = _parse_form(form)
    if not new["title"]:
        raise HTTPException(status_code=400, detail="title required")
    diff = {f: [d[f], new[f]] for f in _FIELDS if (d[f] or None) != (new[f] or None)}
    # Covered clients only meaningful for 'specific'; otherwise we clear the set.
    want_cover = (
        {int(x) for x in form.getlist("cover_client_ids") if x.isdigit()}
        if new["coverage_scope"] == "specific"
        else set()
    )
    have_cover = {
        r["client_id"]
        for r in db.all_("SELECT client_id FROM license_clients WHERE license_id=?", (license_id,))
    }
    if not diff and want_cover == have_cover:
        return RedirectResponse(f"/admin/studio/licenses/{license_id}", status_code=303)
    with db.tx() as con:
        con.execute(
            """UPDATE licenses SET title=?, scope=?, coverage_scope=?, usage_tier=?,
               exclusivity=?, territory=?, channels=?, published=?, fee_cents=?,
               starts_on=?, ends_on=?, perpetual=?, notes=?, project_id=?, gallery_id=?,
               updated_at=datetime('now') WHERE id=?""",
            (
                new["title"],
                new["scope"],
                new["coverage_scope"],
                new["usage_tier"],
                new["exclusivity"],
                new["territory"],
                new["channels"],
                new["published"],
                new["fee_cents"],
                new["starts_on"],
                new["ends_on"],
                new["perpetual"],
                new["notes"],
                new["project_id"],
                new["gallery_id"],
                license_id,
            ),
        )
        if want_cover != have_cover:
            con.execute("DELETE FROM license_clients WHERE license_id=?", (license_id,))
            for cid in want_cover:
                con.execute(
                    "INSERT INTO license_clients (license_id, client_id) VALUES (?,?)",
                    (license_id, cid),
                )
            diff["covered_clients"] = [sorted(have_cover), sorted(want_cover)]
        audit.log(con, "license", license_id, "update", diff=diff)
    log.info("license %s updated (%d fields)", license_id, len(diff))
    return RedirectResponse(f"/admin/studio/licenses/{license_id}", status_code=303)


@router.post("/licenses/{license_id}/status")
async def change_status(license_id: int, status: str = Form(...)):
    d = get_license(license_id)
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="bad status")
    if status == d["status"]:
        return RedirectResponse(f"/admin/studio/licenses/{license_id}", status_code=303)
    with db.tx() as con:
        con.execute(
            "UPDATE licenses SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, license_id),
        )
        audit.log(
            con, "license", license_id, "status_change", diff={"status": [d["status"], status]}
        )
    log.info("license %s status %s -> %s", license_id, d["status"], status)
    return RedirectResponse(f"/admin/studio/licenses/{license_id}", status_code=303)


@router.post("/licenses/{license_id}/delete")
async def delete_license(license_id: int):
    d = get_license(license_id)
    with db.tx() as con:
        con.execute("UPDATE licenses SET deleted_at=datetime('now') WHERE id=?", (license_id,))
        audit.log(
            con,
            "license",
            license_id,
            "soft_delete",
            diff={"title": d["title"], "holder_client_id": d["holder_client_id"]},
        )
    log.info("license %s soft-deleted", license_id)
    return RedirectResponse(f"/admin/studio/clients/{d['holder_client_id']}", status_code=303)
