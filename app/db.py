"""SQLite access — WAL mode, short-lived connections (safe across job threads)."""

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

from . import config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Apply order is lexicographic on the full filename (see migrate()). New files
# must use a unique NNN_ prefix — never reuse 054/055 (those collisions already
# exist on disk; do not renumber them on a live DB). Policy: ops/MIGRATIONS.md.
MIGRATION_ALIASES = {
    # An earlier host briefly applied the Plutus gallery columns under this later filename.
    # Treat both names as equivalent so a clean GitHub deploy does not re-run
    # the same ALTER TABLE statements against production.
    "055_plutus_upsell.sql": {"058_plutus_upsell.sql"},
    "058_plutus_upsell.sql": {"055_plutus_upsell.sql"},
}
# Leading whitespace/comments, skipped when classifying a statement.
_STMT_NOISE = re.compile(r"\A(?:\s+|--[^\n]*|/\*.*?\*/)+", re.DOTALL)
# Transaction control a migration file carries itself. The runner owns the
# transaction (see _apply), so these are stripped rather than obeyed. ROLLBACK
# is NOT here on purpose — it asks for the opposite and _apply rejects it.
_TX_CONTROL = frozenset({"BEGIN", "COMMIT", "END"})
# Pragmas a migration file may toggle around its statements. These run OUTSIDE
# the transaction, on the one connection every later file in the same run shares,
# so the runner must be able to put each one back afterwards — which needs a
# pragma whose current value reads back by name with no side effect of its own
# (`PRAGMA optimize`, `PRAGMA wal_checkpoint` and friends do work when read).
# 031's FK toggle is the only user today; anything else fails loud in _apply
# rather than silently leaking into the next migration.
_RESTORABLE_PRAGMAS = frozenset({"foreign_keys"})
_PRAGMA_NAME = re.compile(r"\APRAGMA\s+(\w+)", re.IGNORECASE)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _split_statements(sql: str) -> list[str]:
    """Split migration SQL into individual statements.

    `sqlite3.complete_statement` wraps SQLite's own tokenizer, so a semicolon
    inside a string literal or a comment does not end a statement, and a
    `CREATE TRIGGER` body stays whole until its `END;` (no migration has a
    trigger today; splitting stays correct if one ever lands). A semicolon is
    the only token that can complete a statement, so that is the only place
    worth testing.
    """
    statements: list[str] = []
    start = 0
    for i, ch in enumerate(sql):
        if ch == ";" and sqlite3.complete_statement(sql[start : i + 1]):
            statements.append(sql[start : i + 1])
            start = i + 1
    if sql[start:].strip():
        statements.append(sql[start:])
    return statements


def _statement_keyword(stmt: str) -> str:
    """First SQL keyword of a statement, uppercased ('' if only comments)."""
    body = _STMT_NOISE.sub("", stmt)
    return body.split(maxsplit=1)[0].upper().rstrip(";") if body.strip() else ""


def _pragma_name(stmt: str) -> str:
    """Name of the pragma a PRAGMA statement reads or sets, lowercased."""
    match = _PRAGMA_NAME.match(_STMT_NOISE.sub("", stmt))
    return match.group(1).lower() if match else ""


def _apply(con: sqlite3.Connection, path: Path) -> None:
    """Run one migration file and record it, atomically.

    Everything the file does lands in ONE transaction together with its
    `schema_migrations` row: a failure part-way through rolls the whole file
    back, and a crash right after the last statement cannot lose the tracking
    row. `executescript` could do neither — it commits first and then lets each
    statement autocommit, so a half-applied file re-ran from the top on the next
    boot and died on a duplicate column (the app then restart-loops).

    Three kinds of statement cannot live inside that transaction:

    * the file's own `BEGIN`/`COMMIT`/`END` — obeying it would close the runner's
      transaction before the tracking row is written, which is the bug being
      fixed, so the runner strips them and owns the transaction itself;
    * `ROLLBACK` — the runner cannot honor it either, but stripping it would mean
      committing work the file explicitly asked to discard, and executing it would
      end the transaction mid-file and let every later statement autocommit alone.
      Both are worse than stopping, so it is rejected before anything runs;
    * `PRAGMA` — SQLite silently ignores `PRAGMA foreign_keys` inside a
      transaction, and 031 needs FK enforcement genuinely off while it rebuilds
      `projects`. Leading pragmas run before the transaction opens and trailing
      ones after it commits — exactly where 031 already puts them, so its
      behavior is unchanged. A pragma in the middle cannot be honored, so it
      fails loud before any statement runs; so does one this runner cannot
      restore afterwards (see _RESTORABLE_PRAGMAS).
    """
    prologue: list[str] = []
    body: list[str] = []
    epilogue: list[str] = []
    for stmt in _split_statements(path.read_text()):
        keyword = _statement_keyword(stmt)
        if not keyword or keyword in _TX_CONTROL:
            continue
        if keyword == "ROLLBACK":
            raise RuntimeError(
                f"{path.name}: ROLLBACK cannot run inside the migration transaction — it "
                "would tear the transaction down mid-file and leave the remaining "
                "statements autocommitting one by one. Remove it; a failing statement "
                "already rolls the whole file back."
            )
        if keyword == "PRAGMA":
            name = _pragma_name(stmt)
            if name not in _RESTORABLE_PRAGMAS:
                raise RuntimeError(
                    f"{path.name}: PRAGMA {name or stmt.strip()!r} runs outside the migration "
                    "transaction, on the connection every later file in this run shares, and "
                    "the runner cannot put it back afterwards. Add it to "
                    "db._RESTORABLE_PRAGMAS (only if reading its value is side-effect free) "
                    "or drop it."
                )
            (epilogue if body else prologue).append(stmt)
        elif epilogue:
            raise RuntimeError(
                f"{path.name}: a PRAGMA between statements would be silently ignored "
                "inside the migration transaction — move it to the top or bottom "
                "of the file"
            )
        else:
            body.append(stmt)

    before = {
        name: con.execute(f"PRAGMA {ident(name, _RESTORABLE_PRAGMAS)}").fetchone()[0]
        for name in {_pragma_name(stmt) for stmt in prologue + epilogue}
    }
    try:
        for stmt in prologue:
            con.execute(stmt)
        # BEGIN IMMEDIATE, not a deferred BEGIN: take the write lock up front
        # instead of at the first write, the idiom app/scheduling.py already uses
        # for the booking race. Both callers (main.lifespan, the specialty-event
        # script) run migrate() on its own, so a concurrent writer just means
        # waiting out busy_timeout here rather than half-way through the file.
        con.execute("BEGIN IMMEDIATE")
        try:
            for stmt in body:
                con.execute(stmt)
            con.execute("INSERT INTO schema_migrations (name) VALUES (?)", (path.name,))
            con.commit()
        except Exception:
            con.rollback()
            raise
        for stmt in epilogue:
            con.execute(stmt)
    finally:
        # A file that failed never reaches its trailing `PRAGMA foreign_keys=ON`,
        # and this connection goes on to run every later migration in the run —
        # so restore every pragma the file touched, not just the FK one.
        for name, value in before.items():
            con.execute(f"PRAGMA {ident(name, _RESTORABLE_PRAGMAS)}={int(value)}")


def migrate() -> None:
    """Apply pending migrations, each one atomically (see _apply)."""
    config.ensure_dirs()
    con = connect()
    # _apply drives BEGIN/COMMIT itself; keep pysqlite from wrapping statements
    # in implicit transactions of its own.
    con.isolation_level = None
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                       name TEXT PRIMARY KEY,
                       applied_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        applied = {r["name"] for r in con.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            aliases = MIGRATION_ALIASES.get(path.name, set())
            if path.name in applied or aliases.intersection(applied):
                continue
            _apply(con, path)
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


@contextmanager
def tx():
    """Atomic unit of work: commit on clean exit, rollback on exception.

    Use when multiple writes must land together (e.g. a soft-delete and its
    audit_log row). The caller runs statements on the yielded connection.

    The transaction is BEGIN IMMEDIATE, not pysqlite's deferred one, so the whole
    unit — its reads included — is serialized against other writers. Deferred, a
    read-then-write unit takes the write lock only at its first write, and under
    WAL the loser of that race either reads a value another transaction is about
    to overwrite or gets SQLITE_BUSY_SNAPSHOT on the upgrade, which busy_timeout
    does NOT wait out (it surfaces as an instant "database is locked"). Taking
    the lock up front makes the loser wait instead — the same idiom the migration
    runner and the booking claim already use.

    Callers must NOT issue their own BEGIN on the yielded connection: this one is
    already open, and a second one is a nested-transaction error.
    """
    con = connect()
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
