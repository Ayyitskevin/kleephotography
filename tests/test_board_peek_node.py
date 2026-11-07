"""Behavioral JavaScript contract: the studio card's quick-actions peek owns its clicks."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.unit
def test_board_peek_click_contract_in_node():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node.js is required to run the board peek click contract"

    result = subprocess.run(
        [node, "--test", str(root / "tests/js/board-peek.test.mjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
