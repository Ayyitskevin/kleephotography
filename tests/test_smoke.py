"""Smoke suite moved to tests/smoke/ (split by domain).

Run:
  MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
    python -m pytest tests/smoke -q
Or: python -m pytest tests/ -q -m smoke
"""
