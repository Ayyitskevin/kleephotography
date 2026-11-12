"""Compatibility for stored digests and the route-independent sales boundary."""

import subprocess
import sys
from pathlib import Path

import pytest

from app.sales import rules

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "values,expected",
    [
        (
            {"prospect_id": 7, "title": "Follow up with Café", "due_date": "2030-04-16"},
            "02fd10c604a872c2f1cc8470b706b027539c3945d0b22be71d698ccaa94b0572",  # pragma: allowlist secret (synthetic digest)
        ),
        (
            {
                "request": {"channel": "email", "personalization": "Menu → spring"},
                "state": {"sources": [], "pending": None},
            },
            "281d3ec6addc539f7ca45f9c9987cd99652d43183668ca021f0222c3a64167d6",  # pragma: allowlist secret (synthetic digest)
        ),
        (
            {
                "ticket": {"kind": "outreach_preferences", "version": 2},
                "values": {
                    "reason": "Requested pause",
                    "email_blocked": 1,
                    "snoozed_until": "2030-04-16",
                },
            },
            "6c18473ede19fbb0e1408b183eeca87e238394ff0a30e4bb1e2fdf9ba3b9a4ef",  # pragma: allowlist secret (synthetic digest)
        ),
    ],
)
def test_digests_match_records_created_before_extraction(values, expected):
    # These values were captured from the original router helper. Do not change
    # serialization without a compatibility plan for saved retries and reviews.
    assert rules.command_digest(values) == expected
    assert rules.command_digest(dict(reversed(list(values.items())))) == expected


def test_sales_rules_import_without_http_storage_or_app_configuration():
    root = Path(__file__).resolve().parents[1]
    # An isolated interpreter without site packages proves this boundary is usable
    # before loading the web application or any application configuration.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); from app.sales import rules; "
            "assert rules.text_key('  Café  ') == 'café'; "
            "assert not {'app.config', 'app.db', 'app.admin', 'fastapi'} & sys.modules.keys()",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
