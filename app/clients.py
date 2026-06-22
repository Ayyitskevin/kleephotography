"""Client hierarchy walks — the single source of truth for ancestor/descendant
traversal of clients.parent_id (hospitality group -> region -> venue).

Both directions use a depth-counting recursive CTE so callers get results in a
defined order: ancestors nearest-first (so "nearest active kit wins" is real,
not incidental), descendants top-down. Cycle-safe via UNION (de-dupes), though
the set-parent route's guards should make a cycle impossible to create.
"""

from . import db


def ancestor_ids(client_id: int) -> list[int]:
    """Ancestor client ids, NEAREST FIRST (parent, grandparent, ...). Excludes self."""
    rows = db.all_(
        "WITH RECURSIVE sup(id, parent_id, depth) AS ("
        "  SELECT id, parent_id, 0 FROM clients WHERE id=?"
        "  UNION"
        "  SELECT c.id, c.parent_id, sup.depth+1 FROM clients c JOIN sup ON c.id=sup.parent_id"
        ") SELECT id FROM sup WHERE id<>? ORDER BY depth",
        (client_id, client_id),
    )
    return [r["id"] for r in rows]


def descendant_ids(client_id: int) -> list[int]:
    """Descendant client ids, top-down (children, grandchildren, ...). Excludes self."""
    rows = db.all_(
        "WITH RECURSIVE sub(id, depth) AS ("
        "  SELECT id, 0 FROM clients WHERE id=?"
        "  UNION"
        "  SELECT c.id, sub.depth+1 FROM clients c JOIN sub ON c.parent_id=sub.id"
        ") SELECT id FROM sub WHERE id<>? ORDER BY depth, id",
        (client_id, client_id),
    )
    return [r["id"] for r in rows]
