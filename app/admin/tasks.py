"""Admin tasks board + month calendar (split from activity.py)."""

import calendar as cal
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, security
from ..render import templates
from . import studio as studio_mod

log = logging.getLogger("mise.admin.tasks")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

# ---- Tasks (HoneyBook "Tasks" parity, Phase 3) -----------------------------


def _task_due_label(due: str | None, today: dt.date) -> tuple[str, bool]:
    """Return (label, urgent) for a task's due date relative to today.
    Urgent (overdue or due today) drives the clay due-text color in the board."""
    if not due:
        return "", False
    try:
        dd = dt.date.fromisoformat(due[:10])
    except (ValueError, TypeError):
        return "", False
    delta = (dd - today).days
    if delta < 0:
        n = -delta
        return (f"Overdue {n}d" if n <= 9 else "Overdue"), True
    if delta == 0:
        return "Today", True
    if delta == 1:
        return "Tomorrow", False
    if delta <= 6:
        return dd.strftime("%a"), False
    return dd.strftime("%b %-d"), False


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_view(request: Request):
    """Studio to-do board (strict-1:1 prototype): three columns — Today (due
    today or overdue), This week (every other open task), Done (recently
    completed). Each card toggles done via a POST form; due_date feeds the
    calendar."""
    today = studio_mod._today()

    def card(r) -> dict:
        label, urgent = _task_due_label(r["due_date"], today)
        return {
            "id": r["id"],
            "title": r["title"],
            "project": r["project_title"] or "General",
            "project_id": r["project_id"],
            "due": label,
            "urgent": urgent,
        }

    open_rows = db.all_(
        """SELECT t.id, t.title, t.due_date, t.project_id, p.title AS project_title
           FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
           WHERE t.done=0
           ORDER BY (t.due_date IS NULL), t.due_date ASC, t.id DESC"""
    )
    today_iso = today.isoformat()
    today_col, week_col = [], []
    for r in open_rows:
        due = r["due_date"]
        if due and due[:10] <= today_iso:  # overdue or due today
            today_col.append(card(r))
        else:
            week_col.append(card(r))

    done_rows = db.all_(
        """SELECT t.id, t.title, t.due_date, t.done_at, t.project_id,
                  p.title AS project_title
           FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
           WHERE t.done=1 ORDER BY t.done_at DESC LIMIT 12"""
    )
    done_col = []
    for r in done_rows:
        c = card(r)
        c["due"] = ("done " + r["done_at"][:10]) if r["done_at"] else "done"
        c["urgent"] = False
        done_col.append(c)

    week_ago = (today - dt.timedelta(days=7)).isoformat()
    done_week = db.one("SELECT COUNT(*) n FROM tasks WHERE done=1 AND done_at >= ?", (week_ago,))[
        "n"
    ]

    columns = [
        {"key": "today", "label": "Today", "dot": "#7C2F38", "tasks": today_col},
        {"key": "week", "label": "This week", "dot": "#EDB23C", "tasks": week_col},
        {"key": "done", "label": "Done", "dot": "#2f7d57", "tasks": done_col},
    ]
    projects = db.all_(
        """SELECT id, title FROM projects WHERE status != 'archived'
           ORDER BY title"""
    )
    return templates.TemplateResponse(
        request,
        "admin/tasks.html",
        {
            "columns": columns,
            "open_count": len(today_col) + len(week_col),
            "done_week": done_week,
            "projects": projects,
        },
    )


@router.post("/tasks")
async def task_create(title: str = Form(...), due_date: str = Form(""), project_id: str = Form("")):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    due = due_date.strip() or None
    pid = int(project_id) if project_id.strip() else None
    if pid is not None and not db.one("SELECT 1 FROM projects WHERE id=?", (pid,)):
        raise HTTPException(status_code=400, detail="bad project")
    db.run("INSERT INTO tasks (title, due_date, project_id) VALUES (?, ?, ?)", (title, due, pid))
    log.info("task created: %s (due %s, project %s)", title, due, pid)
    return RedirectResponse("/admin/tasks", status_code=303)


@router.post("/tasks/{task_id}/toggle")
async def task_toggle(task_id: int):
    t = db.one("SELECT done FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise HTTPException(status_code=404, detail="no such task")
    if t["done"]:
        db.run("UPDATE tasks SET done=0, done_at=NULL WHERE id=?", (task_id,))
    else:
        db.run("UPDATE tasks SET done=1, done_at=datetime('now') WHERE id=?", (task_id,))
    log.info("task %s toggled -> done=%s", task_id, 0 if t["done"] else 1)
    return RedirectResponse("/admin/tasks", status_code=303)


@router.post("/tasks/{task_id}/delete")
async def task_delete(task_id: int):
    if not db.one("SELECT 1 FROM tasks WHERE id=?", (task_id,)):
        raise HTTPException(status_code=404, detail="no such task")
    db.run("DELETE FROM tasks WHERE id=?", (task_id,))
    log.info("task %s deleted", task_id)
    return RedirectResponse("/admin/tasks", status_code=303)


# ---- Calendar (month grid: shoots + task due dates + invoice due dates) -----

# Three-bucket palette matching the prototype legend (Shoot / Call·delivery / Money).
# Dark-panel tints (editorial-dark, Revamp PR-G) — these feed the .cal-event
# inline style directly, so they can't be reached by CSS; match the same
# clay/ok/honey status tokens the rest of the admin shell uses.
_CAL_BUCKET = {
    "shoot": ("#d98a78", "#2e1a18"),
    "call": ("#9cc178", "#20271a"),
    "money": ("#d8a857", "#2b2413"),
}


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_view(request: Request, year: int = 0, month: int = 0):
    """Month grid overlaying three real date sources, bucketed to the prototype's
    legend: shoots (clay), confirmed consults (green), invoices due (gold).
    Read-only — each cell entry links to the project/booking/invoice it represents."""
    today = studio_mod._today()
    if not (1 <= month <= 12) or year < 1970:
        year, month = today.year, today.month
    first = dt.date(year, month, 1)
    last = dt.date(year, month, cal.monthrange(year, month)[1])
    lo, hi = first.isoformat(), last.isoformat()

    events: dict[int, list[dict]] = {}

    def add(day_iso: str, bucket: str, label: str, url: str):
        try:
            day_d = dt.date.fromisoformat(day_iso[:10])
        except (ValueError, TypeError):
            return
        color, bg = _CAL_BUCKET[bucket]
        events.setdefault(day_d.day, []).append(
            {"label": label, "url": url, "color": color, "bg": bg}
        )

    for r in db.all_(
        """SELECT p.id, p.title, p.shoot_date, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived' AND p.shoot_date IS NOT NULL
             AND p.shoot_date BETWEEN ? AND ?""",
        (lo, hi),
    ):
        who = r["company"] or r["client_name"]
        add(
            r["shoot_date"],
            "shoot",
            f"{r['title']} · {who}" if who else r["title"],
            f"/admin/studio/projects/{r['id']}",
        )
    for r in db.all_(
        """SELECT i.id, i.title, i.due_date, c.name AS client_name, c.company
           FROM invoices i JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
             AND i.due_date IS NOT NULL AND i.due_date BETWEEN ? AND ?""",
        (lo, hi),
    ):
        who = r["company"] or r["client_name"]
        add(
            r["due_date"],
            "money",
            f"{r['title']} · {who}" if who else r["title"],
            f"/admin/studio/invoices/{r['id']}",
        )
    # Confirmed consultations/bookings — start_utc is UTC; show on Kevin's local
    # day. zoneinfo handles DST. Bookings link to the bookings console.
    tz = ZoneInfo(config.TIMEZONE)
    for r in db.all_(
        """SELECT b.id, b.name, b.start_utc, e.name AS event_name FROM bookings b
           JOIN event_types e ON e.id=b.event_type_id
           WHERE b.status='confirmed' AND b.start_utc IS NOT NULL"""
    ):
        try:
            local = dt.datetime.fromisoformat(r["start_utc"]).replace(tzinfo=dt.UTC).astimezone(tz)
        except (ValueError, TypeError):
            continue
        if not (lo <= local.date().isoformat() <= hi):
            continue
        add(
            local.date().isoformat(),
            "call",
            f"{local:%H:%M} {r['event_name']} · {r['name']}",
            "/admin/scheduling/bookings",
        )

    # Sunday-first month grid of cells (prototype layout): leading/trailing blanks
    # so the grid is a whole number of weeks.
    weeks = cal.Calendar(firstweekday=6).monthdayscalendar(year, month)
    cells: list[dict] = []
    for week in weeks:
        for day in week:
            if day == 0:
                cells.append({"empty": True, "day": "", "today": False, "events": []})
            else:
                cells.append(
                    {
                        "empty": False,
                        "day": day,
                        "today": (day == today.day and month == today.month and year == today.year),
                        "events": events.get(day, []),
                    }
                )

    prev_m = first - dt.timedelta(days=1)
    next_m = last + dt.timedelta(days=1)
    return templates.TemplateResponse(
        request,
        "admin/calendar.html",
        {
            "year": year,
            "month": month,
            "month_name": first.strftime("%B"),
            "cells": cells,
            "today": today,
            "dow": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            "prev_year": prev_m.year,
            "prev_month": prev_m.month,
            "next_year": next_m.year,
            "next_month": next_m.month,
        },
    )
