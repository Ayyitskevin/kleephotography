"""Export/crop preset definitions — single source of truth (slice D).

Presets are pure data in the crop_presets table. The render path in imaging.py
consumes any row generically; a new channel/format is a new row, not new code.
"""

from . import db


def active() -> list[dict]:
    """Active crop presets, render order. The only reader of crop_presets state."""
    return db.all_("SELECT * FROM crop_presets WHERE active=1 ORDER BY sort, id")
