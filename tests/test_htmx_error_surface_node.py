"""Behavioral JavaScript contract for the global HTMX failure toast."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.unit
def test_htmx_error_surface_in_node():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node.js is required to run the HTMX error-surface contract"

    result = subprocess.run(
        [node, "--test", str(root / "tests/js/htmx-errors.test.mjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
