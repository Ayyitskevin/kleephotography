"""Smoke domain slice — see tests/smoke/conftest.py for fixtures."""

import io
import os
import re
import tempfile
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db, platekit
from app.main import app
from tests.smoke._helpers import (
    _checkout_event,
    _cleanup_money_chain,
    _close,
    _jpeg_bytes,
    _logo_png,
    _mp4_bytes,
    _post_signed,
    _quo_sig,
    _ready_photo_gallery,
    _ready_video,
    _seam_license_with_gallery,
    _seed_money_chain,
    _spark_rect_count,
    _stripe_sig,
)

pytestmark = pytest.mark.smoke


def test_services_page():
    from app.public.site import SERVICES

    with TestClient(app) as pub:
        r = pub.get("/services")
        assert r.status_code == 200
        # every specialty group + its tiers render (titles are HTML-escaped —
        # the specialty groups carry '&' since the per-specialty catalog)
        for s in SERVICES:
            assert s["title"].replace("&", "&amp;") in r.text, s["key"]
            for t in s["tiers"]:
                # every tier name should appear at least 3 times (once per category)
                # but we just need each card present per service
                assert f">{t['name']}</h3>" in r.text
        # tier count: every specialty group renders 3 tiers
        assert r.text.count("svc-tier ") + r.text.count('svc-tier"') >= 3 * len(SERVICES)
        # middle tier flagged as "Most picked" (UX nudge), once per group
        assert r.text.count("Most picked") == len(SERVICES)
        # Prototype copy: public tier cards show marketing display prices
        # (board dollars match price_cents; admin PRESETS paid lines match too).
        for s in SERVICES:
            assert s["contact_service"]  # deep-link target for /contact
            for t in s["tiers"]:
                assert t["display_price"] in r.text, (s["key"], t["name"])
                assert t["subtitle"] in r.text, (s["key"], t["name"])
        # Tier CTAs deep-link to /contact with service+tier prefill; foot
        # keeps Book a shoot → /book; secondary "See past work" → /work.
        assert "contact?service=" in r.text
        assert "tier=" in r.text
        assert r.text.count('data-analytics-event="Service Tier CTA"') == 3 * len(SERVICES)
        assert "data-analytics-service=" in r.text and "data-analytics-tier=" in r.text
        assert 'href="/book"' in r.text
        assert 'href="/work"' in r.text
        # Service + Offer JSON-LD mirrors the catalog (board price_cents as price)
        assert '"@type": "Service"' in r.text
        assert '"@type": "Offer"' in r.text
        assert '"priceCurrency": "USD"' in r.text
        # nav from any other site page links to /services
        assert 'href="/services"' in pub.get("/").text
        # SEO bits
        assert 'name="description"' in r.text and "Asheville" in r.text
        assert 'property="og:title"' in r.text


def test_faq_block():
    # /book and /contact each carry their OWN FAQ set (split 2026-06; book = booking
    # logistics, contact = pricing/ownership). Same accordion + FAQPage JSON-LD, but
    # the content differs, so spot-check a distinctive question from each set — that
    # proves the right set is wired to the right page, not just that *some* FAQ renders.
    from app.public.site import BOOK_FAQS, CONTACT_FAQS

    pages = {
        "/book": (BOOK_FAQS, "How far in advance should I book?"),
        "/contact": (CONTACT_FAQS, "What does a typical project cost?"),
    }
    with TestClient(app) as pub:
        for path, (faqs, spot) in pages.items():
            r = pub.get(path)
            assert r.status_code == 200
            # every Q renders as one <details>
            assert r.text.count('<details class="faq-item">') == len(faqs), path
            # distinctive question text proves the right set is wired to this page
            assert spot in r.text, path
            # FAQPage structured data for Google rich results
            assert '"@type": "FAQPage"' in r.text, path
            assert '"@type": "Question"' in r.text, path
            assert '"acceptedAnswer"' in r.text, path
            # links to /contact (so visitors can escalate beyond the FAQ)
            assert 'href="/contact"' in r.text, path
        # other marketing pages don't carry the FAQ (different intent)
        assert '"@type": "FAQPage"' not in pub.get("/portfolio").text
        assert '"@type": "FAQPage"' not in pub.get("/").text


def test_license_lifecycle(admin):
    import json as _json

    # holder client
    admin.post(
        "/admin/studio/clients",
        data={"name": "Licensing Che", "company": "Moat Bistro", "email": "moat@bistro.com"},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")

    # create a license → one 'create' audit row
    r = admin.post(
        f"/admin/studio/clients/{c['id']}/licenses",
        data={"title": "Spring menu — social"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    lic = db.one("SELECT * FROM licenses ORDER BY id DESC LIMIT 1")
    assert lic["holder_client_id"] == c["id"]
    assert lic["coverage_scope"] == "holder_only"  # the schema default
    assert lic["status"] == "draft" and lic["published"] == 0
    created = db.all_(
        """SELECT * FROM audit_log WHERE entity_type='license'
                         AND entity_id=? AND action='create'""",
        (lic["id"],),
    )
    assert len(created) == 1

    # detail page renders
    assert admin.get(f"/admin/studio/licenses/{lic['id']}").status_code == 200

    # update with a real change → 'update' audit row carries the diff
    r = admin.post(
        f"/admin/studio/licenses/{lic['id']}",
        data={
            "title": "Spring menu — social + web",
            "usage_tier": "extended",
            "exclusivity": "non_exclusive",
            "coverage_scope": "holder_only",
            "fee": "1500.00",
            "territory": ["US", "worldwide"],
            "channels": ["website", "social_organic"],
            "ends_on": "2099-01-01",
            "published": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    lic = db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))
    assert lic["fee_cents"] == 150000 and lic["usage_tier"] == "extended"
    assert lic["published"] == 1
    assert set(_json.loads(lic["territory"])) == {"US", "worldwide"}
    upd = db.one(
        """SELECT diff_json FROM audit_log WHERE entity_type='license'
                    AND entity_id=? AND action='update' ORDER BY id DESC LIMIT 1""",
        (lic["id"],),
    )
    diff = _json.loads(upd["diff_json"])
    assert "usage_tier" in diff and diff["usage_tier"] == ["standard", "extended"]
    assert "fee_cents" in diff

    # a no-op update writes NO new audit row (append-only stays clean)
    before = db.one(
        """SELECT COUNT(*) AS n FROM audit_log
                       WHERE entity_type='license' AND entity_id=?""",
        (lic["id"],),
    )["n"]
    admin.post(
        f"/admin/studio/licenses/{lic['id']}",
        data={
            "title": lic["title"],
            "usage_tier": "extended",
            "exclusivity": "non_exclusive",
            "coverage_scope": "holder_only",
            "fee": "1500.00",
            "territory": ["US", "worldwide"],
            "channels": ["website", "social_organic"],
            "ends_on": "2099-01-01",
            "published": "1",
        },
        follow_redirects=False,
    )
    after = db.one(
        """SELECT COUNT(*) AS n FROM audit_log
                      WHERE entity_type='license' AND entity_id=?""",
        (lic["id"],),
    )["n"]
    assert after == before

    # status change → its own audit row + status persisted
    admin.post(
        f"/admin/studio/licenses/{lic['id']}/status",
        data={"status": "active"},
        follow_redirects=False,
    )
    assert db.one("SELECT status FROM licenses WHERE id=?", (lic["id"],))["status"] == "active"
    sc = db.one(
        """SELECT diff_json FROM audit_log WHERE entity_type='license'
                   AND entity_id=? AND action='status_change' ORDER BY id DESC LIMIT 1""",
        (lic["id"],),
    )
    assert _json.loads(sc["diff_json"])["status"] == ["draft", "active"]

    # bad status rejected
    assert (
        admin.post(
            f"/admin/studio/licenses/{lic['id']}/status",
            data={"status": "bogus"},
            follow_redirects=False,
        ).status_code
        == 400
    )

    # active + dated within 45d surfaces on the dashboard + licenses strips
    db.run("UPDATE licenses SET ends_on=date('now','+10 days') WHERE id=?", (lic["id"],))
    assert lic["title"] in admin.get("/admin/studio/licenses").text
    assert "expiring" in admin.get("/admin/studio/activity").text.lower()

    # 'specific' coverage syncs the join table inside the same tx
    other = db.run("INSERT INTO clients (name) VALUES (?)", ("Sister Venue",))
    admin.post(
        f"/admin/studio/licenses/{lic['id']}",
        data={
            "title": lic["title"],
            "usage_tier": "extended",
            "exclusivity": "non_exclusive",
            "coverage_scope": "specific",
            "fee": "1500.00",
            "cover_client_ids": [str(other)],
            "published": "1",
        },
        follow_redirects=False,
    )
    covered = db.all_("SELECT client_id FROM license_clients WHERE license_id=?", (lic["id"],))
    assert [r["client_id"] for r in covered] == [other]

    # atomic soft-delete: deleted_at set, one soft_delete audit row, excluded
    # from the active list, but the audit trail survives (append-only)
    n_audit_before = db.one(
        """SELECT COUNT(*) AS n FROM audit_log
                               WHERE entity_type='license' AND entity_id=?""",
        (lic["id"],),
    )["n"]
    r = admin.post(f"/admin/studio/licenses/{lic['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT deleted_at FROM licenses WHERE id=?", (lic["id"],))["deleted_at"]
    assert admin.get(f"/admin/studio/licenses/{lic['id']}").status_code == 404
    assert lic["title"] not in admin.get("/admin/studio/licenses").text
    n_audit_after = db.one(
        """SELECT COUNT(*) AS n FROM audit_log
                              WHERE entity_type='license' AND entity_id=?""",
        (lic["id"],),
    )["n"]
    assert n_audit_after == n_audit_before + 1
    assert (
        db.one(
            """SELECT action FROM audit_log WHERE entity_type='license'
                     AND entity_id=? ORDER BY id DESC LIMIT 1""",
            (lic["id"],),
        )["action"]
        == "soft_delete"
    )


def test_license_holder_and_descendants_cascade(admin):
    """coverage_scope='holder_and_descendants' resolves through the Domain A
    client tree (clients.descendant_ids): a group-held license reaches every
    descendant venue, holder first. holder_only stays just the holder; 'specific'
    stays explicit-only (descendants are NOT auto-pulled). The detail page makes
    the resolved reach visible — proving the cascade, not just storing the flag."""

    from app.admin.licenses import effective_coverage

    def _cov_block(html):  # isolate the effective-coverage readout from the
        m = re.search(r'coverage-list">(.*?)</ul>', html, re.S)  # 'Also covers'
        return m.group(1) if m else ""  # checkbox list

    group = db.run(
        "INSERT INTO clients (name, company) VALUES (?,?)", ("Hospitality Group", "BigCo")
    )
    region = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("West Region", group))
    venue = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("Harbor Venue", region))

    r = admin.post(
        f"/admin/studio/clients/{group}/licenses",
        data={"title": "Group brand grant"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    lic = db.one(
        "SELECT * FROM licenses WHERE holder_client_id=? ORDER BY id DESC LIMIT 1", (group,)
    )

    # schema default holder_only → only the holder is reached
    assert effective_coverage(db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))) == [group]
    page = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert "1 client reached" in page
    assert "Harbor Venue" not in _cov_block(page)  # descendant not yet reached

    # flip to holder_and_descendants → holder first, then descendants top-down
    admin.post(
        f"/admin/studio/licenses/{lic['id']}",
        data={"title": lic["title"], "coverage_scope": "holder_and_descendants"},
        follow_redirects=False,
    )
    row = db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))
    assert effective_coverage(row) == [group, region, venue]
    page = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert "3 clients reached" in page
    block = _cov_block(page)
    assert "Harbor Venue" in block and "West Region" in block  # cascade is VISIBLE

    # 'specific' is explicit-only — the descendant venue is NOT auto-included
    admin.post(
        f"/admin/studio/licenses/{lic['id']}",
        data={
            "title": lic["title"],
            "coverage_scope": "specific",
            "cover_client_ids": [str(region)],
        },
        follow_redirects=False,
    )
    row = db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))
    assert effective_coverage(row) == [group, region]  # venue NOT pulled in
    assert "Harbor Venue" not in _cov_block(admin.get(f"/admin/studio/licenses/{lic['id']}").text)


def test_pricing_suggestion_math():
    """The Asheville rate card maps usage params -> a suggested licensing fee.
    Encodes WHY each driver matters: territory takes the MAX selected, channels
    add per-channel uplift (heavy > light), perpetual doubles, multi-year prorates,
    and the 'exclusive' tier already prices lockout so the exclusivity flag must
    NOT stack on it (else clients get double-charged)."""
    from app import pricing

    def lic(**kw):
        base = {
            "usage_tier": "standard",
            "territory": "[]",
            "channels": "[]",
            "exclusivity": "non_exclusive",
            "perpetual": 0,
            "starts_on": None,
            "ends_on": None,
        }
        base.update(kw)
        return base

    assert pricing.suggest_license_fee(lic())["total_cents"] == 27500  # base only
    r = pricing.suggest_license_fee(
        lic(territory='["US"]', channels='["website","social_organic","social_paid"]')
    )
    assert r["territory_mult"] == 1.4
    assert r["channel_mult"] == 1.28  # website free + .08 light + .20 heavy
    assert r["total_cents"] == round(27500 * 1.4 * 1.28)
    # 'exclusive' tier: exclusivity flag does NOT stack (no double-count).
    et = pricing.suggest_license_fee(lic(usage_tier="exclusive", exclusivity="exclusive"))
    assert et["excl_mult"] == 1.0 and et["total_cents"] == 170000
    # exclusivity DOES multiply a non-exclusive tier.
    ex = pricing.suggest_license_fee(lic(usage_tier="extended", exclusivity="exclusive"))
    assert ex["excl_mult"] == 1.8 and ex["total_cents"] == round(60000 * 1.8)
    # perpetual doubles; territory is the MAX of those selected.
    pp = pricing.suggest_license_fee(lic(perpetual=1, territory='["local_metro","worldwide"]'))
    assert pp["term_mult"] == 2.0 and pp["territory_mult"] == 2.5
    assert pp["total_cents"] == round(27500 * 2.5 * 2.0)
    # ~17-month fixed term spans into year 2 -> +25%.
    y2 = pricing.suggest_license_fee(lic(starts_on="2026-01-01", ends_on="2027-06-01"))
    assert y2["term_mult"] == 1.25


def test_pricing_travel_market_rate_cards():
    """Charlotte and Raleigh are travel markets with their own base rate cards
    (Charlotte premium, Raleigh mid). Encodes WHY: only the per-tier base changes
    between markets — the usage multipliers are market-independent doctrine, so
    the same row priced in two markets differs ONLY by the base ratio. An unknown
    market falls back to Asheville rather than erroring (advisory, never blocking)."""
    from app import pricing

    def lic(**kw):
        base = {
            "usage_tier": "standard",
            "territory": "[]",
            "channels": "[]",
            "exclusivity": "non_exclusive",
            "perpetual": 0,
            "starts_on": None,
            "ends_on": None,
        }
        base.update(kw)
        return base

    # Base-only suggestion picks the market's base card.
    assert pricing.suggest_license_fee(lic(), market="raleigh")["total_cents"] == 35000
    assert pricing.suggest_license_fee(lic(), market="charlotte")["total_cents"] == 42500
    # Multipliers are identical across markets — only the base scales.
    args = dict(territory='["worldwide"]', channels='["website","print"]')
    ash = pricing.suggest_license_fee(lic(**args), market="asheville")
    chr = pricing.suggest_license_fee(lic(**args), market="charlotte")
    assert ash["territory_mult"] == chr["territory_mult"] == 2.5
    assert ash["channel_mult"] == chr["channel_mult"] == 1.20
    assert chr["total_cents"] == round(42500 * 2.5 * 1.20)
    # The premium ratio is carried entirely by the base.
    assert chr["total_cents"] / ash["total_cents"] == 42500 / 27500
    # Unknown market -> Asheville fallback, but the breakdown reports what was asked.
    unk = pricing.suggest_license_fee(lic(), market="nashville")
    assert unk["base_cents"] == 27500 and unk["market"] == "nashville"


def test_license_suggestion_follows_client_market(admin):
    """The license detail page prices the suggestion in the HOLDER client's home
    market, not a hardcoded default. Encodes WHY: a Charlotte client's grant must
    quote the Charlotte rate card, so changing the client's market changes the
    suggested fee end-to-end (route -> pricing -> rendered page)."""
    cli = db.run("INSERT INTO clients (name) VALUES (?)", ("Charlotte Bistro",))
    # Move the client to Charlotte via the editor route (validates the vocab).
    r = admin.post(
        f"/admin/studio/clients/{cli}",
        data={"name": "Charlotte Bistro", "market": "charlotte"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.one("SELECT market FROM clients WHERE id=?", (cli,))["market"] == "charlotte"
    admin.post(
        f"/admin/studio/clients/{cli}/licenses",
        data={"title": "Bistro web license"},
        follow_redirects=False,
    )
    lic = db.one(
        "SELECT id FROM licenses WHERE holder_client_id=? ORDER BY id DESC LIMIT 1", (cli,)
    )
    page = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert "Suggested (charlotte rate card)" in page
    # An unknown market is rejected at the editor, not silently stored.
    bad = admin.post(
        f"/admin/studio/clients/{cli}",
        data={"name": "Charlotte Bistro", "market": "atlantis"},
        follow_redirects=False,
    )
    assert bad.status_code == 400
    assert db.one("SELECT market FROM clients WHERE id=?", (cli,))["market"] == "charlotte"


def test_license_suggested_fee_is_advisory(admin):
    """The suggested fee is DISPLAY ONLY. It renders on the detail page, but the
    human-typed fee_cents is the source of truth — saving never lets the pricing
    engine overwrite what Kevin chose to charge (governance: AI suggests prices,
    it does not set them)."""
    cli = db.run("INSERT INTO clients (name) VALUES (?)", ("Asheville Cafe",))
    r = admin.post(
        f"/admin/studio/clients/{cli}/licenses",
        data={"title": "Cafe web license"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    lic = db.one("SELECT * FROM licenses WHERE holder_client_id=? ORDER BY id DESC LIMIT 1", (cli,))
    page = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert "Suggested (asheville rate card)" in page
    admin.post(
        f"/admin/studio/licenses/{lic['id']}",
        data={"title": lic["title"], "usage_tier": "standard", "fee": "123.45"},
        follow_redirects=False,
    )
    row = db.one("SELECT fee_cents FROM licenses WHERE id=?", (lic["id"],))
    assert row["fee_cents"] == 12345  # untouched by the suggestion engine


def test_license_reverse_lookup_on_covered_client(admin):
    """The bottom-up inverse of the cascade: a venue's OWN page surfaces licenses
    it is reached by without holding. A holder_and_descendants grant on an
    ancestor shows as 'group cascade'; an explicit 'specific' grant held elsewhere
    that lists the venue shows as 'added explicitly'; a holder_only grant reaches
    nobody below, so it must NOT appear. Coverage must be visible from the covered
    side, not only the holder side (R14/R21) — otherwise a venue can't see what it
    may use without hunting every ancestor."""

    def _covered(html):  # isolate the 'Also covered by' table from the rest of
        m = re.search(r"Also covered by</h3>(.*?)<h2>", html, re.S)  # the page
        return m.group(1) if m else ""

    grp = db.run("INSERT INTO clients (name) VALUES (?)", ("Reverse Group",))
    reg = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("Reverse Region", grp))
    ven = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("Reverse Venue", reg))

    admin.post(
        f"/admin/studio/clients/{grp}/licenses",
        data={"title": "RL group grant"},
        follow_redirects=False,
    )
    lic = db.one("SELECT * FROM licenses WHERE holder_client_id=? ORDER BY id DESC LIMIT 1", (grp,))

    # holder_only (create default) reaches nobody below → venue page shows nothing
    assert "RL group grant" not in _covered(admin.get(f"/admin/studio/clients/{ven}").text)

    # flip to holder_and_descendants → cascades down; venue shows it as a group grant
    admin.post(
        f"/admin/studio/licenses/{lic['id']}",
        data={"title": lic["title"], "coverage_scope": "holder_and_descendants"},
        follow_redirects=False,
    )
    block = _covered(admin.get(f"/admin/studio/clients/{ven}").text)
    assert "RL group grant" in block  # the license itself
    assert "Reverse Group" in block  # holder named + linked
    assert "group cascade" in block  # relationship labelled
    # the HOLDER's own page does not list it under 'covered by' — it HOLDS it
    assert "RL group grant" not in _covered(admin.get(f"/admin/studio/clients/{grp}").text)

    # an explicit 'specific' grant held elsewhere, listing the venue → 'added explicitly'
    other = db.run("INSERT INTO clients (name) VALUES (?)", ("Other Holder",))
    admin.post(
        f"/admin/studio/clients/{other}/licenses",
        data={"title": "RL specific grant"},
        follow_redirects=False,
    )
    lic2 = db.one(
        "SELECT * FROM licenses WHERE holder_client_id=? ORDER BY id DESC LIMIT 1", (other,)
    )
    admin.post(
        f"/admin/studio/licenses/{lic2['id']}",
        data={"title": lic2["title"], "coverage_scope": "specific", "cover_client_ids": [str(ven)]},
        follow_redirects=False,
    )
    block = _covered(admin.get(f"/admin/studio/clients/{ven}").text)
    assert "RL specific grant" in block and "added explicitly" in block


def test_license_expiry_cue_on_detail(admin):
    """The detail page shows the SAME expiry urgency cue the list strip uses
    (shared expiry_cue helper / threshold), so the two surfaces never disagree.
    Display-only: within threshold → cue; far-out / perpetual → nothing;
    already past → 'lapsed', not 'expiring'."""
    admin.post(
        "/admin/studio/clients",
        data={"name": "Expiry Cue Che", "company": "Threshold Bistro"},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/licenses",
        data={"title": "Expiry cue license"},
        follow_redirects=False,
    )
    lic = db.one("SELECT * FROM licenses ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/licenses/{lic['id']}/status",
        data={"status": "active"},
        follow_redirects=False,
    )
    url = f"/admin/studio/licenses/{lic['id']}"

    # within threshold (active, dated, not perpetual) → the cue renders
    db.run(
        "UPDATE licenses SET ends_on=date('now','+10 days'), perpetual=0 WHERE id=?", (lic["id"],)
    )
    body = admin.get(url).text
    assert "License period:" in body and "expiring" in body

    # far out → silent (no cue)
    db.run("UPDATE licenses SET ends_on=date('now','+400 days') WHERE id=?", (lic["id"],))
    assert "License period:" not in admin.get(url).text

    # perpetual → silent even though a stray end date exists
    db.run(
        "UPDATE licenses SET perpetual=1, ends_on=date('now','+10 days') WHERE id=?", (lic["id"],)
    )
    assert "License period:" not in admin.get(url).text

    # already lapsed → shows 'lapsed', NOT 'expiring'
    db.run(
        "UPDATE licenses SET perpetual=0, ends_on=date('now','-5 days') WHERE id=?", (lic["id"],)
    )
    body = admin.get(url).text
    assert "lapsed" in body and "expiring" not in body


def test_audit_diff_renders_as_chips(admin):
    """Audit diffs are STORED as JSON (territory/channels are json-array strings),
    and that stored shape is the load-bearing append-only record — unchanged. The
    audit VIEW renders those values as chips, not raw ["..."] brackets."""
    import json as _json

    admin.post("/admin/studio/clients", data={"name": "Audit Chips Co"}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/licenses",
        data={"title": "Chips license"},
        follow_redirects=False,
    )
    lic = db.one("SELECT * FROM licenses ORDER BY id DESC LIMIT 1")
    # update with multi-value territory + channels → diff stores JSON-array strings
    admin.post(
        f"/admin/studio/licenses/{lic['id']}",
        data={
            "title": "Chips license",
            "usage_tier": "standard",
            "exclusivity": "non_exclusive",
            "coverage_scope": "holder_only",
            "fee": "0",
            "territory": ["US", "worldwide"],
            "channels": ["website", "social_organic"],
        },
        follow_redirects=False,
    )

    # the STORED diff is still raw JSON — the new value is a json-encoded array
    # string (the contract audit.log writes is untouched)
    row = db.one(
        """SELECT diff_json FROM audit_log WHERE entity_type='license'
                    AND entity_id=? AND action='update' ORDER BY id DESC LIMIT 1""",
        (lic["id"],),
    )
    stored = _json.loads(row["diff_json"])
    assert _json.loads(stored["territory"][1]) == ["US", "worldwide"]

    # the VIEW renders chips, not the bracketed/escaped JSON string
    body = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert '<span class="diff-chip">US</span>' in body
    assert '<span class="diff-chip">worldwide</span>' in body
    assert '<span class="diff-chip">website</span>' in body
    assert "[&#34;US&#34;," not in body  # raw escaped-quote JSON must not leak through


def test_crop_preset_engine(client):
    """The render path consumes any crop_presets row generically: a new format
    is a new row, not new code. Proven by adding a 4th preset and rendering it
    with zero changes to imaging.make_crops."""
    from pathlib import Path as P

    from app import imaging, presets

    # the 3 social ratios ship seeded + active, slugs match the on-disk filenames
    active = presets.active()
    seeded = {ps["slug"]: (ps["width"], ps["height"]) for ps in active}
    assert seeded == {"1x1": (1080, 1080), "4x5": (1080, 1350), "9x16": (1080, 1920)}

    src = P(tempfile.mkdtemp()) / "dish.jpg"
    src.write_bytes(_jpeg_bytes(2000, 1500))

    with tempfile.TemporaryDirectory() as d:
        out = P(d)
        written = imaging.make_crops(str(src), out, "dish", 85, active)
        assert sorted(written) == ["dish_1x1.jpg", "dish_4x5.jpg", "dish_9x16.jpg"]
        with Image.open(out / "dish_9x16.jpg") as im:
            assert im.size == (1080, 1920)

        # add a brand-new format as pure data — a wide menu-board crop — and
        # render again. No code change: the same render path picks it up.
        db.run("""INSERT INTO crop_presets (slug, name, ratio_label, width, height,
                                            target_channel, sort)
                  VALUES ('3x2','Menu board (3:2)','3:2',1500,1000,'menu_print',40)""")
        try:
            written2 = imaging.make_crops(str(src), out, "dish", 85, presets.active())
            assert "dish_3x2.jpg" in written2
            with Image.open(out / "dish_3x2.jpg") as im:
                assert im.size == (1500, 1000)
        finally:
            db.run("DELETE FROM crop_presets WHERE slug='3x2'")


def test_brand_overlay_additive(client):
    """Load-bearing invariant: an overlay can only ADD pixels, never alter the
    base render. With every seeded preset at brand_overlay=0, passing an overlay
    spec must be byte-identical to overlay=None — same SHA-256 per file."""
    import hashlib
    from pathlib import Path as P

    from app import imaging, presets

    def _hashes(out):
        return {
            f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(out.glob("*.jpg"))
        }

    src = P(tempfile.mkdtemp()) / "dish.jpg"
    src.write_bytes(_jpeg_bytes(2000, 1500))
    logo = P(tempfile.mkdtemp()) / "logo.png"
    logo.write_bytes(_logo_png())
    overlay = {
        "path": str(logo),
        "position": "br",
        "opacity": 100,
        "scale_pct": 22,
        "margin_pct": 4,
    }

    active = presets.active()  # 3 seeded presets, all brand_overlay=0
    assert all(ps["brand_overlay"] == 0 for ps in active)
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        a, b = P(d1), P(d2)
        imaging.make_crops(str(src), a, "dish", 85, active)
        imaging.make_crops(str(src), b, "dish", 85, active, overlay=overlay)
        assert _hashes(a) == _hashes(b)


def test_brand_overlay_composites(client):
    """With brand_overlay=1 AND an overlay spec, the logo composites onto the
    crop; position + opacity honored. overlay=None on the same preset renders
    the untouched base (additive, never required)."""
    from pathlib import Path as P

    from app import imaging, presets

    db.run("""INSERT INTO crop_presets (slug, name, ratio_label, width, height,
                                        brand_overlay, sort)
              VALUES ('ov1','Overlay test','1:1',1000,1000,1,90)""")
    try:
        active = presets.active()
        src = P(tempfile.mkdtemp()) / "dish.jpg"
        src.write_bytes(_jpeg_bytes(2000, 1500))
        logo = P(tempfile.mkdtemp()) / "logo.png"
        logo.write_bytes(_logo_png(300, 150, (0, 200, 255, 255)))
        ov = lambda op: {
            "path": str(logo),
            "position": "br",
            "opacity": op,
            "scale_pct": 30,
            "margin_pct": 5,
        }

        with tempfile.TemporaryDirectory() as d:
            out = P(d)
            # overlay=None → base render, no logo anywhere
            imaging.make_crops(str(src), out, "base", 85, active)
            with Image.open(out / "base_ov1.jpg") as im:
                base_br = im.getpixel((800, 870))
                base_tl = im.getpixel((50, 50))

            # opacity 100 at br → logo colour bottom-right, top-left untouched
            imaging.make_crops(str(src), out, "full", 85, active, overlay=ov(100))
            with Image.open(out / "full_ov1.jpg") as im:
                full_br = im.getpixel((800, 870))
                full_tl = im.getpixel((50, 50))
            assert _close(full_tl, base_tl)  # outside the logo: unchanged
            assert _close(full_br, (0, 200, 255), 40)  # logo colour composited at br
            assert not _close(full_br, base_br, 40)  # br genuinely changed vs base

            # opacity 50 → br is a blend, distinct from full-opacity (opacity honored)
            imaging.make_crops(str(src), out, "half", 85, active, overlay=ov(50))
            with Image.open(out / "half_ov1.jpg") as im:
                half_br = im.getpixel((800, 870))
            assert not _close(half_br, full_br, 25)
    finally:
        db.run("DELETE FROM crop_presets WHERE slug='ov1'")


def test_overlay_contrast_scrim(client):
    """The brand overlay carries a contrast scrim: a soft dark halo derived from
    the logo's own alpha, composited UNDER the logo so a light wordmark stays
    legible on a bright dish. Detect it where the logo itself can't reach — a
    band just below the logo's lower edge is darkened by the halo, and that
    darkened pixel is NOT the logo colour (proving it's the scrim, not the mark)."""
    from pathlib import Path as P

    from app import imaging, presets

    db.run("""INSERT INTO crop_presets (slug, name, ratio_label, width, height,
                                        brand_overlay, sort)
              VALUES ('scrim1','Scrim test','1:1',1000,1000,1,91)""")
    try:
        active = [ps for ps in presets.active() if ps["slug"] == "scrim1"]
        src = P(tempfile.mkdtemp()) / "dish.jpg"
        src.write_bytes(_jpeg_bytes(2000, 1500))
        logo = P(tempfile.mkdtemp()) / "logo.png"
        logo.write_bytes(_logo_png(300, 150, (0, 200, 255, 255)))
        ov = {"path": str(logo), "position": "br", "opacity": 100, "scale_pct": 30, "margin_pct": 5}
        # logo lands at x:650..950, y:800..950; the scrim halo spills a few px
        # past the lower edge. Scan the band just below: scrim region, no logo.
        band = [(800, y) for y in range(951, 970)]
        with tempfile.TemporaryDirectory() as d:
            out = P(d)
            imaging.make_crops(str(src), out, "base", 85, active)
            with Image.open(out / "base_scrim1.jpg") as im:
                base = im.getpixel((800, 500))  # uniform crop
            imaging.make_crops(str(src), out, "scrim", 85, active, overlay=ov)
            with Image.open(out / "scrim_scrim1.jpg") as im:
                darkest = min((im.getpixel(p) for p in band), key=sum)
        assert sum(darkest) < sum(base) - 60  # halo darkened the bare crop
        assert not _close(darkest, (0, 200, 255), 50)  # it's the scrim, not the logo
    finally:
        db.run("DELETE FROM crop_presets WHERE slug='scrim1'")


def test_brand_kit_admin(admin):
    """Admin kit model: raster-only upload, placement params persisted, the
    newest active kit resolves via brand_kits.overlay_for_client, scoped serve."""
    from app import brand_kits as bk

    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)", ("Kit Co", "kit@example.com"))

    # non-raster logo rejected (EPS can't composite onto a JPEG)
    r = admin.post(
        f"/admin/studio/clients/{cid}/kits",
        files={"logo": ("logo.eps", b"%!PS", "application/postscript")},
        data={"position": "br", "opacity": 100, "scale_pct": 22, "margin_pct": 4},
        follow_redirects=False,
    )
    assert r.status_code == 415

    # PNG accepted with placement params
    r = admin.post(
        f"/admin/studio/clients/{cid}/kits",
        files={"logo": ("logo.png", _logo_png(120, 60), "image/png")},
        data={
            "label": "Primary",
            "position": "tl",
            "opacity": 80,
            "scale_pct": 30,
            "margin_pct": 6,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    kit = db.one("SELECT * FROM brand_kits WHERE client_id=?", (cid,))
    assert kit["position"] == "tl" and kit["opacity"] == 80 and kit["active"] == 1

    # resolver hands the render path a plain spec dict
    spec = bk.overlay_for_client(cid)
    assert spec["position"] == "tl" and spec["scale_pct"] == 30
    assert os.path.isfile(spec["path"])

    # scoped serve: right client 200, wrong client 404
    assert admin.get(f"/admin/studio/clients/{cid}/kits/{kit['id']}/logo").status_code == 200
    assert admin.get(f"/admin/studio/clients/{cid + 9999}/kits/{kit['id']}/logo").status_code == 404

    # deactivate → resolver returns None (additive, never required)
    admin.post(
        f"/admin/studio/clients/{cid}/kits/{kit['id']}",
        data={"position": "tl", "opacity": 80, "scale_pct": 30, "margin_pct": 6, "active": 0},
        follow_redirects=False,
    )
    assert bk.overlay_for_client(cid) is None


def test_client_tree_cycle_guards(admin):
    """set-parent route enforces both cycle guards: A->A and A->B->A are
    rejected, and a legitimate parent assignment is accepted."""
    from app import clients as ch

    a = db.run("INSERT INTO clients (name) VALUES (?)", ("Tree A",))
    b = db.run("INSERT INTO clients (name) VALUES (?)", ("Tree B",))

    # A->A: a client cannot be its own parent (422; DB CHECK is the backstop)
    r = admin.post(
        f"/admin/studio/clients/{a}/parent", data={"parent_id": str(a)}, follow_redirects=False
    )
    assert r.status_code == 422

    # legitimate: B under A
    r = admin.post(
        f"/admin/studio/clients/{b}/parent", data={"parent_id": str(a)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert db.one("SELECT parent_id FROM clients WHERE id=?", (b,))["parent_id"] == a

    # A->B->A: A cannot adopt B as parent now that B is A's descendant (422)
    r = admin.post(
        f"/admin/studio/clients/{a}/parent", data={"parent_id": str(b)}, follow_redirects=False
    )
    assert r.status_code == 422
    assert db.one("SELECT parent_id FROM clients WHERE id=?", (a,))["parent_id"] is None

    # helper walks agree
    assert ch.ancestor_ids(b) == [a]
    assert ch.descendant_ids(a) == [b]

    # detach (empty parent_id clears it)
    r = admin.post(
        f"/admin/studio/clients/{b}/parent", data={"parent_id": ""}, follow_redirects=False
    )
    assert r.status_code == 303
    assert db.one("SELECT parent_id FROM clients WHERE id=?", (b,))["parent_id"] is None


def test_client_delete_child_blocker(admin):
    """A parent with children cannot be deleted — not even with force=1.
    Restructuring must go through set-parent, never a delete side-effect."""
    parent = db.run("INSERT INTO clients (name) VALUES (?)", ("Del Group",))
    child = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("Del Venue", parent))

    # plain delete refused
    r = admin.post(f"/admin/studio/clients/{parent}/delete", data={}, follow_redirects=False)
    assert r.status_code == 400
    # force=1 STILL refused (hard blocker)
    r = admin.post(
        f"/admin/studio/clients/{parent}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert r.status_code == 400
    assert db.one("SELECT id FROM clients WHERE id=?", (parent,)) is not None

    # detach the child, then the parent deletes cleanly
    admin.post(
        f"/admin/studio/clients/{child}/parent", data={"parent_id": ""}, follow_redirects=False
    )
    r = admin.post(f"/admin/studio/clients/{parent}/delete", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (parent,)) is None


def test_brand_kit_cascade_nearest_ancestor(admin):
    """3-level tree group->region->venue: the venue inherits the NEAREST active
    ancestor's kit, not the root's. This is what depth-ordered ancestor_ids buys
    us — a 2-level test could not tell nearest from root."""
    from app import brand_kits as bk

    group = db.run("INSERT INTO clients (name) VALUES (?)", ("Casa Group",))
    region = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("Casa West", group))
    venue = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("Casa Downtown", region))

    # group kit at top-left, region kit at bottom-right; venue has none
    admin.post(
        f"/admin/studio/clients/{group}/kits",
        files={"logo": ("g.png", _logo_png(100, 50), "image/png")},
        data={"position": "tl", "opacity": 100, "scale_pct": 20, "margin_pct": 4},
        follow_redirects=False,
    )
    admin.post(
        f"/admin/studio/clients/{region}/kits",
        files={"logo": ("r.png", _logo_png(100, 50), "image/png")},
        data={"position": "br", "opacity": 100, "scale_pct": 20, "margin_pct": 4},
        follow_redirects=False,
    )

    # venue resolves the REGION kit (nearest), not the group's
    spec = bk.overlay_for_client(venue)
    assert spec is not None and spec["position"] == "br"
    assert f"/{region}/" in spec["path"]  # file resolved under the owning client

    # deactivate region's kit → venue falls back to the GROUP kit (next ancestor)
    rk = db.one("SELECT id FROM brand_kits WHERE client_id=?", (region,))
    admin.post(
        f"/admin/studio/clients/{region}/kits/{rk['id']}",
        data={"position": "br", "opacity": 100, "scale_pct": 20, "margin_pct": 4, "active": 0},
        follow_redirects=False,
    )
    spec = bk.overlay_for_client(venue)
    assert spec is not None and spec["position"] == "tl"
    assert f"/{group}/" in spec["path"]

    # a venue with its OWN active kit prefers it over any ancestor
    admin.post(
        f"/admin/studio/clients/{venue}/kits",
        files={"logo": ("v.png", _logo_png(100, 50), "image/png")},
        data={"position": "c", "opacity": 100, "scale_pct": 20, "margin_pct": 4},
        follow_redirects=False,
    )
    spec = bk.overlay_for_client(venue)
    assert spec is not None and spec["position"] == "c"
    assert f"/{venue}/" in spec["path"]


def test_delete_confirm_onsubmit_well_formed(admin):
    """The delete-client confirm() guard lives in a data-confirm attribute
    (dispatched by /static/behaviors.js — no inline JS under the nonce'd CSP).
    Jinja autoescaping must keep a hostile client name (apostrophe +
    double-quote + ampersand) inside the attribute: html.unescape of the
    attribute value has to round-trip the exact message, and the value must
    never terminate the attribute early (the pre-CSP bug this test was born
    from). An irreversible delete keeps its guard."""
    import html as html_mod

    nasty = 'O\'Brien "Smoke" & Oak'
    cid = db.run("INSERT INTO clients (name) VALUES (?)", (nasty,))

    def assert_intact(page, must_contain):
        # exactly one delete-client guard on the page, on the delete form
        m = re.findall(
            r'data-confirm="([^"]*)"[^>]*>\s*(?:<input[^>]*>\s*)?<button class="link danger">Delete client',
            page,
        )
        assert len(m) == 1, "delete-client form must carry exactly one data-confirm"
        msg = html_mod.unescape(m[0])
        assert must_contain in msg
        # no legacy inline handler may reappear — CSP would silently kill it
        assert "onsubmit=" not in page
        return msg

    # no blocker → "Delete <name>? This is final." (name carries the nasty chars)
    page = admin.get(f"/admin/studio/clients/{cid}").text
    msg = assert_intact(page, "This is final.")
    # the hostile characters round-trip intact through attribute escaping
    assert nasty in msg

    # with a child blocker → the WARNING summary, still well-formed
    child = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("Child Venue", cid))
    page = admin.get(f"/admin/studio/clients/{cid}").text
    assert_intact(page, "child client")
    assert 'name="force" value="1"' in page

    db.run("DELETE FROM clients WHERE id=?", (child,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))


def test_crop_preset_admin(admin):
    """Admin CRUD over crop_presets — the surface that makes the overlay engine
    and future delivery/print formats reachable without a DB edit. Every write
    lands an audit_log row (entity_type='crop_preset', R14: this table feeds the
    public render path); a no-op edit writes none (append-only stays clean); a
    slug with a path separator / space / quote is rejected cleanly, not 500'd;
    and slug is immutable on edit."""
    import json as _json

    # the list page renders with the seeded presets + the new nav link
    page = admin.get("/admin/studio/presets")
    assert page.status_code == 200
    assert "Crop presets" in page.text and "1x1" in page.text

    # add a new preset → row persisted with seeded defaults + a 'create' audit row
    r = admin.post(
        "/admin/studio/presets",
        data={
            "slug": "3x4test",
            "name": "Tall (3:4)",
            "ratio_label": "3:4",
            "width": "1200",
            "height": "1600",
            "centering_x": "0.5",
            "centering_y": "0.4",
            "target_channel": "pinterest",
            "sort": "50",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    ps = db.one("SELECT * FROM crop_presets WHERE slug='3x4test'")
    assert ps and ps["width"] == 1200 and ps["height"] == 1600
    assert ps["active"] == 1 and ps["brand_overlay"] == 0  # schema-seeded defaults
    assert (
        len(
            db.all_(
                """SELECT 1 FROM audit_log WHERE entity_type='crop_preset'
                          AND entity_id=? AND action='create'""",
                (ps["id"],),
            )
        )
        == 1
    )

    # bad slugs rejected cleanly (400, not a 500) and never persisted. The slug
    # is a filename key + URL token, so a path separator is the dangerous case.
    # (uppercase is normalized to lowercase, not rejected — so it's not here)
    for s in ["../etc", "a/b", "has space", 'quo"te', "dot.ted", ""]:
        rr = admin.post(
            "/admin/studio/presets",
            data={"slug": s, "name": "x", "ratio_label": "1:1", "width": "100", "height": "100"},
            follow_redirects=False,
        )
        assert rr.status_code == 400, f"slug {s!r} must be rejected"
    assert not db.one("SELECT 1 FROM crop_presets WHERE slug='../etc'")

    # duplicate slug → clean 400, not a raw IntegrityError 500
    dup = admin.post(
        "/admin/studio/presets",
        data={
            "slug": "3x4test",
            "name": "dupe",
            "ratio_label": "3:4",
            "width": "1200",
            "height": "1600",
        },
        follow_redirects=False,
    )
    assert dup.status_code == 400

    # bad dimensions / centering rejected cleanly
    assert (
        admin.post(
            "/admin/studio/presets",
            data={"slug": "bad1", "name": "x", "ratio_label": "1:1", "width": "0", "height": "100"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            "/admin/studio/presets",
            data={
                "slug": "bad2",
                "name": "x",
                "ratio_label": "1:1",
                "width": "100",
                "height": "100",
                "centering_x": "2",
            },
            follow_redirects=False,
        ).status_code
        == 400
    )

    # edit with a real change → 'update' audit row with the diff; slug immutable
    # even if a slug field is smuggled into the form.
    r = admin.post(
        f"/admin/studio/presets/{ps['id']}",
        data={
            "slug": "hacked",
            "name": "Tall portrait",
            "ratio_label": "3:4",
            "width": "1200",
            "height": "1600",
            "centering_x": "0.5",
            "centering_y": "0.4",
            "target_channel": "pinterest",
            "sort": "55",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    ps2 = db.one("SELECT * FROM crop_presets WHERE id=?", (ps["id"],))
    assert ps2["slug"] == "3x4test"  # NOT 'hacked' — slug never updated
    assert ps2["name"] == "Tall portrait" and ps2["sort"] == 55
    upd = db.one(
        """SELECT diff_json FROM audit_log WHERE entity_type='crop_preset'
                    AND entity_id=? AND action='update' ORDER BY id DESC LIMIT 1""",
        (ps["id"],),
    )
    diff = _json.loads(upd["diff_json"])
    assert diff["name"] == ["Tall (3:4)", "Tall portrait"]
    assert diff["sort"] == [50, 55]
    assert "slug" not in diff  # slug isn't a tracked editable field

    # a no-op edit (identical values) writes NO new audit row
    before = db.one(
        """SELECT COUNT(*) AS n FROM audit_log
                       WHERE entity_type='crop_preset' AND entity_id=?""",
        (ps["id"],),
    )["n"]
    admin.post(
        f"/admin/studio/presets/{ps['id']}",
        data={
            "name": "Tall portrait",
            "ratio_label": "3:4",
            "width": "1200",
            "height": "1600",
            "centering_x": "0.5",
            "centering_y": "0.4",
            "target_channel": "pinterest",
            "sort": "55",
        },
        follow_redirects=False,
    )
    after = db.one(
        """SELECT COUNT(*) AS n FROM audit_log
                      WHERE entity_type='crop_preset' AND entity_id=?""",
        (ps["id"],),
    )["n"]
    assert after == before

    # overlay toggle flips the flag + lands its own audit row with the diff
    admin.post(f"/admin/studio/presets/{ps['id']}/overlay", follow_redirects=False)
    assert (
        db.one("SELECT brand_overlay FROM crop_presets WHERE id=?", (ps["id"],))["brand_overlay"]
        == 1
    )
    ov = db.one(
        """SELECT diff_json FROM audit_log WHERE entity_type='crop_preset'
                   AND entity_id=? AND action='overlay_change' ORDER BY id DESC LIMIT 1""",
        (ps["id"],),
    )
    assert _json.loads(ov["diff_json"])["brand_overlay"] == [0, 1]

    # active toggle flips + audit row; the trail (with the action) renders on the page
    admin.post(f"/admin/studio/presets/{ps['id']}/active", follow_redirects=False)
    assert db.one("SELECT active FROM crop_presets WHERE id=?", (ps["id"],))["active"] == 0
    assert "active_change" in admin.get("/admin/studio/presets").text

    # tidy up so the deactivate-invariant test below sees only the 3 seeded presets
    db.run("DELETE FROM crop_presets WHERE id=?", (ps["id"],))


def test_preset_deactivate_via_admin_holds_public_invariant(admin):
    """Slice D proved that deactivating a preset (via a direct DB write) makes
    portal.crop() 404 and drops it from crops_zip. This proves the SAME invariant
    when the deactivation comes through the NEW admin route — the admin must not
    be able to create a state that breaks the public render path, only a clean
    absence. (The route reads presets.active(); deactivation is the only off
    switch, there is no destructive delete.)"""
    from pathlib import Path as P

    from app import jobs, presets

    # Build a fully self-contained chain rather than leaning on suite-leftover
    # favorites (later delete-tests remove those): client → published gallery
    # linked to that client → a ready photo → a visitor who favorited it →
    # a published portal with a known PIN → a rendered crop on disk per preset.
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Crop Invariant Co",))
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, client_id, published) VALUES (?,?,?,?,1)",
        ("CropInvariantGal01", "Crop invariant shoot", "1234", cid),
    )
    stem = "cropinvariant0001"
    aid = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status) VALUES (?,?,?,?,?)",
        (gid, "photo", "plate.jpg", f"{stem}.jpg", "ready"),
    )
    vid = db.run(
        "INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (gid, "vtoken-crop-invariant")
    )
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, aid))
    db.run(
        "INSERT INTO portals (client_id, slug, pin, published) VALUES (?,?,?,1)",
        (cid, "CropInvariantPortal01", "4321"),
    )
    p = db.one("SELECT * FROM portals WHERE slug='CropInvariantPortal01'")
    a = db.one("SELECT * FROM assets WHERE id=?", (aid,))

    # write a dummy rendered crop on disk for every active preset slug
    crops = jobs.crops_dir(gid)
    crops.mkdir(parents=True, exist_ok=True)
    active = presets.active()
    assert len(active) >= 2, "need >=2 active presets to prove surgical exclusion"
    for ps in active:
        (crops / f"{stem}_{ps['slug']}.jpg").write_bytes(_jpeg_bytes(64, 64))
    target = active[0]
    slug = target["slug"]

    with TestClient(app) as pub:
        pub.post(f"/portal/{p['slug']}/pin", data={"pin": p["pin"]}, follow_redirects=False)
        # active: the crop resolves and the zip bundles it
        assert pub.get(f"/portal/{p['slug']}/crop/{a['id']}/{slug}").status_code == 200
        z = pub.get(f"/portal/{p['slug']}/crops.zip")
        assert z.status_code == 200
        before = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        assert any(n.endswith(f"_{slug}.jpg") for n in before)

        # deactivate THROUGH THE ADMIN ROUTE (not a db.run UPDATE)
        r = admin.post(f"/admin/studio/presets/{target['id']}/active", follow_redirects=False)
        assert r.status_code == 303
        assert db.one("SELECT active FROM crop_presets WHERE id=?", (target["id"],))["active"] == 0

        # public path now refuses the slug cleanly and drops it from the zip,
        # while other active presets stay bundled (surgical, not all-or-nothing)
        assert pub.get(f"/portal/{p['slug']}/crop/{a['id']}/{slug}").status_code == 404
        z2 = pub.get(f"/portal/{p['slug']}/crops.zip")
        assert z2.status_code == 200
        after = zipfile.ZipFile(io.BytesIO(z2.content)).namelist()
        assert not any(n.endswith(f"_{slug}.jpg") for n in after)
        assert after, "other active presets should still bundle"

        # reactivate via admin → resolves again; no destructive state was created
        admin.post(f"/admin/studio/presets/{target['id']}/active", follow_redirects=False)
        assert pub.get(f"/portal/{p['slug']}/crop/{a['id']}/{slug}").status_code == 200


def test_client_children_roster(admin):
    """Read-only group->venue roster on the client page: a group with venues
    under it lists every descendant (top-down), while a childless client renders
    no roster at all (clean empty state). Completes the hierarchy — the parent
    selector is the venue->group direction, this is the inverse view."""
    group = db.run("INSERT INTO clients (name) VALUES (?)", ("Roster Group",))
    region = db.run(
        "INSERT INTO clients (name, company, parent_id) VALUES (?,?,?)",
        ("Roster West", "West Co", group),
    )
    venue = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)", ("Roster Bistro", region))
    lone = db.run("INSERT INTO clients (name) VALUES (?)", ("Roster Solo",))

    # the group's page lists BOTH descendants (region + grandchild venue)
    page = admin.get(f"/admin/studio/clients/{group}").text
    assert "Venues under this group" in page
    assert "Roster West" in page and "West Co" in page
    assert "Roster Bistro" in page
    assert f'href="/admin/studio/clients/{region}"' in page
    assert f'href="/admin/studio/clients/{venue}"' in page

    # a childless client renders no roster block whatsoever (clean empty state)
    solo = admin.get(f"/admin/studio/clients/{lone}").text
    assert "Venues under this group" not in solo

    # the leaf venue is also childless → no roster, even though it HAS a parent
    leaf = admin.get(f"/admin/studio/clients/{venue}").text
    assert "Venues under this group" not in leaf


def test_delivery_app_presets_are_data_not_code(admin):
    """The boundary thesis end-to-end: a new delivery channel (DoorDash, Uber
    Eats) is a new crop_presets ROW entered through the slice-5 admin UI, not new
    code. These rows render through the SAME imaging.make_crops as the seeded
    social ratios with zero render-path changes; sRGB/72dpi come from the schema
    defaults (the admin form doesn't touch them), brand_overlay stays off (a
    restaurant's own platform listing shouldn't carry the studio wordmark)."""
    from pathlib import Path as P

    from app import imaging, presets

    # real platform specs: DoorDash menu/detail hero is 16:9 (min 1400×800);
    # Uber Eats cover/hero is 5:4 at 2880×2304. Entered THROUGH THE ADMIN ROUTE.
    rows = [
        {
            "slug": "doordash",
            "name": "DoorDash hero (16:9)",
            "ratio_label": "16:9",
            "width": "1920",
            "height": "1080",
            "target_channel": "doordash",
            "sort": "40",
        },
        {
            "slug": "ubereats",
            "name": "Uber Eats cover (5:4)",
            "ratio_label": "5:4",
            "width": "2880",
            "height": "2304",
            "target_channel": "ubereats",
            "sort": "50",
        },
    ]
    try:
        for r in rows:
            assert (
                admin.post("/admin/studio/presets", data=r, follow_redirects=False).status_code
                == 303
            )

        dd = db.one("SELECT * FROM crop_presets WHERE slug='doordash'")
        ue = db.one("SELECT * FROM crop_presets WHERE slug='ubereats'")
        # schema defaults the admin UI never set — exactly the sRGB/72dpi spec,
        # no overlay (restaurant's own listing), active on creation
        for ps in (dd, ue):
            assert ps["color_space"] == "sRGB" and ps["dpi"] == 72
            assert ps["brand_overlay"] == 0 and ps["active"] == 1
        assert (dd["width"], dd["height"]) == (1920, 1080)
        assert (ue["width"], ue["height"]) == (2880, 2304)

        # they render through the EXISTING generic path, alongside the seeded
        # ratios, in one make_crops call — no per-channel branch, no code change
        src = P(tempfile.mkdtemp()) / "dish.jpg"
        src.write_bytes(_jpeg_bytes(3000, 2400))
        with tempfile.TemporaryDirectory() as d:
            out = P(d)
            written = imaging.make_crops(str(src), out, "dish", 85, presets.active())
            assert "dish_doordash.jpg" in written and "dish_ubereats.jpg" in written
            with Image.open(out / "dish_doordash.jpg") as im:
                assert im.size == (1920, 1080)  # 16:9, exact
            with Image.open(out / "dish_ubereats.jpg") as im:
                assert im.size == (2880, 2304)  # 5:4, exact
    finally:
        db.run("DELETE FROM crop_presets WHERE slug IN ('doordash','ubereats')")


def test_recurring_plan_draft_generation(admin):
    """Recurring billing slice 1: a plan is a template that GENERATES a draft
    invoice — it never sends or charges (manual-send doctrine intact). The plan
    keys off the calendar month; a second generate in the same period is a
    dedupe no-op so a double-click can't spawn duplicate invoices."""
    from app.admin.recurring import _period

    # fresh client + project so the plan list is isolated
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Retainer Co",
            "company": "Monthly Bites",
            "email": "ops@monthlybites.com",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Brand partner retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create plan from the project page form
    r = admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Monthly content retainer"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    assert plan["project_id"] == proj["id"] and plan["active"] == 1
    assert plan["total_cents"] == 0 and plan["last_run_period"] is None

    # project page lists the plan
    assert "Monthly content retainer" in admin.get(f"/admin/studio/projects/{proj['id']}").text

    # generating with a zero total is refused (nothing to bill)
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/generate", follow_redirects=False)
    assert r.status_code == 400

    # edit: line items + anchor day; total recalculates from the rows
    r = admin.post(
        f"/admin/studio/recurring/{plan['id']}",
        data={
            "title": "Monthly content retainer",
            "item_label_0": "Content day",
            "item_qty_0": "1",
            "item_price_0": "1200",
            "item_label_1": "Reels (3)",
            "item_qty_1": "3",
            "item_price_1": "150",
            "anchor_day": "5",
            "active": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    plan = db.one("SELECT * FROM recurring_plans WHERE id=?", (plan["id"],))
    assert plan["total_cents"] == 165000 and plan["anchor_day"] == 5

    # anchor day outside 1–28 is rejected
    assert (
        admin.post(
            f"/admin/studio/recurring/{plan['id']}",
            data={"title": "x", "anchor_day": "31"},
            follow_redirects=False,
        ).status_code
        == 400
    )

    # generate → a DRAFT invoice linked to the plan, period stamped
    period = _period()
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/generate", follow_redirects=False)
    assert r.status_code == 303
    inv = db.one(
        "SELECT * FROM invoices WHERE recurring_plan_id=? ORDER BY id DESC LIMIT 1", (plan["id"],)
    )
    assert inv is not None
    assert inv["status"] == "draft"  # manual-send preserved — nothing auto-sends
    assert inv["total_cents"] == 165000
    assert period in inv["title"]
    assert (
        db.one("SELECT last_run_period FROM recurring_plans WHERE id=?", (plan["id"],))[
            "last_run_period"
        ]
        == period
    )

    # second generate in the same period is a dedupe no-op (400), no new invoice
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/generate", follow_redirects=False)
    assert r.status_code == 400
    assert (
        db.one("SELECT COUNT(*) AS n FROM invoices WHERE recurring_plan_id=?", (plan["id"],))["n"]
        == 1
    )

    # a paused plan refuses to generate
    db.run("UPDATE recurring_plans SET active=0, last_run_period=NULL WHERE id=?", (plan["id"],))
    assert (
        admin.post(
            f"/admin/studio/recurring/{plan['id']}/generate", follow_redirects=False
        ).status_code
        == 400
    )

    # soft-delete drops it from the project page but keeps the generated invoice
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert (
        db.one("SELECT deleted_at FROM recurring_plans WHERE id=?", (plan["id"],))["deleted_at"]
        is not None
    )
    # the plan's own row link is gone (its name lingers only in the kept
    # invoice's title, which is expected — generated invoices survive)
    assert (
        f"/admin/studio/recurring/{plan['id']}"
        not in admin.get(f"/admin/studio/projects/{proj['id']}").text
    )
    assert (
        db.one("SELECT COUNT(*) AS n FROM invoices WHERE recurring_plan_id=?", (plan["id"],))["n"]
        == 1
    )


def test_recurring_scheduler_sweep(admin):
    """Slice 2 — the in-process scheduler: on the anchor day each month Mise
    auto-generates that period's DRAFT with no click (drafts only, manual-send
    doctrine intact). The sweep is date-driven (run_due_plans(today=...)) and
    idempotent — the last_run_period claim means a second sweep the same month,
    or an overlapping manual click, can never double-bill."""
    import datetime as dt

    from app.admin import recurring

    admin.post(
        "/admin/studio/clients",
        data={"name": "Sweep Diner", "company": "Sweep Co", "email": "ops@sweep.co", "phone": ""},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Sweep retainer"},
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/recurring/{plan['id']}",
        data={
            "title": "Sweep retainer",
            "item_label_0": "Content day",
            "item_qty_0": "1",
            "item_price_0": "1000",
            "anchor_day": "10",
            "active": "1",
        },
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans WHERE id=?", (plan["id"],))
    assert plan["total_cents"] == 100000 and plan["anchor_day"] == 10

    def invcount():
        return db.one(
            "SELECT COUNT(*) AS n FROM invoices WHERE recurring_plan_id=?", (plan["id"],)
        )["n"]

    # before the anchor day → the plan isn't due yet, nothing generated
    recurring.run_due_plans(today=dt.date(2026, 9, 9))
    assert invcount() == 0

    # on the anchor day → exactly one DRAFT, period stamped
    recurring.run_due_plans(today=dt.date(2026, 9, 10))
    assert invcount() == 1
    inv = db.one(
        "SELECT * FROM invoices WHERE recurring_plan_id=? ORDER BY id DESC LIMIT 1", (plan["id"],)
    )
    assert inv["status"] == "draft" and "2026-09" in inv["title"]
    assert (
        db.one("SELECT last_run_period FROM recurring_plans WHERE id=?", (plan["id"],))[
            "last_run_period"
        ]
        == "2026-09"
    )

    # a later sweep the SAME month is a no-op — the period claim dedupes
    recurring.run_due_plans(today=dt.date(2026, 9, 25))
    assert invcount() == 1

    # next month → a fresh draft
    recurring.run_due_plans(today=dt.date(2026, 10, 12))
    assert invcount() == 2
    assert (
        "2026-10"
        in db.one(
            "SELECT title FROM invoices WHERE recurring_plan_id=? ORDER BY id DESC LIMIT 1",
            (plan["id"],),
        )["title"]
    )

    # paused → the sweep skips it entirely
    db.run("UPDATE recurring_plans SET active=0 WHERE id=?", (plan["id"],))
    recurring.run_due_plans(today=dt.date(2026, 11, 15))
    assert invcount() == 2

    # everything the sweep made is a DRAFT — it never sends or charges
    assert all(
        r["status"] == "draft"
        for r in db.all_("SELECT status FROM invoices WHERE recurring_plan_id=?", (plan["id"],))
    )

    # leave no active plan lingering in the shared DB for later modules
    db.run("UPDATE recurring_plans SET deleted_at=datetime('now') WHERE id=?", (plan["id"],))


def test_retainer_draft_waiting_strip(admin):
    """Slice 3 — the manual-send safety valve: once slice 2's scheduler can
    create retainer drafts unattended, those drafts must not rot unsent. The
    studio dashboard surfaces a 'Retainer drafts waiting to send' strip listing
    every unsent recurring-plan draft; sending one removes it from the strip."""
    import datetime as dt

    from app.admin import recurring

    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Waiting Diner",
            "company": "Waiting Co",
            "email": "ops@waiting.co",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Waiting retainer"},
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/recurring/{plan['id']}",
        data={
            "title": "Waiting retainer",
            "item_label_0": "Content day",
            "item_qty_0": "1",
            "item_price_0": "1000",
            "anchor_day": "10",
            "active": "1",
        },
        follow_redirects=False,
    )

    # this plan hasn't generated anything yet → its invoice isn't waiting.
    # (Earlier recurring tests leave their own drafts in the shared DB, so we
    # assert on THIS plan's invoice, not the strip's global presence.)
    assert (
        db.one("SELECT COUNT(*) AS n FROM invoices WHERE recurring_plan_id=?", (plan["id"],))["n"]
        == 0
    )

    # scheduler sweep makes a DRAFT unattended → it now nags on the dashboard
    recurring.run_due_plans(today=dt.date(2026, 9, 10))
    inv = db.one(
        "SELECT * FROM invoices WHERE recurring_plan_id=? ORDER BY id DESC LIMIT 1", (plan["id"],)
    )
    assert inv["status"] == "draft"
    page = admin.get("/admin/studio/activity").text
    assert "Retainer drafts waiting to send" in page
    assert f"/admin/studio/invoices/{inv['id']}" in page

    # Kevin reviews and Sends it → it drops off the waiting strip
    r = admin.post(f"/admin/studio/invoices/{inv['id']}/send", follow_redirects=False)
    assert r.status_code == 303
    page = admin.get("/admin/studio/activity").text
    assert f"/admin/studio/invoices/{inv['id']}" not in page

    # leave no active plan lingering in the shared DB for later modules
    db.run("UPDATE recurring_plans SET deleted_at=datetime('now') WHERE id=?", (plan["id"],))


def test_retainer_deliverable_quota(admin):
    """Domain G slice 1: a retainer commits to a monthly deliverable quota
    (labeled targets), and Kevin keeps a MANUAL per-period log of what was
    delivered. The plan page lines the log up against the quota as on-track/met.
    Encodes WHY: the quota is advisory content-tracking only — it never touches
    invoices/billing and is never auto-credited from galleries (Kevin's count, by
    doctrine), and the quota is a plan not a cap, so un-targeted deliveries still
    log (as 'extra') without being rejected."""
    import json

    from app.admin.recurring import _period

    admin.post(
        "/admin/studio/clients",
        data={"name": "Quota Kitchen", "company": "Quota Co", "email": "ops@quota.co", "phone": ""},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Brand partner retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Content retainer"},
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    assert plan["quota"] == "[]"  # default — no commitment until set

    # set the quota (labeled targets) alongside the billing line items; quota is
    # parsed independently and stored as JSON, leaving the invoice total alone.
    r = admin.post(
        f"/admin/studio/recurring/{plan['id']}",
        data={
            "title": "Content retainer",
            "item_label_0": "Content day",
            "item_qty_0": "1",
            "item_price_0": "1200",
            "anchor_day": "5",
            "active": "1",
            "quota_label_0": "Hero images",
            "quota_target_0": "20",
            "quota_label_1": "Reels",
            "quota_target_1": "4",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    plan = db.one("SELECT * FROM recurring_plans WHERE id=?", (plan["id"],))
    quota = json.loads(plan["quota"])
    assert quota == [{"label": "Hero images", "target": 20}, {"label": "Reels", "target": 4}]
    assert plan["total_cents"] == 120000  # quota did NOT bleed into billing

    period = _period()
    # nothing logged yet → full target outstanding
    page = admin.get(f"/admin/studio/recurring/{plan['id']}").text
    assert "Hero images" in page and "20 to go" in page

    # log a partial delivery this period → progress reflects it
    r = admin.post(
        f"/admin/studio/recurring/{plan['id']}/deliveries",
        data={"label": "Hero images", "qty": "5", "period": period, "note": "spring menu batch"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (
        db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (plan["id"],))["n"]
        == 1
    )
    page = admin.get(f"/admin/studio/recurring/{plan['id']}").text
    assert "15 to go" in page  # 20 target − 5 delivered

    # a second entry sums with the first; hitting the target reads "met"
    admin.post(
        f"/admin/studio/recurring/{plan['id']}/deliveries",
        data={"label": "Hero images", "qty": "15", "period": period},
        follow_redirects=False,
    )
    page = admin.get(f"/admin/studio/recurring/{plan['id']}").text
    assert "met" in page

    # an un-targeted label still logs (quota is a plan, not a cap) → shows 'extra'
    admin.post(
        f"/admin/studio/recurring/{plan['id']}/deliveries",
        data={"label": "Stories", "qty": "3", "period": period},
        follow_redirects=False,
    )
    page = admin.get(f"/admin/studio/recurring/{plan['id']}").text
    assert "Stories" in page and "extra" in page

    # bad inputs are rejected, not silently coerced
    assert (
        admin.post(
            f"/admin/studio/recurring/{plan['id']}/deliveries",
            data={"label": "Hero images", "qty": "0", "period": period},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/studio/recurring/{plan['id']}/deliveries",
            data={"label": "Hero images", "qty": "1", "period": "nope"},
            follow_redirects=False,
        ).status_code
        == 400
    )

    # delete one entry → it leaves the log
    e = db.one(
        "SELECT id FROM retainer_deliveries WHERE plan_id=? AND label='Stories'", (plan["id"],)
    )
    r = admin.post(
        f"/admin/studio/recurring/{plan['id']}/deliveries/{e['id']}/delete", follow_redirects=False
    )
    assert r.status_code == 303
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=? AND label='Stories'",
            (plan["id"],),
        )["n"]
        == 0
    )

    # CASCADE: a HARD plan delete cascades the deliveries (FK ON DELETE CASCADE).
    db.run("DELETE FROM recurring_plans WHERE id=?", (plan["id"],))
    assert (
        db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (plan["id"],))["n"]
        == 0
    )


def test_retainer_behind_quota_strip(admin):
    """Domain G slice 2 — the pace-aware 'behind quota' dashboard strip. A retainer
    surfaces only when its this-period delivery lags the month's run-rate (a label
    is behind when delivered < target × fraction-of-month-elapsed). Encodes WHY two
    date-independent edges hold regardless of the day the test runs: a quota with
    NOTHING delivered is always behind pace (0 < target×elapsed for any day ≥1), so
    it always shows; a FULLY delivered quota is never behind (done == target ≥
    target×elapsed even on the last day), so it stays silent — the strip nags about
    real risk, not about every retainer at the start of the month."""
    from app.admin.recurring import _period

    def mk_plan(client_name, plan_title, quota_label, target):
        admin.post(
            "/admin/studio/clients",
            data={"name": client_name, "company": client_name + " Co", "email": "", "phone": ""},
            follow_redirects=False,
        )
        c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/clients/{c['id']}/projects",
            data={"title": "Retainer"},
            follow_redirects=False,
        )
        proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/projects/{proj['id']}/recurring",
            data={"title": plan_title},
            follow_redirects=False,
        )
        plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/recurring/{plan['id']}",
            data={
                "title": plan_title,
                "item_label_0": "Content day",
                "item_qty_0": "1",
                "item_price_0": "1000",
                "anchor_day": "5",
                "active": "1",
                "quota_label_0": quota_label,
                "quota_target_0": str(target),
            },
            follow_redirects=False,
        )
        return plan["id"]

    period = _period()
    behind_id = mk_plan("Behind Bistro", "Behind retainer", "Hero images", 20)
    ontrack_id = mk_plan("Ontrack Oyster", "Ontrack retainer", "Reels", 4)
    # fully deliver the on-track plan's quota → never behind pace, any day
    admin.post(
        f"/admin/studio/recurring/{ontrack_id}/deliveries",
        data={"label": "Reels", "qty": "4", "period": period},
        follow_redirects=False,
    )

    page = admin.get("/admin/studio/activity").text
    assert "Retainers behind quota" in page
    # the un-delivered retainer is on the strip with its worst-label gap
    assert f"/admin/studio/recurring/{behind_id}" in page
    assert "Hero images" in page and "20 to go" in page
    # the fully-delivered retainer is NOT (met its pace)
    assert f"/admin/studio/recurring/{ontrack_id}" not in page

    # delivering enough to clear the run-rate drops it off the strip. On the last
    # day of the month elapsed==1.0, so only a FULL delivery is guaranteed to clear
    # pace on every possible run date — deliver the whole target.
    admin.post(
        f"/admin/studio/recurring/{behind_id}/deliveries",
        data={"label": "Hero images", "qty": "20", "period": period},
        follow_redirects=False,
    )
    page = admin.get("/admin/studio/activity").text
    assert f"/admin/studio/recurring/{behind_id}" not in page

    # a PAUSED behind plan never nags (you chose to stop the retainer)
    paused_id = mk_plan("Paused Pub", "Paused retainer", "Stories", 10)
    db.run("UPDATE recurring_plans SET active=0 WHERE id=?", (paused_id,))
    page = admin.get("/admin/studio/activity").text
    assert f"/admin/studio/recurring/{paused_id}" not in page

    # clean up: hard-delete the plans so no active quota plan lingers for later modules
    for pid in (behind_id, ontrack_id, paused_id):
        db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))


def test_retainer_content_calendar(admin):
    """Domain G slice 3 — a forward-looking content calendar on a retainer plan.
    Dated slots (label + optional title/note) move planned → shot → delivered. The
    plan page shows only THIS period's slots. Encodes WHY the calendar is DECOUPLED
    from the slice-1 delivery log: advancing a slot to 'delivered' is purely a
    planning state and must NOT touch retainer_deliveries (the quota count stays
    Kevin's manual log, by doctrine). Bad date / blank label are rejected, a hard
    plan delete cascades the calendar (FK ON DELETE CASCADE)."""
    from app.admin.recurring import _period

    admin.post(
        "/admin/studio/clients",
        data={"name": "Calendar Cafe", "company": "Cal Co", "email": "", "phone": ""},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Content retainer"},
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]

    period = _period()  # 'YYYY-MM'
    this_period_date = f"{period}-15"

    # add a slot in the current period → it renders on the calendar
    r = admin.post(
        f"/admin/studio/recurring/{pid}/calendar",
        data={
            "slot_date": this_period_date,
            "label": "Hero images",
            "title": "Spring menu hero",
            "note": "pasta close-up",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    slot = db.one("SELECT * FROM content_calendar WHERE plan_id=? ORDER BY id DESC LIMIT 1", (pid,))
    assert slot["status"] == "planned"  # default
    page = admin.get(f"/admin/studio/recurring/{pid}").text
    assert "Spring menu hero" in page and this_period_date in page

    # a slot dated outside this period is NOT shown on the period view
    admin.post(
        f"/admin/studio/recurring/{pid}/calendar",
        data={"slot_date": "2099-01-10", "label": "Reels"},
        follow_redirects=False,
    )
    page = admin.get(f"/admin/studio/recurring/{pid}").text
    assert "2099-01-10" not in page

    # advance status → reflected, and the delivery log is UNTOUCHED (decoupled)
    r = admin.post(
        f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
        data={"status": "delivered"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (
        db.one("SELECT status FROM content_calendar WHERE id=?", (slot["id"],))["status"]
        == "delivered"
    )
    assert (
        db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0
    )  # marking delivered did NOT auto-log a delivery

    # bad inputs rejected, not coerced
    assert (
        admin.post(
            f"/admin/studio/recurring/{pid}/calendar",
            data={"slot_date": "nope", "label": "Hero images"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/studio/recurring/{pid}/calendar",
            data={"slot_date": this_period_date, "label": "  "},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
            data={"status": "shipped"},
            follow_redirects=False,
        ).status_code
        == 400
    )

    # delete a slot → it leaves the calendar
    r = admin.post(
        f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/delete", follow_redirects=False
    )
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) AS n FROM content_calendar WHERE id=?", (slot["id"],))["n"] == 0

    # CASCADE: a hard plan delete cascades the calendar slots
    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))
    assert db.one("SELECT COUNT(*) AS n FROM content_calendar WHERE plan_id=?", (pid,))["n"] == 0


def test_retainer_assisted_credit_prefill(admin):
    """Domain G slice 4 — assisted-credit pre-fill closes the forget-to-log hole
    WITHOUT auto-crediting. Flipping a calendar slot to 'delivered' redirects with
    credit_* query params that seed the delivery-log form (label, qty=1, period from
    the slot date); the human still submits. Encodes WHY two invariants must hold:
    (a) the slot→delivered transition itself writes NO retainer_deliveries row — the
    slice-3 decoupling guarantee survives; (b) the pre-fill carries the right
    label/qty/period but performs no write on its own (only the existing /deliveries
    POST writes). Date-independent: period is derived from _period() and the slot date
    is built from it, so the assertions don't depend on the calendar day."""
    from urllib.parse import parse_qs, urlparse

    from app.admin.recurring import _period

    admin.post(
        "/admin/studio/clients",
        data={"name": "Credit Counter", "company": "Credit Co", "email": "", "phone": ""},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Content retainer"},
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]

    period = _period()  # 'YYYY-MM'
    slot_date = f"{period}-09"
    admin.post(
        f"/admin/studio/recurring/{pid}/calendar",
        data={"slot_date": slot_date, "label": "Hero images"},
        follow_redirects=False,
    )
    slot = db.one("SELECT * FROM content_calendar WHERE plan_id=? ORDER BY id DESC LIMIT 1", (pid,))

    # flip planned → delivered: 303 redirect carries the pre-fill query params
    r = admin.post(
        f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
        data={"status": "delivered"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["credit_label"] == ["Hero images"]
    assert q["credit_qty"] == ["1"]
    assert q["credit_period"] == [period]  # substr(slot_date,1,7)
    # INVARIANT (a): the transition wrote NO delivery row — decoupling preserved
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0

    # INVARIANT (b): rendering the pre-filled page injects the values as form
    # DEFAULTS and still writes nothing on its own
    page = admin.get(
        f"/admin/studio/recurring/{pid}",
        params={"credit_label": "Hero images", "credit_qty": "1", "credit_period": period},
    ).text
    assert 'value="Hero images"' in page  # label seeded
    assert f'value="{period}"' in page  # period seeded
    assert (
        db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0
    )  # GET is not a write

    # the pre-fill fires ONLY on a transition into delivered, not on a re-save:
    # delivered → delivered carries no credit params
    r2 = admin.post(
        f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
        data={"status": "delivered"},
        follow_redirects=False,
    )
    assert "credit_label" not in r2.headers["location"]
    # nor on a non-delivered transition (planned/shot)
    admin.post(
        f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
        data={"status": "shot"},
        follow_redirects=False,
    )
    r3 = admin.post(
        f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
        data={"status": "shot"},
        follow_redirects=False,
    )
    assert "credit_label" not in r3.headers["location"]

    # the human submit is the ONLY thing that writes the count
    admin.post(
        f"/admin/studio/recurring/{pid}/deliveries",
        data={"label": "Hero images", "qty": "1", "period": period},
        follow_redirects=False,
    )
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 1

    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))


def test_content_due_strip(admin, monkeypatch):
    """Domain G slice 5 — the 'Content due' dashboard strip: calendar slots
    scheduled this period and not yet delivered (the 'what's coming' companion to
    behind-quota's 'what's at risk'). Encodes WHY each edge holds: a planned/shot
    in-period slot appears; a DELIVERED slot drops off (composes with slice-4's
    assisted credit — flipping to delivered clears it here); an OVERDUE slot
    (slot_date < today, not delivered) appears flagged urgent (most actionable, never
    hidden); empty ⇒ the strip is silent. This strip is date-WINDOWED, so today is
    pinned to a fixed date — the assertions can't flake by calendar day (stronger
    than relative dates)."""
    import datetime as _dt
    import types

    from app.admin import studio

    class _FixedDate(_dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 15)

    monkeypatch.setattr(
        studio,
        "dt",
        types.SimpleNamespace(
            date=_FixedDate, datetime=_dt.datetime, timezone=_dt.timezone, timedelta=_dt.timedelta
        ),
    )
    period = "2026-06"

    def mk_plan(client_name, slot_date, status="planned"):
        admin.post(
            "/admin/studio/clients",
            data={"name": client_name, "company": client_name + " Co", "email": "", "phone": ""},
            follow_redirects=False,
        )
        c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/clients/{c['id']}/projects",
            data={"title": "Retainer"},
            follow_redirects=False,
        )
        proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/projects/{proj['id']}/recurring",
            data={"title": client_name + " retainer"},
            follow_redirects=False,
        )
        plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/recurring/{plan['id']}/calendar",
            data={"slot_date": slot_date, "label": "Hero images"},
            follow_redirects=False,
        )
        slot = db.one(
            "SELECT * FROM content_calendar WHERE plan_id=? ORDER BY id DESC LIMIT 1", (plan["id"],)
        )
        if status != "planned":
            admin.post(
                f"/admin/studio/recurring/{plan['id']}/calendar/{slot['id']}/status",
                data={"status": status},
                follow_redirects=False,
            )
        return plan["id"]

    # future in-period (not overdue), overdue (past in-period), delivered (dropped)
    due_id = mk_plan("Due Diner", f"{period}-25")
    overdue_id = mk_plan("Overdue Oven", f"{period}-05")
    delivered_id = mk_plan("Done Deli", f"{period}-20", status="delivered")

    page = admin.get("/admin/studio/activity").text
    assert "Content due" in page
    # planned/shot in-period slots appear, linking to the plan's #calendar anchor
    assert f"/admin/studio/recurring/{due_id}#calendar" in page
    assert f"/admin/studio/recurring/{overdue_id}#calendar" in page
    # the overdue chip is flagged overdue in its when-label (specific to the chip
    # text, not the upcoming-overdue CSS class)
    assert ">overdue</span>" in page
    # a delivered slot drops off the strip (composes with slice-4 assisted credit)
    assert f"/admin/studio/recurring/{delivered_id}#calendar" not in page

    # marking the remaining slots delivered empties the strip → it goes silent
    for pid_ in (due_id, overdue_id):
        s = db.one("SELECT id FROM content_calendar WHERE plan_id=? ORDER BY id LIMIT 1", (pid_,))
        admin.post(
            f"/admin/studio/recurring/{pid_}/calendar/{s['id']}/status",
            data={"status": "delivered"},
            follow_redirects=False,
        )
    page = admin.get("/admin/studio/activity").text
    assert "Content due" not in page  # silent when empty

    # clean up: hard-delete the plans (cascades their calendar slots)
    for pid_ in (due_id, overdue_id, delivered_id):
        db.run("DELETE FROM recurring_plans WHERE id=?", (pid_,))


def test_content_due_carries_overdue_across_period_rollover(admin, monkeypatch):
    """Overdue-rollover VISIBILITY fix (Domain G, read-only): an undelivered content
    slot from a PRIOR period must NOT vanish when the month rolls over — it stays on
    the Content-due strip as overdue until it's delivered. Encodes WHY: a shoot the
    studio still owes a client doesn't disappear just because the calendar turned;
    the old `substr(slot_date,1,7)=period` filter silently dropped it on the 1st of
    the next month. Date-WINDOWED so today is pinned. The companion guarantee — that
    FUTURE-period slots stay hidden (look-ahead is still period-bounded) — is asserted
    too, so the fix is carryover-only and didn't accidentally widen the window."""
    import datetime as _dt
    import types

    from app.admin import studio

    class _FixedDate(_dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 15)

    monkeypatch.setattr(
        studio,
        "dt",
        types.SimpleNamespace(
            date=_FixedDate, datetime=_dt.datetime, timezone=_dt.timezone, timedelta=_dt.timedelta
        ),
    )

    def mk_plan(client_name, slot_date, status="planned"):
        admin.post(
            "/admin/studio/clients",
            data={"name": client_name, "company": client_name + " Co", "email": "", "phone": ""},
            follow_redirects=False,
        )
        c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/clients/{c['id']}/projects",
            data={"title": "Retainer"},
            follow_redirects=False,
        )
        proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/projects/{proj['id']}/recurring",
            data={"title": client_name + " retainer"},
            follow_redirects=False,
        )
        plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
        admin.post(
            f"/admin/studio/recurring/{plan['id']}/calendar",
            data={"slot_date": slot_date, "label": "Hero images"},
            follow_redirects=False,
        )
        if status != "planned":
            slot = db.one(
                "SELECT id FROM content_calendar WHERE plan_id=? ORDER BY id DESC LIMIT 1",
                (plan["id"],),
            )
            admin.post(
                f"/admin/studio/recurring/{plan['id']}/calendar/{slot['id']}/status",
                data={"status": status},
                follow_redirects=False,
            )
        return plan["id"]

    # carried over from LAST month (undelivered), this month, and NEXT month
    carry_id = mk_plan("Carryover Cafe", "2026-05-28")  # prior period, still owed
    current_id = mk_plan("Current Counter", "2026-06-25")  # this period
    future_id = mk_plan("Future Fry", "2026-07-10")  # next period (look-ahead)

    page = admin.get("/admin/studio/activity").text
    # the carried-over prior-period slot STAYS visible (this is the fix) and reads overdue
    assert f"/admin/studio/recurring/{carry_id}#calendar" in page
    # current-period slot still shows (unchanged behavior)
    assert f"/admin/studio/recurring/{current_id}#calendar" in page
    # future-period slot stays hidden — carryover didn't widen the look-ahead window
    assert f"/admin/studio/recurring/{future_id}#calendar" not in page

    # delivering the carried-over slot clears it from the strip (status leaves planned/shot)
    s = db.one("SELECT id FROM content_calendar WHERE plan_id=? ORDER BY id LIMIT 1", (carry_id,))
    admin.post(
        f"/admin/studio/recurring/{carry_id}/calendar/{s['id']}/status",
        data={"status": "delivered"},
        follow_redirects=False,
    )
    page = admin.get("/admin/studio/activity").text
    assert f"/admin/studio/recurring/{carry_id}#calendar" not in page

    # clean up: hard-delete the plans (cascades their calendar slots)
    for pid_ in (carry_id, current_id, future_id):
        db.run("DELETE FROM recurring_plans WHERE id=?", (pid_,))


def test_retainer_caption_pack(admin):
    """Domain G slice 6a — caption packs (MANUAL, no AI): storage + human workflow
    for caption deliverables, tracked against the quota via the EXISTING delivery log.
    Encodes WHY the decoupling spine extends to captions: creating/editing a caption,
    and advancing it draft→approved, all write NO retainer_deliveries row — the manual
    log stays the count's single source. Approving REUSES slice-4 assisted credit
    (label, qty=1, period) so the human credits in one click; only the /deliveries POST
    moves the count. Also: current-period filtering, CASCADE on hard plan delete, and
    400s on blank text / bad period shape. Date-independent: period from _period()."""
    from urllib.parse import parse_qs, urlparse

    from app.admin.recurring import _period

    admin.post(
        "/admin/studio/clients",
        data={"name": "Caption Kitchen", "company": "Caption Co", "email": "", "phone": ""},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Content retainer"},
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]

    period = _period()  # 'YYYY-MM'

    # create a caption → it renders this period; writes NO delivery row (decoupled)
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions",
        data={"label": "Hero images", "body": "Golden hour pasta, fresh basil.", "period": period},
        follow_redirects=False,
    )
    assert r.status_code == 303
    cap = db.one("SELECT * FROM retainer_captions WHERE plan_id=? ORDER BY id DESC LIMIT 1", (pid,))
    assert cap["status"] == "draft"  # default
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0
    page = admin.get(f"/admin/studio/recurring/{pid}").text
    assert "Golden hour pasta, fresh basil." in page

    # edit the caption text → persists, still NO delivery row
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cap['id']}",
        data={"label": "Hero images", "body": "Edited: smoked brisket, slaw."},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (
        db.one("SELECT body FROM retainer_captions WHERE id=?", (cap["id"],))["body"]
        == "Edited: smoked brisket, slaw."
    )
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0

    # a caption in another period is NOT shown on the current-period view
    admin.post(
        f"/admin/studio/recurring/{pid}/captions",
        data={"label": "Reels", "body": "Next-quarter teaser.", "period": "2099-01"},
        follow_redirects=False,
    )
    page = admin.get(f"/admin/studio/recurring/{pid}").text
    assert "Next-quarter teaser." not in page

    # approve: draft→approved redirects with assisted-credit prefill; NO write here
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cap['id']}/status",
        data={"status": "approved"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["credit_label"] == ["Hero images"]
    assert q["credit_qty"] == ["1"]
    assert q["credit_period"] == [period]
    assert (
        db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0
    )  # approving did NOT auto-log a delivery

    # the prefill fires ONLY on the transition into approved, not on a re-save
    r2 = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cap['id']}/status",
        data={"status": "approved"},
        follow_redirects=False,
    )
    assert "credit_label" not in r2.headers["location"]

    # the human submit of the existing /deliveries route is the ONLY thing that counts
    admin.post(
        f"/admin/studio/recurring/{pid}/deliveries",
        data={"label": "Hero images", "qty": "1", "period": period},
        follow_redirects=False,
    )
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 1

    # bad inputs rejected, not coerced
    assert (
        admin.post(
            f"/admin/studio/recurring/{pid}/captions",
            data={"label": "Hero images", "body": "   ", "period": period},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/studio/recurring/{pid}/captions",
            data={"label": "Hero images", "body": "ok", "period": "nope"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/studio/recurring/{pid}/captions/{cap['id']}/status",
            data={"status": "published"},
            follow_redirects=False,
        ).status_code
        == 400
    )

    # delete a caption → it leaves the list
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cap['id']}/delete", follow_redirects=False
    )
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) AS n FROM retainer_captions WHERE id=?", (cap["id"],))["n"] == 0

    # CASCADE: a hard plan delete cascades the captions (FK ON DELETE CASCADE)
    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))
    assert db.one("SELECT COUNT(*) AS n FROM retainer_captions WHERE plan_id=?", (pid,))["n"] == 0


def test_caption_ai_draft(admin, monkeypatch):
    """Domain G slice 6b — AI caption drafting via Odysseus (assisted, with
    provenance). The first AI-generated content in Mise, at maximum doctrine stress.
    The Odysseus mesh call is STUBBED — no live Odysseus / real model. Encodes WHY
    each guarantee holds: (a) a draft is a SUGGESTION — it populates body but leaves
    status='draft' and writes ZERO delivery rows (drafting can never both generate and
    credit); (b) provenance is recorded AND the verbatim AI draft is retained distinct
    from a later human edit (the draft→final pair is the dataset — losing it is the one
    thing this slice can't do); (c) a mesh failure leaves body/status untouched and
    writes nothing (no partial drafts); (d) Draft-with-AI never silently overwrites
    human words; (e) the slice-6a credit path is unchanged. Date-independent (period
    from _period())."""
    from urllib.parse import parse_qs, urlparse

    from app import caption_ai
    from app.admin.recurring import _period

    admin.post(
        "/admin/studio/clients",
        data={"name": "AI Diner", "company": "AI Co", "email": "", "phone": ""},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Content retainer"},
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]
    period = _period()

    # a caption seeded with HUMAN text
    admin.post(
        f"/admin/studio/recurring/{pid}/captions",
        data={"label": "Hero images", "body": "my own words", "period": period},
        follow_redirects=False,
    )
    cap = db.one("SELECT * FROM retainer_captions WHERE plan_id=? ORDER BY id DESC LIMIT 1", (pid,))
    cid = cap["id"]

    AI_TEXT = "Golden hour pasta, basil fresh off the pass. #avleats #fnbphoto"
    calls = []

    def fake_draft(ctx):
        calls.append(ctx)
        return {"caption": AI_TEXT, "model": "magistral:24b"}

    monkeypatch.setattr(caption_ai, "draft_caption", fake_draft)

    # (d) no-clobber: drafting over HUMAN body without replace is refused — the mesh
    # is never even called, and the human's words survive untouched
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cid}/draft", data={}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "caption_error" in r.headers["location"]
    assert len(calls) == 0
    row = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert row["body"] == "my own words" and row["ai_drafted"] == 0

    # (a) with explicit replace: the draft lands in body as a SUGGESTION — status
    # stays draft, provenance recorded, ZERO delivery rows (never generates+credits)
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
        data={"replace": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "caption_error" not in r.headers["location"]
    assert len(calls) == 1
    row = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert row["body"] == AI_TEXT
    assert row["status"] == "draft"  # generation NEVER approves
    assert row["ai_drafted"] == 1
    assert row["ai_model"] == "magistral:24b"  # model as reported by Odysseus
    assert row["ai_drafted_at"]  # drafted-at timestamp recorded
    assert row["ai_draft_original"] == AI_TEXT
    assert (
        db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0
    )  # drafting never moves the count

    # (b) a human edits the draft → body changes but the ORIGINAL is still recoverable
    admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cid}",
        data={"label": "Hero images", "body": AI_TEXT + " — tightened by hand"},
        follow_redirects=False,
    )
    row = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert row["body"] == AI_TEXT + " — tightened by hand"
    assert row["ai_draft_original"] == AI_TEXT  # the (draft → final) diff survives
    assert row["ai_drafted"] == 1

    # post-edit, body is human-edited again → a re-draft without replace is refused
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cid}/draft", data={}, follow_redirects=False
    )
    assert "caption_error" in r.headers["location"]

    # (c) a stubbed mesh FAILURE writes nothing and leaves body/status/original intact
    def fake_fail(ctx):
        raise caption_ai.CaptionDraftError("Odysseus unreachable: timed out")

    monkeypatch.setattr(caption_ai, "draft_caption", fake_fail)
    before = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
        data={"replace": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "caption_error" in r.headers["location"]
    after = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert after["body"] == before["body"]
    assert after["status"] == before["status"]
    assert after["ai_draft_original"] == before["ai_draft_original"]  # never wiped

    # (e) the slice-6a credit path is unchanged: human approve→delivered carries the
    # prefill with 0 writes; only the /deliveries POST moves the count
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cid}/status",
        data={"status": "approved"},
        follow_redirects=False,
    )
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["credit_label"] == ["Hero images"]
    assert q["credit_qty"] == ["1"]
    assert q["credit_period"] == [period]
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0
    admin.post(
        f"/admin/studio/recurring/{pid}/deliveries",
        data={"label": "Hero images", "qty": "1", "period": period},
        follow_redirects=False,
    )
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 1

    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))


def test_caption_ai_live_wiring(admin, monkeypatch):
    """Domain G slice 6c — wire draft_caption to the LIVE Odysseus endpoint. Unlike 6b's
    test, this stubs the NETWORK seam (urllib.request.urlopen), not the whole function,
    so the real wiring is exercised: the bearer header, the configured URL, the body Mise
    builds, the JSON round-trip, and the 210s>180s timeout. Encodes WHY each guarantee
    holds: (a) the outbound request carries Authorization: Bearer <token> and hits the
    configured URL with the built body, at the deployed timeout (above the endpoint's
    budget so the ENDPOINT decides failure, not the client); (b) a 200 lands as a
    SUGGESTION — body populated, status still 'draft', 0 delivery rows, and the SERVED
    model string (not a static label) persisted as provenance; (c) an HTTP 502/401/400
    raises CaptionDraftError and writes nothing (no partial drafts); (d) no-clobber
    refuses over human body without replace and the network is NEVER touched; (e) with
    URL/token unset, is_enabled() is False and draft_caption raises with no network call.
    Date-independent (period from _period())."""
    import json as _json
    import urllib.error

    from app import caption_ai
    from app.admin.recurring import _period

    URL = "http://mickey:7010/draft/caption"
    TOKEN = "stub-bearer-do-not-log"
    monkeypatch.setattr(config, "ODYSSEUS_CAPTION_URL", URL)
    monkeypatch.setattr(config, "ODYSSEUS_CAPTION_TOKEN", TOKEN)
    # deployed default timeout is deliberately above the endpoint's ~180s budget
    assert config.ODYSSEUS_TIMEOUT > 180

    # (e) not configured -> is_enabled False and draft_caption raises WITHOUT a call
    monkeypatch.setattr(config, "ODYSSEUS_CAPTION_TOKEN", "")
    assert caption_ai.is_enabled() is False
    fired = []
    monkeypatch.setattr(caption_ai.urllib.request, "urlopen", lambda *a, **k: fired.append(1))
    try:
        caption_ai.draft_caption({"label": "x"})
        assert False, "expected CaptionDraftError when not configured"
    except caption_ai.CaptionDraftError:
        pass
    assert fired == []
    monkeypatch.setattr(config, "ODYSSEUS_CAPTION_TOKEN", TOKEN)
    assert caption_ai.is_enabled() is True

    SERVED = "qwen3.5:122b"  # served truth from Odysseus's echo, not a static route label
    AI_TEXT = "Backlit espresso, crema like silk. #avleats #fnbphoto"
    captured: dict = {}

    class _Resp:
        def __init__(self, payload):
            self._b = _json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    def fake_ok(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = _json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _Resp({"caption": AI_TEXT, "model": SERVED})

    monkeypatch.setattr(caption_ai.urllib.request, "urlopen", fake_ok)

    # (a) direct call: outbound carries the bearer, hits the configured URL, sends the
    # built body, and uses the deployed (>180s) timeout
    out = caption_ai.draft_caption({"label": "Hero", "client": "Wire Co", "period": "x"})
    assert out == {"caption": AI_TEXT, "model": SERVED}
    assert captured["url"] == URL
    assert captured["auth"] == f"Bearer {TOKEN}"
    assert captured["body"]["label"] == "Hero" and captured["body"]["client"] == "Wire Co"
    assert captured["timeout"] == config.ODYSSEUS_TIMEOUT

    admin.post(
        "/admin/studio/clients",
        data={"name": "Wire Diner", "company": "Wire Co", "email": "", "phone": ""},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "Retainer"},
        follow_redirects=False,
    )
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/projects/{proj['id']}/recurring",
        data={"title": "Content retainer"},
        follow_redirects=False,
    )
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]
    period = _period()
    admin.post(
        f"/admin/studio/recurring/{pid}/captions",
        data={"label": "Hero images", "body": "placeholder", "period": period},
        follow_redirects=False,
    )
    cap = db.one("SELECT * FROM retainer_captions WHERE plan_id=? ORDER BY id DESC LIMIT 1", (pid,))
    cid = cap["id"]

    # (b) through the route, a stubbed 200 lands as a SUGGESTION: body populated, status
    # still draft, provenance = the SERVED model, and ZERO delivery rows written
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
        data={"replace": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "caption_error" not in r.headers["location"]
    row = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert row["body"] == AI_TEXT
    assert row["status"] == "draft"
    assert row["ai_drafted"] == 1
    assert row["ai_model"] == SERVED
    assert row["ai_draft_original"] == AI_TEXT
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?", (pid,))["n"] == 0

    # (d) no-clobber: a caption with HUMAN body refuses a no-replace draft, and the
    # network seam is NEVER touched
    admin.post(
        f"/admin/studio/recurring/{pid}/captions",
        data={"label": "Interiors", "body": "chef's own caption", "period": period},
        follow_redirects=False,
    )
    hcap = db.one(
        "SELECT * FROM retainer_captions WHERE plan_id=? AND label='Interiors' "
        "ORDER BY id DESC LIMIT 1",
        (pid,),
    )
    tripped = []
    monkeypatch.setattr(caption_ai.urllib.request, "urlopen", lambda *a, **k: tripped.append(1))
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{hcap['id']}/draft",
        data={},
        follow_redirects=False,
    )
    assert "caption_error" in r.headers["location"]
    assert tripped == []
    assert (
        db.one("SELECT body FROM retainer_captions WHERE id=?", (hcap["id"],))["body"]
        == "chef's own caption"
    )

    # (c) a stubbed HTTP 502 raises CaptionDraftError through the route — nothing written
    before = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))

    def fake_502(req, timeout=None):
        raise urllib.error.HTTPError(URL, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(caption_ai.urllib.request, "urlopen", fake_502)
    r = admin.post(
        f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
        data={"replace": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "caption_error" in r.headers["location"]
    after = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert after["body"] == before["body"]
    assert after["status"] == before["status"]
    assert after["ai_draft_original"] == before["ai_draft_original"]

    # 401 and 400 also surface as a clean CaptionDraftError (no partial draft)
    for code in (401, 400):

        def fake_err(req, timeout=None, _c=code):
            raise urllib.error.HTTPError(URL, _c, "err", {}, None)

        monkeypatch.setattr(caption_ai.urllib.request, "urlopen", fake_err)
        try:
            caption_ai.draft_caption({"label": "x"})
            assert False, f"expected CaptionDraftError on HTTP {code}"
        except caption_ai.CaptionDraftError:
            pass

    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))
