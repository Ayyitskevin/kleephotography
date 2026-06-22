admin/      back-office routers (galleries, studio, invoices, contracts,
              proposals, licenses, presets, press, recurring, shotlist, uploads, activity)
              common.py (shared for splits)

Extractions (2026-06): dir_size/fmt_size/short_date/gallery_card/today moved to common.py; spark_series + get_or_404 + clients_for_select to db.py. Tests for common + fixed test_admin_common imports. CI: units (ignore smoke) + ruff check/format strict before smoke.

Ruff fixes post-extract (import hygiene, unused).

## Testing

- Unit tests (fast feedback): `python -m pytest tests/ --ignore=tests/test_smoke.py -q -m unit`
- Full smoke (e2e against temp DB): `MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw python -m pytest tests/test_smoke.py -q`
- Extracted basics (healthz, security headers, CSP, CSRF) now in `tests/test_basic.py` for units.
- Lint + format enforced in CI (ruff check + ruff format --check) before smoke.
