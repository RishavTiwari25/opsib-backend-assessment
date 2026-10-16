"""Neutral, evidence-bounded explanation for a receipt shortfall.

The fixture pack has no supplier dispatch (ASN), invoice or carrier data. A shortfall therefore says only
that fewer units were accepted and posted than the PO line ordered. It does not say who caused it.
"""
from __future__ import annotations

from typing import Any

from app.detection.rules import LineResult


def _q(v: Any) -> str:
    return format(v.normalize(), "f")


def build_explanation(result: LineResult) -> dict[str, Any]:
    po = result.po_record
    counted = [u for u in result.receipts if u.counted]
    excluded = [u for u in result.receipts if not u.counted]
    unit = result.base_uom
    known = [
        f"ERP PO {po.po_number} line {po.line_number} ordered {_q(po.ordered_qty)} {po.ordered_uom} of "
        f"{result.canonical_product_id} for {result.canonical_location_id} = {_q(result.expected_qty)} {unit} in the base unit.",
        f"The expected-receipt deadline was {po.expected_receipt_by.isoformat()} and had passed at the evaluation time.",
        f"{_q(result.received_qty)} {unit} were accepted on {len(counted)} posted WMS receipt line(s): "
        + (", ".join(u.fact.source_key for u in counted) or "none") + ".",
        f"Accepted quantity is {_q(result.variance_qty)} {unit} below the ordered quantity.",
    ]
    if excluded:
        known.append(
            "Receipt lines linked to this PO line but not counted: "
            + ", ".join(f"{u.fact.source_key} ({u.reason})" for u in excluded)
            + "."
        )
    return {
        "statement": (
            "Receipt shortfall against the purchase order. This records a quantity difference between what was "
            "ordered and what was accepted and posted. It does not establish supplier fault, carrier loss or a "
            "financial loss."
        ),
        "known": known,
        "unknown": [
            "Whether the supplier dispatched the full quantity: no ASN / dispatch note was supplied.",
            "Whether goods were lost or damaged in transit, or where the discrepancy occurred.",
            "Whether goods arrived but were not yet received or posted in the WMS.",
            "Whether the supplier has invoiced the ordered or the received quantity.",
        ],
        "evidence_needed": [
            "Supplier ASN / dispatch note with dispatched quantity for this PO line.",
            "Carrier proof of delivery showing delivered quantity and any exceptions noted at the dock.",
            f"Receiving-dock record or physical count at {result.canonical_location_id}.",
            "Any pending, unposted or reversed GRNs for this PO line in the WMS.",
            "Supplier invoice quantity, before any credit note is requested.",
        ],
    }
