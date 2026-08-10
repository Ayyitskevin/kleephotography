"""Shared video-comment thread queries and status transitions."""

from fastapi import HTTPException

from . import db


def video_comment_threads(asset_ids) -> dict[int, list[dict]]:
    """Visible comments for several videos at once, grouped by asset id.

    The admin bench renders one thread per video asset, so asking per asset cost
    a query per video. Same visibility rule and ordering as the single-asset
    call — that one delegates here, so the rule cannot fork. Every requested id
    gets an entry, empty list included."""
    ids = list(asset_ids)
    if not ids:
        return {}
    grouped: dict[int, list[dict]] = {asset_id: [] for asset_id in ids}
    rows = db.all_(
        f"""SELECT id, parent_id, asset_id, timecode, body, author_role, status, created_at
           FROM video_comments
           WHERE asset_id IN ({",".join("?" * len(ids))}) AND deleted_at IS NULL
           ORDER BY asset_id, timecode, created_at, id""",
        tuple(ids),
    )
    for row in rows:
        comment = dict(row)
        grouped[comment.pop("asset_id")].append(comment)
    return grouped


def video_comment_thread(asset_id: int) -> list[dict]:
    """Visible comments for one video, ordered for both client and admin views."""
    return video_comment_threads([asset_id])[asset_id]


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
