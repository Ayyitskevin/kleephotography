"""SQLite access — WAL mode, short-lived connections (safe across job threads)."""

import datetime as dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

from . import config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def migrate() -> None:
    config.ensure_dirs()
    con = connect()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                       name TEXT PRIMARY KEY,
                       applied_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        applied = {r["name"] for r in con.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            con.executescript(path.read_text())
            con.execute("INSERT INTO schema_migrations (name) VALUES (?)", (path.name,))
            con.commit()
    finally:
        con.close()


def ident(name: str, allowed) -> str:
    """Gate a SQL identifier (table/column) that gets interpolated into a query
    string. Values always go through `?` placeholders; identifiers can't, so any
    interpolated name must be checked against an allowlist HERE, at the point of
    use. Raises if `name` isn't allowed — a careless edit fails loud instead of
    becoming injection (R12)."""
    if name not in allowed:
        raise ValueError(f"disallowed SQL identifier: {name!r}")
    return name


def one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    con = connect()
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


def all_(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    con = connect()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def run(sql: str, params: tuple = ()) -> int:
    """Execute and commit; returns lastrowid."""
    con = connect()
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def get_or_404(sql: str, params: tuple = (), *, detail: str = "Not found") -> sqlite3.Row:
    """Convenience wrapper: one() + 404 if missing.

    Reduces the repeated get_* + 404 boilerplate across admin modules.
    Use for simple ID lookups; complex JOIN queries can stay in place or use
    this with their full SELECT.
    """
    row = one(sql, params)
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


def clients_for_select() -> list[sqlite3.Row]:
    """Lightweight list for admin <select> dropdowns (id, name, company)."""
    return all_("SELECT id, name, company FROM clients ORDER BY name")


def date_window_labels(today: dt.date, window: int) -> list[str]:
    """ISO date labels for the trailing `window` days ending today (oldest first)."""
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(window - 1, -1, -1)]


def spark_series(table: str, today: dt.date, window: int) -> tuple[list[int], int]:
    """Daily counts for the `window` days ending `today` (reused from studio).

    `table` must be allowlisted by caller (use ident if interpolated).
    Returns (series_list aligned to date_window_labels, total).
    """
    start = (today - dt.timedelta(days=window - 1)).isoformat()
    labels = date_window_labels(today, window)
    rows = all_(
        f"""SELECT date(created_at, 'localtime') AS d, COUNT(*) AS n
                       FROM {table}
                       WHERE date(created_at, 'localtime') >= ?
                       GROUP BY date(created_at, 'localtime')""",
        (start,),
    )
    b = {r["d"]: r["n"] for r in rows}
    series = [b.get(d, 0) for d in labels]
    return series, sum(series)


@contextmanager
def tx():
    """Atomic unit of work: commit on clean exit, rollback on exception.

    Use when multiple writes must land together (e.g. a soft-delete and its
    audit_log row). The caller runs statements on the yielded connection.
    """
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
