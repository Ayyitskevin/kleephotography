"""Behavioral JavaScript timing contracts for the shared lightbox."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.unit
def test_lightbox_comment_outcomes_in_node():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node.js is required to run the lightbox timing contract"

    result = subprocess.run(
        [node, "--test", str(root / "tests/js/lightbox-comments.test.mjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
