"""Ingestion against real PostgreSQL: row-level validation, duplicates, idempotent re-import, conflicting
source ids, business-key collisions and concurrent imports."""
from __future__ import annotations

import threading

from sqlalchemy import func, select

from app.db.models import ImportRow, SourceConflict, SourceRecord
from tests.helpers import fixture_bytes, load, load_pack, replace_rows, with_rows


def _facts(factory, dataset: str) -> int:
    with factory() as s:
        return s.execute(select(func.count()).select_from(SourceRecord).where(SourceRecord.dataset == dataset)).scalar_one()


def test_fixture_pack_counts_and_identical_duplicates(factory):
    batches = load_pack(factory)
    po, grn = batches["purchase_order_lines"], batches["goods_receipt_lines"]
    assert (po.row_count, po.accepted_count, po.duplicate_in_file_count, po.rejected_count) == (7, 6, 1, 0)
    assert (grn.row_count, grn.accepted_count, grn.duplicate_in_file_count, grn.rejected_count) == (7, 6, 1, 0)
    assert all(b.status == "COMPLETED" for b in batches.values())
    assert _facts(factory, "goods_receipt_lines") == 6
    with factory() as s:
        dup = s.execute(select(ImportRow).where(ImportRow.import_id == grn.id, ImportRow.outcome == "DUPLICATE_IN_FILE")).scalar_one()
        assert dup.source_key == "WMS-GRN506-A-L1" and dup.row_number == 7


def test_reimporting_the_same_files_is_a_no_op(factory):
    load_pack(factory)
    with factory() as s:
        before = s.execute(select(SourceRecord.id, SourceRecord.content_hash, SourceRecord.version).order_by(SourceRecord.id)).all()
    second = load_pack(factory)
    assert all(b.accepted_count == 0 and b.conflict_count == 0 for b in second.values())
    assert second["goods_receipt_lines"].already_loaded_count == 7
    with factory() as s:
        after = s.execute(select(SourceRecord.id, SourceRecord.content_hash, SourceRecord.version).order_by(SourceRecord.id)).all()
    assert before == after


def test_formatting_noise_is_not_a_change(factory):
    load(factory, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"))
    noisy = replace_rows(
        "goods_receipt_lines",
        {"WMS-GRN501-A-L1,GRN-501-A,1,PO-501,1,WH-MH-014,WMS-BRC-001,60,EA,2026-10-15T10:00:00+05:30,POSTED": "WMS-GRN501-A-L1,GRN-501-A,1,PO-501,1,WH-MH-014,WMS-BRC-001,60.000,ea,2026-10-15T04:30:00Z,POSTED"},
    )
    batch = load(factory, "goods_receipt_lines", noisy)
    assert batch.conflict_count == 0 and batch.already_loaded_count == 7


def test_invalid_rows_are_rejected_with_reasons_and_valid_rows_still_load(factory):
    content = with_rows(
        "goods_receipt_lines",
        "WMS-BAD-1,GRN-900,1,PO-501,1,WH-MH-014,WMS-BRC-001,-5,EA,2026-10-15T10:00:00+05:30,POSTED",
        "WMS-BAD-2,GRN-901,1,PO-501,1,WH-MH-014,WMS-BRC-001,5,EA,2026-10-15T10:00:00,POSTED",
        "WMS-BAD-3,GRN-902,1,PO-501,1,WH-MH-014,WMS-BRC-001,5,EA,2026-10-15T10:00:00+05:30,SHIPPED",
        ",GRN-903,1,PO-501,1,WH-MH-014,WMS-BRC-001,5,EA,2026-10-15T10:00:00+05:30,POSTED",
    )
    batch = load(factory, "goods_receipt_lines", content)
    assert batch.status == "COMPLETED_WITH_ERRORS"
    assert (batch.accepted_count, batch.rejected_count) == (6, 4)
    with factory() as s:
        rejected = s.execute(select(ImportRow).where(ImportRow.import_id == batch.id, ImportRow.outcome == "REJECTED").order_by(ImportRow.row_number)).scalars().all()
    fields = [r.errors[0]["field"] for r in rejected]
    assert fields == ["accepted_qty", "received_at", "posting_status", "source_record_id"]
    assert "UTC offset" in rejected[1].errors[0]["reason"]
    assert rejected[0].raw["source_record_id"] == "WMS-BAD-1"  # raw row kept for review


def test_missing_required_column_fails_the_whole_file_visibly(factory):
    content = b"source_record_id,po_number\nX,PO-1\n"
    batch = load(factory, "purchase_order_lines", content)
    assert batch.status == "FAILED"
    assert "missing required columns" in batch.failure_reason
    assert _facts(factory, "purchase_order_lines") == 0


def test_same_source_id_with_changed_content_is_flagged_not_overwritten(factory):
    load(factory, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"))
    changed = replace_rows(
        "goods_receipt_lines",
        {"WMS-GRN501-B-L1,GRN-501-B,1,PO-501,1,WH-MH-014,WMS-BRC-001,32,EA": "WMS-GRN501-B-L1,GRN-501-B,1,PO-501,1,WH-MH-014,WMS-BRC-001,40,EA"},
    )
    batch = load(factory, "goods_receipt_lines", changed)
    assert batch.conflict_count == 1 and batch.status == "COMPLETED_WITH_ERRORS"
    with factory() as s:
        rec = s.execute(select(SourceRecord).where(SourceRecord.source_key == "WMS-GRN501-B-L1")).scalar_one()
        assert rec.payload["accepted_qty"] == "32" and rec.version == 1
        conflict = s.execute(select(SourceConflict)).scalar_one()
        assert (conflict.kind, conflict.status, conflict.incoming_payload["accepted_qty"]) == ("CHANGED_CONTENT", "OPEN", "40")
    # importing the same conflicting file again does not open a second conflict
    load(factory, "goods_receipt_lines", changed)
    with factory() as s:
        assert s.execute(select(func.count()).select_from(SourceConflict)).scalar_one() == 1


def test_new_source_id_reusing_a_business_key_is_a_collision(factory):
    load(factory, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"))
    batch = load(factory, "goods_receipt_lines", with_rows("goods_receipt_lines", "WMS-OTHER-ID,GRN-501-A,1,PO-501,1,WH-MH-014,WMS-BRC-001,60,EA,2026-10-15T10:00:00+05:30,POSTED"))
    assert batch.conflict_count == 1
    with factory() as s:
        assert s.execute(select(SourceConflict.kind)).scalar_one() == "BUSINESS_KEY_COLLISION"
    assert _facts(factory, "goods_receipt_lines") == 6


def test_conflicting_duplicate_inside_one_file(factory):
    content = with_rows("purchase_order_lines", "ERP-PO501-L1,PO-501,1,SUP-07,MUM-014,ERP-SKU-001,90,EA,OPEN,2026-10-15T12:00:00+05:30")
    batch = load(factory, "purchase_order_lines", content)
    assert (batch.accepted_count, batch.duplicate_in_file_count, batch.conflict_count) == (6, 1, 1)


def test_concurrent_imports_of_the_same_file_store_each_fact_once(factory):
    errors: list[BaseException] = []

    def run():
        try:
            load(factory, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert _facts(factory, "goods_receipt_lines") == 6
    with factory() as s:
        accepted = s.execute(select(func.count()).select_from(ImportRow).where(ImportRow.outcome == "ACCEPTED")).scalar_one()
    assert accepted == 6  # exactly one import accepted each identity


def test_employee_needs_some_scope_and_extra_columns_do_not_break_import(factory):
    content = (
        "source_employee_id,display_name,employment_status,role,location_code,manager_employee_id,region_id,badge\n"
        "HR-900,Test Person,ACTIVE,receiving_associate,,,,B1\n"
        "HR-901,Test Person,ACTIVE,receiving_associate,ST-014,,REGION-MH,B2\n"
    ).encode()
    batch = load(factory, "employees", content)
    assert (batch.rejected_count, batch.accepted_count) == (1, 1)
