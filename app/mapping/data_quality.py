"""Data-quality view: everything that stops a record from being used as settled evidence."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.facts import build_mapping_snapshot, load_facts, open_conflict_record_ids
from app.db.models import (
    DetectionRun,
    ImportBatch,
    ImportRow,
    LineEvaluation,
    SourceConflict,
)
from app.ingestion.records import EmployeeRecord, POLineRecord, ReceiptLineRecord


def data_quality_report(session: Session, tenant_id: str) -> dict[str, Any]:
    blocked = open_conflict_record_ids(session, tenant_id)
    mapping = build_mapping_snapshot(session, tenant_id, blocked)

    unresolved: list[dict[str, Any]] = []
    pos = load_facts(session, tenant_id, "purchase_order_lines")
    for f in pos:
        r: POLineRecord = f.record  # type: ignore[assignment]
        p = mapping.resolve_product(f.source_system, r.item_code, r.ordered_uom)
        loc = mapping.resolve_location(f.source_system, r.location_code)
        if not p.ok:
            unresolved.append({"dataset": "purchase_order_lines", "source_record_id": f.source_key, "field": "item_code", "value": r.item_code, "issue": p.issue})
        if not loc.ok:
            unresolved.append({"dataset": "purchase_order_lines", "source_record_id": f.source_key, "field": "location_code", "value": r.location_code, "issue": loc.issue})
    po_keys = {(f.record.po_number, f.record.line_number) for f in pos}  # type: ignore[attr-defined]

    orphans: list[dict[str, Any]] = []
    for f in load_facts(session, tenant_id, "goods_receipt_lines"):
        rr: ReceiptLineRecord = f.record  # type: ignore[assignment]
        p = mapping.resolve_product(f.source_system, rr.product_ref, rr.uom)
        loc = mapping.resolve_location(f.source_system, rr.location_code)
        if not p.ok:
            unresolved.append({"dataset": "goods_receipt_lines", "source_record_id": f.source_key, "field": "product_ref", "value": rr.product_ref, "issue": p.issue})
        if not loc.ok:
            unresolved.append({"dataset": "goods_receipt_lines", "source_record_id": f.source_key, "field": "location_code", "value": rr.location_code, "issue": loc.issue})
        if (rr.po_number, rr.po_line_number) not in po_keys:
            orphans.append({"source_record_id": f.source_key, "po_number": rr.po_number, "po_line_number": rr.po_line_number, "issue": "RECEIPT_FOR_UNKNOWN_PO_LINE"})

    for f in load_facts(session, tenant_id, "employees"):
        e: EmployeeRecord = f.record  # type: ignore[assignment]
        if e.location_code:
            loc = mapping.resolve_location("HRMS", e.location_code)
            if not loc.ok:
                unresolved.append({"dataset": "employees", "source_record_id": f.source_key, "field": "location_code", "value": e.location_code, "issue": loc.issue})

    # Rejections that are still current: rows rejected by the latest import of each dataset.
    latest = (
        select(ImportBatch.dataset, func.max(ImportBatch.started_at).label("ts"))
        .where(ImportBatch.tenant_id == tenant_id)
        .group_by(ImportBatch.dataset)
        .subquery()
    )
    latest_ids = select(ImportBatch.id).join(latest, (ImportBatch.dataset == latest.c.dataset) & (ImportBatch.started_at == latest.c.ts))
    rejected = session.execute(
        select(ImportBatch.dataset, ImportRow).join(ImportRow, ImportRow.import_id == ImportBatch.id)
        .where(ImportRow.outcome == "REJECTED", ImportBatch.id.in_(latest_ids))
        .order_by(ImportBatch.dataset, ImportRow.row_number)
    ).all()
    failed = session.execute(
        select(ImportBatch).where(ImportBatch.tenant_id == tenant_id, ImportBatch.status == "FAILED", ImportBatch.id.in_(latest_ids))
    ).scalars().all()

    conflicts = session.execute(
        select(SourceConflict).where(SourceConflict.tenant_id == tenant_id, SourceConflict.status == "OPEN").order_by(SourceConflict.id)
    ).scalars().all()

    last_run = session.execute(
        select(DetectionRun).where(DetectionRun.tenant_id == tenant_id).order_by(DetectionRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    not_evaluable = []
    if last_run is not None:
        not_evaluable = [
            {"po_number": le.po_number, "po_line_number": le.po_line_number, "reasons": le.reasons}
            for le in session.execute(
                select(LineEvaluation).where(LineEvaluation.run_id == last_run.id, LineEvaluation.outcome == "NOT_EVALUABLE")
            ).scalars()
        ]

    return {
        "mapping_version": mapping.version,
        "unresolved_mappings": unresolved,
        "orphan_receipts": orphans,
        "open_conflicts": [
            {
                "conflict_id": c.id,
                "dataset": c.dataset,
                "source_key": c.source_key,
                "kind": c.kind,
                "existing_fact_id": c.existing_record_id,
                "incoming_payload": c.incoming_payload,
                "first_import_id": str(c.first_import_id),
                "first_row_number": c.first_row_number,
            }
            for c in conflicts
        ],
        "rejected_rows_latest_imports": [
            {"dataset": ds, "import_id": str(row.import_id), "row_number": row.row_number, "errors": row.errors, "raw": row.raw}
            for ds, row in rejected
        ],
        "failed_latest_imports": [{"dataset": b.dataset, "import_id": str(b.id), "reason": b.failure_reason} for b in failed],
        "not_evaluable_po_lines_last_run": not_evaluable,
        "last_detection_run_id": str(last_run.id) if last_run else None,
    }
