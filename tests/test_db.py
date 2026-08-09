"""Tests for db.ident's SQL-identifier gate and the migration runner.

The ident cases stay pure units; the migration cases touch SQLite and are
marked integration. Full row/404 behavior is covered by smoke + integration.
"""

import shutil
import sqlite3

import pytest

from app import config, db


@pytest.mark.unit
def test_ident_allows_whitelisted():
    allowed = {"clients", "projects", "galleries", "assets", "inquiries"}
    assert db.ident("clients", allowed) == "clients"
    assert db.ident("projects", allowed) == "projects"


@pytest.mark.unit
def test_ident_rejects_disallowed_raises():
    with pytest.raises(ValueError) as exc:
        db.ident("; DROP TABLE clients;", {"clients"})
    assert "disallowed" in str(exc.value).lower()


def _isolated_db(tmp_path, monkeypatch):
    """Point config + the migration runner at a throwaway tree.

    Returns the (empty) migrations directory the test should fill.
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mise.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "ZIP_DIR", tmp_path / "zips")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "BRAND_DIR", tmp_path / "brand")
    monkeypatch.setattr(config, "RECEIPTS_DIR", tmp_path / "receipts")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    return migrations


def _clients_columns():
    return {r["name"] for r in db.all_("PRAGMA table_info(clients)")}


@pytest.mark.integration
def test_failed_migration_leaves_no_partial_schema(tmp_path, monkeypatch):
    """A file that dies half-way applies nothing and records nothing.

    Under executescript each statement autocommitted, so the first two landed
    and the missing schema_migrations row sent the next boot back through an
    already-applied ALTER — a restart loop.
    """
    migrations = _isolated_db(tmp_path, monkeypatch)
    (migrations / "001_init.sql").write_text("CREATE TABLE clients (id INTEGER PRIMARY KEY);")
    (migrations / "002_broken.sql").write_text(
        "CREATE TABLE keepers (id INTEGER PRIMARY KEY);\n"
        "ALTER TABLE clients ADD COLUMN nickname TEXT;\n"
        "ALTER TABLE clients ADD COLUMN nickname TEXT;  -- duplicate: fails\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        db.migrate()

    assert db.one("SELECT name FROM sqlite_master WHERE name='keepers'") is None
    assert "nickname" not in _clients_columns()
    assert db.one("SELECT name FROM schema_migrations WHERE name='002_broken.sql'") is None
    assert db.one("SELECT name FROM schema_migrations WHERE name='001_init.sql'") is not None


@pytest.mark.integration
def test_migration_reruns_cleanly_once_fixed(tmp_path, monkeypatch):
    """After the rollback there is nothing in the way of a corrected re-run."""
    migrations = _isolated_db(tmp_path, monkeypatch)
    (migrations / "001_init.sql").write_text("CREATE TABLE clients (id INTEGER PRIMARY KEY);")
    broken = migrations / "002_broken.sql"
    broken.write_text(
        "CREATE TABLE keepers (id INTEGER PRIMARY KEY);\n"
        "ALTER TABLE clients ADD COLUMN nickname TEXT;\n"
        "ALTER TABLE clients ADD COLUMN nickname TEXT;\n"
    )
    with pytest.raises(sqlite3.OperationalError):
        db.migrate()

    broken.write_text(
        "CREATE TABLE keepers (id INTEGER PRIMARY KEY);\n"
        "ALTER TABLE clients ADD COLUMN nickname TEXT;\n"
    )
    db.migrate()

    assert db.one("SELECT name FROM sqlite_master WHERE name='keepers'") is not None
    assert "nickname" in _clients_columns()
    assert {r["name"] for r in db.all_("SELECT name FROM schema_migrations")} == {
        "001_init.sql",
        "002_broken.sql",
    }


@pytest.mark.integration
def test_upgrade_preserves_rows_in_an_existing_database(tmp_path, monkeypatch):
    """Real chain, mid-way, with data in it — the case production is always in.

    This is the guard on 031's leading `PRAGMA foreign_keys=OFF` having to run
    OUTSIDE the runner's transaction (SQLite ignores it inside one). 031 rebuilds
    `projects` by copying it, dropping it and renaming the copy into place. With
    enforcement left ON, SQLite runs an implicit DELETE before that DROP, which
    fires the child tables' FK actions: every ON DELETE CASCADE child of a
    project (invoices — and through invoices, payments) is wiped, and every
    ON DELETE SET NULL child (emails_log) is orphaned. The *parent* rows survive
    either way, because the copy happens before the drop, so seeding child rows
    is the only way this test can tell a working runner from a broken one.
    """
    real = sorted(db.MIGRATIONS_DIR.glob("*.sql"))
    migrations = _isolated_db(tmp_path, monkeypatch)
    cutoff = "031_pipeline_stages.sql"
    for path in [p for p in real if p.name < cutoff]:
        shutil.copy(path, migrations / path.name)
    db.migrate()

    client_id = db.run("INSERT INTO clients (name, email) VALUES (?, ?)", ("Ada", "a@ex.com"))
    lead_id = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?, ?, 'lead')",
        (client_id, "Brand shoot"),
    )
    paid_id = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?, ?, 'invoice')",
        (client_id, "Editorial day"),
    )
    unpaid_id = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?, ?, 'invoice')",
        (client_id, "Headshots"),
    )
    paid_invoice_id = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status)
           VALUES (?, 'inv-paid', 'Editorial day', 120000, 'paid')""",
        (paid_id,),
    )
    db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status)
           VALUES (?, 'inv-sent', 'Headshots', 40000, 'sent')""",
        (unpaid_id,),
    )
    payment_id = db.run(
        "INSERT INTO payments (invoice_id, amount_cents, kind) VALUES (?, 120000, 'full')",
        (paid_invoice_id,),
    )
    db.run(
        """INSERT INTO emails_log (project_id, doc_kind, to_email, subject)
           VALUES (?, 'invoice', 'a@ex.com', 'Your invoice')""",
        (paid_id,),
    )

    for path in [p for p in real if p.name >= cutoff]:
        shutil.copy(path, migrations / path.name)
    db.migrate()

    # Parents: rows carried over, and 031's remap ran (payment is the gate for
    # an 'invoice' project — unpaid falls back to contract_signed).
    assert {r["title"]: r["status"] for r in db.all_("SELECT title, status FROM projects")} == {
        "Brand shoot": "inquiry_received",
        "Editorial day": "retainer_paid",
        "Headshots": "contract_signed",
    }
    assert db.one("SELECT client_id FROM projects WHERE id=?", (lead_id,))["client_id"] == client_id
    assert db.one("SELECT name FROM clients WHERE id=?", (client_id,))["name"] == "Ada"

    # Children: the rows FK enforcement would have destroyed during the rebuild.
    assert {
        r["slug"]: r["project_id"] for r in db.all_("SELECT slug, project_id FROM invoices")
    } == {"inv-paid": paid_id, "inv-sent": unpaid_id}
    payment = db.one("SELECT invoice_id FROM payments WHERE id=?", (payment_id,))
    assert payment is not None and payment["invoice_id"] == paid_invoice_id
    email = db.one("SELECT project_id FROM emails_log WHERE subject='Your invoice'")
    assert email["project_id"] == paid_id
    assert db.all_("PRAGMA foreign_key_check") == []
    assert len(db.all_("SELECT name FROM schema_migrations")) == len(real)


@pytest.mark.integration
def test_rollback_in_a_migration_file_is_rejected(tmp_path, monkeypatch):
    """A stray ROLLBACK must stop the run, not quietly end the transaction.

    Executing it would end the runner's transaction mid-file and let everything
    after it autocommit individually; stripping it would commit work the file
    asked to discard. Either way the file is half-applied, silently.
    """
    migrations = _isolated_db(tmp_path, monkeypatch)
    (migrations / "001_stray_rollback.sql").write_text(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);\nROLLBACK;\nCREATE TABLE b (id INTEGER);\n"
    )

    with pytest.raises(RuntimeError, match="ROLLBACK"):
        db.migrate()

    assert db.one("SELECT name FROM sqlite_master WHERE name='a'") is None
    assert db.one("SELECT name FROM schema_migrations WHERE name='001_stray_rollback.sql'") is None


@pytest.mark.integration
def test_unrestorable_pragma_is_rejected_before_it_can_leak(tmp_path, monkeypatch):
    """Prologue pragmas run on the connection every later file shares.

    `foreign_keys` is the one the runner knows how to put back; anything else
    would silently change how the REST of the run behaves, so it fails loud.
    """
    migrations = _isolated_db(tmp_path, monkeypatch)
    (migrations / "001_odd_pragma.sql").write_text(
        "PRAGMA legacy_alter_table=ON;\nCREATE TABLE a (id INTEGER PRIMARY KEY);\n"
    )

    with pytest.raises(RuntimeError, match="legacy_alter_table"):
        db.migrate()

    assert db.one("SELECT name FROM sqlite_master WHERE name='a'") is None


@pytest.mark.integration
def test_a_files_pragma_does_not_leak_into_the_next_migration(tmp_path, monkeypatch):
    """The restore is what keeps one file's FK toggle from disarming the next.

    002 turns enforcement off and never turns it back on (a file that failed
    part-way would look the same). 003 then inserts a child row pointing at a
    parent that does not exist: with enforcement restored it raises, and 003 is
    not recorded. If the pragma leaked, the orphan would land silently.
    """
    migrations = _isolated_db(tmp_path, monkeypatch)
    (migrations / "001_init.sql").write_text(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id));\n"
    )
    (migrations / "002_fk_off.sql").write_text(
        "PRAGMA foreign_keys=OFF;\nCREATE TABLE unrelated (id INTEGER PRIMARY KEY);\n"
    )
    (migrations / "003_needs_fk.sql").write_text("INSERT INTO child (parent_id) VALUES (999);\n")

    with pytest.raises(sqlite3.IntegrityError):
        db.migrate()

    assert db.all_("SELECT parent_id FROM child") == []
    assert db.one("SELECT name FROM schema_migrations WHERE name='003_needs_fk.sql'") is None
    assert db.one("SELECT name FROM schema_migrations WHERE name='002_fk_off.sql'") is not None
