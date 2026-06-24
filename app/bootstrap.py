"""One-shot public-site showcase seeding — idempotent, runs as the mise process."""

from __future__ import annotations

import logging

from . import db

log = logging.getLogger("mise.bootstrap")

_PHOTO_TAGS = ("dishes", "drinks", "pastry", "interiors", "dishes", "drinks")

_DEMO_CS = {
    "title": "Seasonal Tasting Menu",
    "client_name": "Cúrate",
    "cs_tagline": "A tasting menu, shot at its peak.",
    "cs_brief": (
        "A full menu refresh and brand library in a single service window — "
        "plating, pours, and the dining room, delivered as a same-week gallery "
        "with social crops baked in."
    ),
    "cs_credits": (
        "Client: Cúrate\n"
        "Scope: Menu refresh · brand library\n"
        "Deliverables: 6 finals · social crop pack\n"
        "Turnaround: Same-week gallery"
    ),
    "cs_location": "Asheville, NC",
}

_TESTIMONIALS = (
    (
        "Our reservations jumped the week the new photos went live. Kevin made "
        "the food look exactly like the room feels.",
        "Maria Solis",
        "Cúrate",
    ),
    (
        "Fastest turnaround we have ever had, and the social crops mean our "
        "marketing person stopped re-cropping everything by hand.",
        "Dev Carter",
        "High Five Coffee",
    ),
    (
        "He shot a full menu refresh between lunch and dinner service without "
        "ever getting in the way. Rare.",
        "Jamie Booth",
        "Bull & Beggar",
    ),
)


def ensure_public_showcase() -> bool:
    """Idempotent backfill so prototype layout sections have content to render.

    Each piece is independent — partial prior seeding (e.g. photos only via admin)
    does not block videos, case study, or testimonials.
    """
    changed = False

    if not db.one("SELECT 1 AS x FROM assets WHERE portfolio=1 AND status='ready' LIMIT 1"):
        photos = db.all_("SELECT id FROM assets WHERE kind='photo' AND status='ready' ORDER BY id")
        for i, photo in enumerate(photos):
            db.run(
                "UPDATE assets SET portfolio=1, portfolio_tag=? WHERE id=?",
                (_PHOTO_TAGS[i % len(_PHOTO_TAGS)], photo["id"]),
            )
        changed = True
        log.info("portfolio photos starred (%d)", len(photos))

    if not db.one(
        "SELECT 1 AS x FROM assets WHERE portfolio=1 AND kind='video' AND status='ready' LIMIT 1"
    ):
        db.run(
            """UPDATE assets SET portfolio=1, portfolio_tag='motion'
               WHERE kind='video' AND status='ready' AND portfolio=0"""
        )
        changed = True
        log.info("portfolio video starred for motion sections")

    gallery = db.one(
        "SELECT id, title, client_name, cs_published FROM galleries ORDER BY id LIMIT 1"
    )
    if gallery and not gallery["cs_published"]:
        db.run(
            """UPDATE galleries
               SET title=?, client_name=?, cs_published=1,
                   cs_tagline=?, cs_brief=?, cs_credits=?, cs_location=?
               WHERE id=?""",
            (
                _DEMO_CS["title"],
                _DEMO_CS["client_name"],
                _DEMO_CS["cs_tagline"],
                _DEMO_CS["cs_brief"],
                _DEMO_CS["cs_credits"],
                _DEMO_CS["cs_location"],
                gallery["id"],
            ),
        )
        changed = True
        log.info("demo case study published (gallery %s)", gallery["id"])
    elif gallery and (
        (gallery["client_name"] or "").strip() in {"", "Mise Demo"}
        or (gallery["title"] or "").strip() == "Sample Tasting Menu"
    ):
        db.run(
            """UPDATE galleries SET title=?, client_name=?,
                   cs_tagline=COALESCE(NULLIF(cs_tagline,''), ?),
                   cs_brief=COALESCE(NULLIF(cs_brief,''), ?),
                   cs_credits=CASE
                       WHEN cs_credits IS NULL OR cs_credits='' OR cs_credits LIKE '%Mise Demo%'
                       THEN ? ELSE cs_credits END,
                   cs_location=COALESCE(NULLIF(cs_location,''), ?),
                   cs_published=1
               WHERE id=?""",
            (
                _DEMO_CS["title"],
                _DEMO_CS["client_name"],
                _DEMO_CS["cs_tagline"],
                _DEMO_CS["cs_brief"],
                _DEMO_CS["cs_credits"],
                _DEMO_CS["cs_location"],
                gallery["id"],
            ),
        )
        changed = True
        log.info("showcase gallery relabeled (gallery %s)", gallery["id"])

    if not db.one("SELECT 1 AS x FROM testimonials WHERE published=1 LIMIT 1"):
        gid = gallery["id"] if gallery else None
        for pos, (quote, name, biz) in enumerate(_TESTIMONIALS):
            db.run(
                """INSERT INTO testimonials
                   (quote, attribution_name, business, gallery_id, position, published)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (quote, name, biz, gid if pos == 0 else None, pos),
            )
        changed = True
        log.info("testimonials seeded (%d)", len(_TESTIMONIALS))

    return changed
