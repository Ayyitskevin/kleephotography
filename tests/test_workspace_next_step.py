"""R9a: the workspace's one-next-step rule, without a database.

`next_step` decides which single document carries the page's amber primary
(proposal-open → contract-open → invoice-with-balance → gallery-live) and how
every other document is drawn: still outstanding, done (collapsed), or closed.
It only moves emphasis — it never decides what the client may open, so nothing
here can gate a later step behind an earlier one (that is R19's call).
"""

import pytest

from app.public.workspace import next_step
from tests.workspace_rows import ROWS


def build(proposal, contract, invoice, gallery):
    """Rows shaped like the view's four queries (dicts stand in for sqlite Rows)."""
    proposals = [{"slug": "p1", "title": "Proposal one", "status": proposal, "total_cents": 1}]
    contracts = [{"slug": "c1", "title": "Contract one", "status": contract}]
    invoices = []
    if invoice:
        status, total, deposit = invoice
        invoices = [
            {
                "slug": "i1",
                "title": "Invoice one",
                "status": status,
                "total_cents": total,
                "deposit_cents": deposit,
                "due_date": None,
            }
        ]
    g = {"slug": "g1", "title": "Gallery one", "expires_at": None} if gallery else None
    return (
        proposals if proposal else [],
        contracts if contract else [],
        invoices,
        g,
        gallery == "expired",
    )


def _flat_states(result):
    flat = {}
    for kind in ("proposal", "contract", "invoice"):
        flat.update(result["states"][kind])
    if result["states"]["gallery"] is not None:
        flat["g1"] = result["states"]["gallery"]
    return flat


@pytest.mark.unit
@pytest.mark.parametrize(
    ("proposal", "contract", "invoice", "gallery", "primary", "states", "order"),
    [r[1:] for r in ROWS],
    ids=[r[0] for r in ROWS],
)
def test_next_step_table(proposal, contract, invoice, gallery, primary, states, order):
    result = next_step(*build(proposal, contract, invoice, gallery))
    assert result["primary"] == primary
    assert _flat_states(result) == states
    assert result["order"] == order


@pytest.mark.unit
def test_primary_is_the_open_invoice_among_several():
    """A paid invoice beside an open one: the open one is the next step."""
    invoices = [
        {
            "slug": "i-paid",
            "title": "Deposit",
            "status": "paid",
            "total_cents": 5000,
            "deposit_cents": 0,
            "due_date": None,
        },
        {
            "slug": "i-open",
            "title": "Balance",
            "status": "sent",
            "total_cents": 9000,
            "deposit_cents": 0,
            "due_date": None,
        },
    ]
    result = next_step([], [], invoices, None, False)
    assert result["primary"] == ("invoice", "i-open")
    assert result["states"]["invoice"] == {"i-paid": "done", "i-open": "outstanding"}
    assert result["order"] == ["invoice"]
