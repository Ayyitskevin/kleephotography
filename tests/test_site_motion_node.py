"""Behavioral JavaScript contract for the marketing motion layer (plan D-8)."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.unit
def test_site_motion_layer_in_node():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node.js is required to run the site motion contract"

    result = subprocess.run(
        [node, "--test", str(root / "tests/js/site-motion.test.mjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
