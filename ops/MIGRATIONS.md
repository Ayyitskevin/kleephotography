# Migrations policy

Forward SQL lives in [`migrations/`](../migrations/). Apply order is
**lexicographic filename order** via `sorted(MIGRATIONS_DIR.glob("*.sql"))` in
[`app/db.py`](../app/db.py). Applied names are recorded in `schema_migrations`.

## Rules going forward

1. **Unique numeric prefixes.** Each new migration gets the next unused `NNN_`
   prefix. Do not reuse a number that already exists on disk.
2. **Never renumber applied migrations** on a live database. Renaming a file that
   production already recorded under another name will either no-op (alias) or
   double-apply — both are incidents waiting to happen.
3. Prefer **additive** `ALTER TABLE … ADD COLUMN` / new tables. Table rebuilds
   only when unavoidable.
4. Rollbacks under `migrations/rollback/` are optional and incomplete; grow
   rollback coverage for **money-touching** migrations when you add them.
5. Schema / migration edits are **red-light** ([`AGENTS.md`](../AGENTS.md)):
   branch + PR, human merge.

## Known duplicate prefixes (do not “fix” by renaming)

These coexist on purpose of history; apply order is still deterministic by full
filename:

| Prefix | Files |
|--------|--------|
| `054` | `054_argus_vision.sql`, `054_gallery_reminders.sql` |
| `055` | `055_contract_unsigned_nudge.sql`, `055_plutus_upsell.sql` |

Plutus also has a filename **alias** in `app/db.py` (`MIGRATION_ALIASES`) between
`055_plutus_upsell.sql` and `058_plutus_upsell.sql` so a clean GitHub deploy does
not re-run the same `ALTER`s against production. Leave that map alone unless you
are deliberately changing deploy aliasing (red-light).
