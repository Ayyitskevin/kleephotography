"""Behavioral JavaScript contracts for the shared lightbox and copy-link handlers."""

import shutil
import subprocess
from pathlib import Path

import pytest


def _run_node_suite(name: str) -> None:
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node.js is required to run the JavaScript contracts"

    result = subprocess.run(
        [node, "--test", str(root / "tests/js" / name)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.unit
def test_lightbox_comment_outcomes_in_node():
    _run_node_suite("lightbox-comments.test.mjs")


@pytest.mark.unit
def test_lightbox_filtered_announcement_in_node():
    _run_node_suite("lightbox-filter.test.mjs")


@pytest.mark.unit
def test_copy_link_delegation_in_node():
    _run_node_suite("copy-link.test.mjs")
