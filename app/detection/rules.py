"""Receipt-shortfall rule. Pure and deterministic: same facts + same mapping + same as-of time = same result.

Grain: one evaluation per PO line fact. Receipts are joined to a PO line by (po_number, po_line_number),
and each receipt fact is counted once because facts are already unique per source identity. Quantities
are compared only after both sides are converted to the product's base unit.

Order of checks (first match wins):
  1. PO line has an open source conflict          -> NOT_EVALUABLE (CONFLICTING_PO_LINE)
  2. PO line is CANCELLED                          -> NOT_APPLICABLE_CANCELLED
  3. PO product/location cannot be resolved        -> NOT_EVALUABLE (mapping issue = data quality)
  4. deadline has not passed at as-of time         -> NOT_DUE   (deadline == as_of is NOT passed)
  5. any linked receipt is conflicted / unmapped /
     maps to another product or location           -> NOT_EVALUABLE
  6. expected - accepted > tolerance               -> SHORTFALL
     accepted > expected                           -> OVER_RECEIPT (recorded, not raised by this rule)
     otherwise                                     -> NO_SHORTFALL

Receipts count only if posting_status is POSTED and received_at <= as_of. Unposted or reversed lines and
lines stamped after the as-of time are listed in the evaluation as excluded, with the reason.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from app.db.facts import Fact
from app.ingestion.records import (
    POLineRecord,
    POStatus,
    PostingStatus,
    ReceiptLineRecord,
)
from app.mapping.resolver import MappingSnapshot

RULE_ID = "RECEIPT_SHORTFALL"
RULE_VERSION = "1.0.0"


class Outcome(str, Enum):
    SHORTFALL = "SHORTFALL"
    NO_SHORTFALL = "NO_SHORTFALL"
    OVER_RECEIPT = "OVER_RECEIPT"
    NOT_DUE = "NOT_DUE"
    NOT_APPLICABLE_CANCELLED = "NOT_APPLICABLE_CANCELLED"
    NOT_EVALUABLE = "NOT_EVALUABLE"


@dataclass(frozen=True)
class ReceiptUse:
    fact: Fact
    counted: bool
    reason: str | None
    qty_base: Decimal | None


@dataclass
class LineResult:
    po: Fact
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    canonical_product_id: str | None = None
    canonical_location_id: str | None = None
    region_id: str | None = None
    base_uom: str | None = None
    expected_qty: Decimal | None = None
    received_qty: Decimal | None = None
    variance_qty: Decimal | None = None
    receipts: list[ReceiptUse] = field(default_factory=list)
    product_alias_record_id: int | None = None
    location_alias_record_id: int | None = None

    @property
    def po_record(self) -> POLineRecord:
        return self.po.record  # type: ignore[return-value]

    def details(self) -> dict[str, Any]:
        def q(v: Decimal | None) -> str | None:
            return None if v is None else format(v.normalize(), "f")

        return {
            "canonical_product_id": self.canonical_product_id,
            "canonical_location_id": self.canonical_location_id,
            "base_uom": self.base_uom,
            "expected_qty": q(self.expected_qty),
            "received_qty": q(self.received_qty),
            "variance_qty": q(self.variance_qty),
            "po_source": self.po.ref("purchase_order_lines"),
            "receipts": [
                {
                    **u.fact.ref("goods_receipt_lines"),
                    "counted": u.counted,
                    "excluded_reason": u.reason,
                    "qty_base": q(u.qty_base),
                }
                for u in self.receipts
            ],
        }


def evaluate_po_line(
    po: Fact,
    receipts: Sequence[Fact],
    mapping: MappingSnapshot,
    *,
    as_of: datetime,
    tolerance: Decimal = Decimal("0"),
    conflicted_po_ids: Iterable[int] = (),
    conflicted_receipt_ids: Iterable[int] = (),
) -> LineResult:
    rec: POLineRecord = po.record  # type: ignore[assignment]
    result = LineResult(po=po, outcome=Outcome.NOT_EVALUABLE)

    if po.record_id in set(conflicted_po_ids):
        result.reasons.append("CONFLICTING_PO_LINE: an open source conflict exists for this PO line")
        return result

    if rec.status == POStatus.CANCELLED:
        result.outcome = Outcome.NOT_APPLICABLE_CANCELLED
        result.reasons.append("PO line status is CANCELLED in the ERP; a missing receipt is expected")
        return result

    product = mapping.resolve_product(po.source_system, rec.item_code, rec.ordered_uom)
    location = mapping.resolve_location(po.source_system, rec.location_code)
    if not product.ok:
        result.reasons.append(f"{product.issue}: PO item {po.source_system}/{rec.item_code} ({rec.ordered_uom})")
    if not location.ok:
        result.reasons.append(f"{location.issue}: PO location {po.source_system}/{rec.location_code}")
    if result.reasons:
        return result

    result.canonical_product_id = product.canonical_product_id
    result.canonical_location_id = location.canonical_location_id
    result.region_id = location.region_id
    result.base_uom = product.base_uom
    result.product_alias_record_id = product.alias_record_id
    result.location_alias_record_id = location.alias_record_id
    result.expected_qty = product.to_base(rec.ordered_qty)

    blocked_receipts = set(conflicted_receipt_ids)
    counted = Decimal("0")
    for fact in receipts:
        r: ReceiptLineRecord = fact.record  # type: ignore[assignment]
        if fact.record_id in blocked_receipts:
            result.reasons.append(f"CONFLICTING_RECEIPT: {fact.source_key} has an open source conflict")
            result.receipts.append(ReceiptUse(fact, False, "OPEN_CONFLICT", None))
            continue
        if r.posting_status != PostingStatus.POSTED:
            result.receipts.append(ReceiptUse(fact, False, f"POSTING_STATUS_{r.posting_status.value}", None))
            continue
        if r.received_at > as_of:
            result.receipts.append(ReceiptUse(fact, False, "RECEIVED_AFTER_AS_OF", None))
            continue
        r_product = mapping.resolve_product(fact.source_system, r.product_ref, r.uom)
        r_location = mapping.resolve_location(fact.source_system, r.location_code)
        if not r_product.ok or not r_location.ok:
            issue = r_product.issue if not r_product.ok else r_location.issue
            result.reasons.append(f"{issue}: receipt {fact.source_key}")
            result.receipts.append(ReceiptUse(fact, False, issue, None))
            continue
        if r_product.canonical_product_id != product.canonical_product_id:
            result.reasons.append(
                f"RECEIPT_PRODUCT_MISMATCH: {fact.source_key} is {r_product.canonical_product_id}, "
                f"PO line is {product.canonical_product_id}"
            )
            result.receipts.append(ReceiptUse(fact, False, "PRODUCT_MISMATCH", None))
            continue
        if r_location.canonical_location_id != location.canonical_location_id:
            result.reasons.append(
                f"RECEIPT_LOCATION_MISMATCH: {fact.source_key} is {r_location.canonical_location_id}, "
                f"PO line is {location.canonical_location_id}"
            )
            result.receipts.append(ReceiptUse(fact, False, "LOCATION_MISMATCH", None))
            continue
        qty = r_product.to_base(r.accepted_qty)
        counted += qty
        result.receipts.append(ReceiptUse(fact, True, None, qty))

    result.received_qty = counted
    result.variance_qty = result.expected_qty - counted

    if as_of <= rec.expected_receipt_by:
        result.outcome = Outcome.NOT_DUE
        result.reasons.insert(0, "receiving window still open at the evaluation time")
        return result
    if result.reasons:  # any receipt-level identity or conflict problem blocks a business conclusion
        result.outcome = Outcome.NOT_EVALUABLE
        return result
    if result.variance_qty > tolerance:
        result.outcome = Outcome.SHORTFALL
    elif result.variance_qty < 0:
        result.outcome = Outcome.OVER_RECEIPT
    else:
        result.outcome = Outcome.NO_SHORTFALL
    return result


def group_receipts_by_po_line(receipts: Iterable[Fact]) -> dict[tuple[str, int], list[Fact]]:
    grouped: dict[tuple[str, int], list[Fact]] = {}
    for fact in receipts:
        r: ReceiptLineRecord = fact.record  # type: ignore[assignment]
        grouped.setdefault((r.po_number, r.po_line_number), []).append(fact)
    return grouped
