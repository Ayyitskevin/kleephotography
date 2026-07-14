"""Shared video-comment thread queries and status transitions."""

from fastapi import HTTPException

from . import db


def video_comment_thread(asset_id: int) -> list[dict]:
    """Visible comments for one video, ordered for both client and admin views."""
    rows = db.all_(
        """SELECT id, parent_id, timecode, body, author_role, status, created_at
           FROM video_comments
           WHERE asset_id=? AND deleted_at IS NULL
           ORDER BY timecode, created_at, id""",
        (asset_id,),
    )
    return [dict(row) for row in rows]


def cascade_status(con, root_id: int, status: str) -> None:
    """Set one thread root and its descendants to the same status."""
    con.execute(
        """WITH RECURSIVE sub(id) AS (
             SELECT id FROM video_comments WHERE id=?
             UNION ALL
             SELECT vc.id FROM video_comments vc JOIN sub ON vc.parent_id=sub.id)
           UPDATE video_comments SET status=?
           WHERE id IN (SELECT id FROM sub)""",
        (root_id, status),
    )


def resolve_comment_parent(asset_id: int, parent_raw) -> tuple[int | None, float]:
    """Validate an optional reply target and return its id and inherited timecode."""
    if not parent_raw:
        return None, -1.0
    try:
        parent_id = int(parent_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad parent_id")
    parent = db.one(
        "SELECT timecode FROM video_comments WHERE id=? AND asset_id=? AND deleted_at IS NULL",
        (parent_id, asset_id),
    )
    if not parent:
        raise HTTPException(status_code=400, detail="reply target not found")
    return parent_id, float(parent["timecode"])
