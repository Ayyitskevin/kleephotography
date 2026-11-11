"""Catch input-only upgrades before CI installs an older locked environment."""

import runpy
from pathlib import Path

import pytest

guard = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/check_requirement_locks.py")
)
pytestmark = pytest.mark.unit


@pytest.fixture
def manifests(tmp_path):
    files = {
        "requirements.txt": "Demo_Package[standard]==1.0\n",
        "requirements-dev.txt": "pytest==9.2.0\n",
        "requirements.lock": "demo-package==1.0 \\\n    --hash=sha256:"
        + "a" * 64
        + "\nchild==2.0\n",
        "requirements-dev.lock": "demo-package==1.0\nchild==2.0\npytest==9.2.0\n",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    return tmp_path


def test_exact_pins_extras_normalized_names_and_hash_continuations(manifests):
    assert guard["check"](manifests) == []


@pytest.mark.parametrize("filename", ["requirements.txt", "requirements-dev.txt"])
def test_input_only_upgrade_is_rejected(manifests, filename):
    path = manifests / filename
    path.write_text(path.read_text().replace("==", "==99."))
    assert guard["check"](manifests)


def test_missing_dependency_is_rejected(manifests):
    (manifests / "requirements.lock").write_text("child==2.0\n")
    assert any("found missing" in error for error in guard["check"](manifests))


def test_transitive_runtime_dev_disagreement_is_rejected(manifests):
    path = manifests / "requirements-dev.lock"
    path.write_text(path.read_text().replace("child==2.0", "child==3.0"))
    assert any("runtime lock" in error for error in guard["check"](manifests))


@pytest.mark.parametrize(
    "content", ["demo>=1.0", "-r other.txt", "demo==1; python_version>'3'", ""]
)
def test_unsupported_or_empty_inputs_fail_closed(manifests, content):
    (manifests / "requirements.txt").write_text(content)
    with pytest.raises(ValueError):
        guard["check"](manifests)


def test_duplicate_normalized_names_are_rejected(manifests):
    (manifests / "requirements.txt").write_text("Demo_Package==1.0\ndemo-package==2.0\n")
    with pytest.raises(ValueError, match="duplicate"):
        guard["check"](manifests)


def test_repository_requirements_match_locks():
    assert guard["check"](Path(__file__).resolve().parents[1]) == []
