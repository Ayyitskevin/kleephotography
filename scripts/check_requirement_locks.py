"""Check this repo's exact pins without installing dependencies or resolving packages."""

import re
import sys
from pathlib import Path

PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9.!+_-]+)")
HASH = re.compile(r"--hash=sha256:[a-f0-9]{64}")


def read_pins(path: Path, *, lock: bool = False) -> dict[str, str]:
    pins = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if lock:
            line = line.removesuffix("\\").rstrip()
        if not line or (lock and HASH.fullmatch(line)):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"{path.name}:{number}: expected an exact package==version pin")
        name, version = match.groups()
        name = re.sub(r"[-_.]+", "-", name).lower()
        if name in pins:
            raise ValueError(f"{path.name}:{number}: duplicate package {name}")
        pins[name] = version
    if not pins:
        raise ValueError(f"{path.name}: no package pins found")
    return pins


def check(root: Path) -> list[str]:
    runtime = read_pins(root / "requirements.txt")
    development = read_pins(root / "requirements-dev.txt")
    runtime_lock = read_pins(root / "requirements.lock", lock=True)
    development_lock = read_pins(root / "requirements-dev.lock", lock=True)
    errors = []
    # The development environment must include the same runtime versions,
    # including transitives, so tests exercise the deployment lock.
    for expected, actual, label in (
        (runtime, runtime_lock, "requirements.lock"),
        (runtime, development_lock, "requirements-dev.lock (runtime inputs)"),
        (development, development_lock, "requirements-dev.lock (dev inputs)"),
        (runtime_lock, development_lock, "requirements-dev.lock (runtime lock)"),
    ):
        for name, version in expected.items():
            if actual.get(name) != version:
                errors.append(
                    f"{label}: {name} expected {version}, found {actual.get(name, 'missing')}"
                )
    return errors


def main() -> int:
    try:
        errors = check(Path(__file__).resolve().parents[1])
    except (OSError, ValueError) as error:
        errors = [str(error)]
    if errors:
        print("Requirements lock check failed:\n" + "\n".join(errors), file=sys.stderr)
        print("Regenerate both hash lockfiles; see ops/DEPENDENCIES.md.", file=sys.stderr)
        return 1
    print("Requirements input and lock versions agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
