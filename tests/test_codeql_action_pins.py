"""CodeQL steps exchange versioned state; mixed pins break the Analyze step."""

import re
from pathlib import Path

import pytest


@pytest.mark.unit
def test_codeql_steps_share_one_pinned_release():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/codeql.yml").read_text()
    steps = re.findall(r"uses:\s*github/codeql-action/([^@\s]+)@([^\s#]+)", workflow)
    assert {"init", "analyze"} <= {action for action, _ in steps}
    assert len({pin for _, pin in steps}) == 1, "Update all CodeQL actions together"
