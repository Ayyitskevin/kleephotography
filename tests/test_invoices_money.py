"""Money-path unit tests — the math that decides what a client is actually charged.

These pin two pure functions that sit directly on the cash path:
  - pay.next_payment: how much is owed right now (drives the Stripe charge amount).
  - proposals.parse_items: turns line-item rows into a total (seeds the invoice).

A regression here is not a cosmetic bug — it over/under-charges a real client or
sends a wrong total. The asserts encode WHY each number must be what it is, so they
fail the moment the money logic drifts (R9).
"""

import json

import pytest
from fastapi import HTTPException

from app.admin.proposals import parse_items
from app.public.pay import next_payment

pytestmark = pytest.mark.unit


def _invoice(*, status, total_cents, deposit_cents=0):
    # next_payment only reads these three keys; a dict stands in for a DB row.
    return {"status": status, "total_cents": total_cents, "deposit_cents": deposit_cents}


# --- next_payment: what is owed right now -----------------------------------


def test_no_deposit_charges_the_full_total():
    amount, kind = next_payment(_invoice(status="sent", total_cents=90000))
    assert (amount, kind) == (90000, "full")


def test_deposit_set_but_unpaid_charges_the_deposit():
    amount, kind = next_payment(_invoice(status="sent", total_cents=90000, deposit_cents=30000))
    assert (amount, kind) == (30000, "deposit")


def test_after_deposit_only_the_remaining_balance_is_owed():
    # The single most dangerous line: balance must be total - deposit, never the
    # full total again (that would double-bill the client for the deposit).
    amount, kind = next_payment(
        _invoice(status="deposit_paid", total_cents=90000, deposit_cents=30000)
    )
    assert (amount, kind) == (60000, "balance")


def test_paid_invoice_owes_nothing():
    # (0, "") is the sentinel pay_invoice uses to refuse a second charge.
    amount, kind = next_payment(_invoice(status="paid", total_cents=90000, deposit_cents=30000))
    assert (amount, kind) == (0, "")


# --- parse_items: line items -> total ---------------------------------------


def test_total_is_sum_of_qty_times_unit_across_rows():
    form = {
        "item_label_0": "Half-day session",
        "item_qty_0": "1",
        "item_price_0": "900.00",
        "item_label_1": "Extra edits",
        "item_qty_1": "3",
        "item_price_1": "25.00",
    }
    items_json, total = parse_items(form)
    items = json.loads(items_json)
    assert len(items) == 2
    assert total == 90000 + 3 * 2500  # 97500 cents
    assert items[0] == {"label": "Half-day session", "qty": 1, "unit_cents": 90000}


def test_rows_without_a_label_are_skipped():
    # The form always submits MAX_ITEM_ROWS rows; blank ones must not become $0 lines.
    form = {
        "item_label_0": "Session",
        "item_qty_0": "1",
        "item_price_0": "500.00",
        "item_label_1": "",
        "item_qty_1": "9",
        "item_price_1": "999.00",
    }
    items_json, total = parse_items(form)
    assert len(json.loads(items_json)) == 1
    assert total == 50000


def test_quantity_below_one_is_floored_to_one():
    form = {"item_label_0": "Session", "item_qty_0": "0", "item_price_0": "500.00"}
    _, total = parse_items(form)
    assert total == 50000  # qty 0 -> 1, not a free line


def test_non_numeric_price_is_rejected_not_silently_zeroed():
    form = {"item_label_0": "Session", "item_qty_0": "1", "item_price_0": "abc"}
    with pytest.raises(HTTPException) as exc:
        parse_items(form)
    assert exc.value.status_code == 400
