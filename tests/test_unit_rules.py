"""Unit tests for the receipt-shortfall rule: UOM conversion, partial receipts, deadline boundaries,
cancellation, posting status and identity checks. Pure functions, no database."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.detection.rules import Outcome, evaluate_po_line
from app.mapping.resolver import AliasFact, MappingSnapshot
from tests.helpers import fact, fixture_bytes

AS_OF = datetime.fromisoformat("2026-10-16T15:00:00+05:30")


def _aliases(dataset: str):
    rows = list(csv.DictReader(io.StringIO(fixture_bytes(dataset).decode())))
    out = []
    for i, row in enumerate(rows, start=1):
        f = fact(dataset, i, **row)
        out.append((AliasFact(i, f.source_key, f"hash{i}", 1), f.record))
    return out


@pytest.fixture(scope="module")
def mapping() -> MappingSnapshot:
    return MappingSnapshot.build(_aliases("product_aliases"), _aliases("location_aliases"))


def po(record_id=1, *, qty="100", uom="EA", item="ERP-SKU-001", loc="MUM-014", status="OPEN", due="2026-10-15T12:00:00+05:30", number="PO-900"):
    return fact(
        "purchase_order_lines",
        record_id,
        source_record_id=f"ERP-{number}-L1",
        po_number=number,
        line_number=1,
        supplier_code="SUP-01",
        location_code=loc,
        item_code=item,
        ordered_qty=qty,
        ordered_uom=uom,
        status=status,
        expected_receipt_by=due,
    )


def grn(record_id, qty, *, sid=None, product="WMS-BRC-001", loc="WH-MH-014", uom="EA", at="2026-10-15T10:00:00+05:30", posting="POSTED", number="PO-900"):
    return fact(
        "goods_receipt_lines",
        record_id,
        source_record_id=sid or f"WMS-{record_id}",
        receipt_number=f"GRN-{record_id}",
        receipt_line=1,
        po_number=number,
        po_line_number=1,
        location_code=loc,
        product_ref=product,
        accepted_qty=qty,
        uom=uom,
        received_at=at,
        posting_status=posting,
    )


def test_partial_receipts_are_summed_into_one_shortfall(mapping):
    r = evaluate_po_line(po(), [grn(10, "60"), grn(11, "32")], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.SHORTFALL
    assert (r.expected_qty, r.received_qty, r.variance_qty) == (Decimal("100"), Decimal("92"), Decimal("8"))
    assert [u.fact.source_key for u in r.receipts if u.counted] == ["WMS-10", "WMS-11"]


def test_box_is_converted_to_each_before_comparison(mapping):
    r = evaluate_po_line(po(qty="10", uom="BOX", item="ERP-BOX-12", loc="MUM-015"), [grn(10, "96", product="WMS-MAT-2002", loc="WH-MH-015")], mapping, as_of=AS_OF)
    assert r.base_uom == "EA"
    assert (r.expected_qty, r.received_qty, r.variance_qty) == (Decimal("120"), Decimal("96"), Decimal("24"))
    assert r.outcome == Outcome.SHORTFALL


def test_box_ordered_and_fully_received_in_each_is_not_a_shortfall(mapping):
    r = evaluate_po_line(po(qty="10", uom="BOX", item="ERP-BOX-12", loc="MUM-015"), [grn(10, "120", product="WMS-MAT-2002", loc="WH-MH-015")], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.NO_SHORTFALL


def test_full_receipt_is_no_shortfall(mapping):
    assert evaluate_po_line(po(qty="40"), [grn(10, "40")], mapping, as_of=AS_OF).outcome == Outcome.NO_SHORTFALL


@pytest.mark.parametrize(
    ("deadline_offset", "expected"),
    [(timedelta(seconds=1), Outcome.NOT_DUE), (timedelta(0), Outcome.NOT_DUE), (timedelta(seconds=-1), Outcome.SHORTFALL)],
    ids=["deadline-after-as-of", "deadline-equals-as-of", "deadline-passed"],
)
def test_deadline_boundary(mapping, deadline_offset, expected):
    due = (AS_OF + deadline_offset).isoformat()
    r = evaluate_po_line(po(qty="50", due=due), [grn(10, "20")], mapping, as_of=AS_OF)
    assert r.outcome == expected
    assert r.received_qty == Decimal("20")  # progress is visible even while not due


def test_deadline_is_compared_as_an_instant_across_offsets(mapping):
    # 09:29:59Z is 14:59:59+05:30, i.e. one second before the frozen evaluation time
    r = evaluate_po_line(po(qty="50", due="2026-10-16T09:29:59Z"), [grn(10, "20")], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.SHORTFALL


def test_cancelled_line_is_not_applicable_even_with_no_receipts(mapping):
    r = evaluate_po_line(po(status="CANCELLED"), [], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.NOT_APPLICABLE_CANCELLED


def test_cancelled_line_with_unmapped_item_is_still_just_cancelled(mapping):
    r = evaluate_po_line(po(status="CANCELLED", item="ERP-UNKNOWN"), [], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.NOT_APPLICABLE_CANCELLED


def test_unposted_and_reversed_receipts_are_excluded(mapping):
    r = evaluate_po_line(po(qty="20"), [grn(10, "10"), grn(11, "10", posting="PENDING"), grn(12, "10", posting="REVERSED")], mapping, as_of=AS_OF)
    assert r.received_qty == Decimal("10")
    assert {u.reason for u in r.receipts if not u.counted} == {"POSTING_STATUS_PENDING", "POSTING_STATUS_REVERSED"}
    assert r.outcome == Outcome.SHORTFALL


def test_receipt_stamped_after_as_of_is_not_counted(mapping):
    r = evaluate_po_line(po(qty="20"), [grn(10, "10"), grn(11, "10", at="2026-10-16T16:00:00+05:30")], mapping, as_of=AS_OF)
    assert r.received_qty == Decimal("10")
    assert [u.reason for u in r.receipts if not u.counted] == ["RECEIVED_AFTER_AS_OF"]


def test_late_receipt_before_as_of_still_counts(mapping):
    r = evaluate_po_line(po(qty="20"), [grn(10, "20", at="2026-10-16T09:00:00+05:30")], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.NO_SHORTFALL


def test_unknown_product_is_a_data_quality_issue_not_an_exception(mapping):
    r = evaluate_po_line(po(item="ERP-SKU-999"), [grn(10, "10")], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.NOT_EVALUABLE
    assert r.reasons[0].startswith("UNMAPPED_PRODUCT")


def test_unit_not_declared_by_alias_is_not_guessed(mapping):
    r = evaluate_po_line(po(qty="1", uom="CASE"), [], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.NOT_EVALUABLE
    assert r.reasons[0].startswith("UOM_MISMATCH")


def test_receipt_for_another_location_blocks_the_line(mapping):
    r = evaluate_po_line(po(qty="20"), [grn(10, "10", loc="WH-MH-015")], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.NOT_EVALUABLE
    assert any(x.startswith("RECEIPT_LOCATION_MISMATCH") for x in r.reasons)


def test_receipt_for_another_product_blocks_the_line(mapping):
    r = evaluate_po_line(po(qty="20"), [grn(10, "10", product="WMS-BRC-003")], mapping, as_of=AS_OF)
    assert r.outcome == Outcome.NOT_EVALUABLE


def test_conflicted_receipt_blocks_the_line(mapping):
    r = evaluate_po_line(po(qty="20"), [grn(10, "10")], mapping, as_of=AS_OF, conflicted_receipt_ids={10})
    assert r.outcome == Outcome.NOT_EVALUABLE


def test_over_receipt_is_recorded_but_not_a_shortfall(mapping):
    assert evaluate_po_line(po(qty="20"), [grn(10, "25")], mapping, as_of=AS_OF).outcome == Outcome.OVER_RECEIPT


def test_configurable_tolerance(mapping):
    r = evaluate_po_line(po(qty="100"), [grn(10, "98")], mapping, as_of=AS_OF, tolerance=Decimal("2"))
    assert r.outcome == Outcome.NO_SHORTFALL
    r = evaluate_po_line(po(qty="100"), [grn(10, "97")], mapping, as_of=AS_OF, tolerance=Decimal("2"))
    assert r.outcome == Outcome.SHORTFALL
