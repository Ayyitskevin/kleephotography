"""Filesystem paths built from request data.

Normalize with ``os.path.realpath`` (so ``..`` and symlinks collapse), then
require the result to sit strictly inside the root. The root's own path and a
sibling such as ``root-evil`` both fail the prefix check.
"""

import os
from pathlib import Path

from fastapi import HTTPException


def path_within(root: Path, *parts: str | Path) -> Path:
    """Return ``root/parts`` only when the resolved path stays inside ``root``."""
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, *parts))
    if not full.startswith(root_real + os.sep):
        raise HTTPException(status_code=400, detail="invalid path")
    return Path(full)
