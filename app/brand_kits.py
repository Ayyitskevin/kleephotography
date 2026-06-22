"""Brand-kit overlay resolution — the active composite logo + placement for a client.

Single source of truth for what gets composited onto crops. Mirrors app/presets.py:
the render path in imaging.py stays pure, this module owns the DB + disk lookup and
hands imaging.make_crops a plain spec dict.

Hierarchy-aware (Domain A): a client with no active kit of its own inherits the
NEAREST active ancestor's kit (venue -> region -> group), via clients.ancestor_ids.
"""

from . import clients, config, db


def _kit_spec(owner_id: int, kit) -> dict | None:
    """Build a spec for kit owned by owner_id, or None if its logo file is gone."""
    path = config.BRAND_DIR / str(owner_id) / kit["stored"]
    if not path.is_file():
        return None
    return {
        "path": str(path),
        "position": kit["position"],
        "opacity": kit["opacity"],
        "scale_pct": kit["scale_pct"],
        "margin_pct": kit["margin_pct"],
    }


def overlay_for_client(client_id: int | None) -> dict | None:
    """The client's effective brand-kit overlay spec, or None when there's no client
    and no active kit anywhere up the tree. The client's own active kit wins; failing
    that, the nearest active ancestor's kit is inherited. A kit whose logo file is
    missing is skipped (the walk continues to the next ancestor)."""
    if not client_id:
        return None
    for owner_id in (client_id, *clients.ancestor_ids(client_id)):
        kit = db.one(
            "SELECT * FROM brand_kits WHERE client_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (owner_id,),
        )
        if kit:
            spec = _kit_spec(owner_id, kit)
            if spec:
                return spec
    return None
