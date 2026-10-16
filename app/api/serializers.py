"""Response shapes. Plain dicts keep the JSON explicit; Decimals are rendered as strings to avoid float drift."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.db.models import ExceptionCase, ImportBatch, ImportRow, Task
from app.workflows.state_machine import allowed_actions


def q(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")


def import_summary(b: ImportBatch) -> dict[str, Any]:
    return {
        "import_id": str(b.id),
        "dataset": b.dataset,
        "adapter": b.adapter,
        "source_system": b.source_system,
        "filename": b.filename,
        "file_sha256": b.file_sha256,
        "status": b.status,
        "counts": {
            "rows": b.row_count,
            "accepted": b.accepted_count,
            "already_loaded": b.already_loaded_count,
            "duplicate_in_file": b.duplicate_in_file_count,
            "rejected": b.rejected_count,
            "conflict": b.conflict_count,
        },
        "failure_reason": b.failure_reason,
        "started_at": b.started_at,
        "finished_at": b.finished_at,
    }


def import_row(r: ImportRow) -> dict[str, Any]:
    return {
        "row_number": r.row_number,
        "outcome": r.outcome,
        "source_key": r.source_key,
        "fact_id": r.source_record_id,
        "conflict_id": r.conflict_id,
        "errors": r.errors,
        "raw": r.raw,
    }


def exception_brief(e: ExceptionCase) -> dict[str, Any]:
    return {
        "exception_id": str(e.id),
        "business_key": e.business_key,
        "type": e.case_type,
        "status": e.status,
        "po_number": e.po_number,
        "po_line_number": e.po_line_number,
        "supplier_code": e.supplier_code,
        "canonical_product_id": e.canonical_product_id,
        "canonical_location_id": e.canonical_location_id,
        "region_id": e.region_id,
        "unit": e.base_uom,
        "expected_qty": q(e.expected_qty),
        "received_qty": q(e.received_qty),
        "variance_qty": q(e.variance_qty),
        "expected_receipt_by": e.expected_receipt_by,
        "detected_as_of": e.detected_as_of,
        "rule": {"id": e.rule_id, "version": e.rule_version},
        "mapping_version": e.mapping_version,
        "revision": e.revision,
    }


def task_view(t: Task, *, with_history: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "task_id": str(t.id),
        "exception_id": str(t.exception_id),
        "task_type": t.task_type,
        "status": t.status,
        "allowed_actions": allowed_actions(t.status),
        "canonical_location_id": t.canonical_location_id,
        "assignee_employee_id": t.assignee_employee_id,
        "assignment_queue": t.assignment_queue,
        "due_at": t.due_at,
        "resolution_code": t.resolution_code,
        "evidence_ref": t.evidence_ref,
        "completed_by": t.completed_by,
        "verified_by": t.verified_by,
        "version": t.version,
    }
    if with_history:
        out["assignments"] = [
            {
                "employee_id": a.employee_id,
                "queue": a.queue,
                "stage": a.stage,
                "reason": a.reason,
                "policy_version": a.policy_version,
                "decision": a.decision,
                "assigned_by": a.assigned_by,
                "as_of": a.as_of,
                "ended_as_of": a.ended_as_of,
            }
            for a in t.assignments
        ]
        out["events"] = [
            {
                "event_type": ev.event_type,
                "from_status": ev.from_status,
                "to_status": ev.to_status,
                "actor": ev.actor,
                "payload": ev.payload,
                "as_of": ev.as_of,
                "recorded_at": ev.recorded_at,
            }
            for ev in t.events
        ]
    return out
